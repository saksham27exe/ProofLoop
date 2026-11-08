"""Cloud provider - NVIDIA NIM (or any OpenAI-compatible endpoint) over HTTPS.

This provider is **opt-in only**. It is used only when an administrator has switched the
model backend to ``cloud`` on the Settings page *and* accepted the cloud-data notice
(see :mod:`app.llm.cloud_notice`). Configuring an API key alone never enables it.

When it is active, task instructions, data-source structure and the rows the AI agent
reads are sent to the provider. The verification engine is unaffected: verdicts are
still computed locally by deterministic rules and never by a model.

Implementation notes:

* Plain ``httpx`` - no vendor SDK. The endpoint must be HTTPS (or loopback, for testing).
* The API key is held as a ``SecretStr`` and only ever placed in the ``Authorization``
  header; it is never logged, returned or included in error messages.
* Messages arrive in the Ollama-style shape used by the rest of ProofLoop and are
  translated to/from the OpenAI chat-completions format here, so the agent and the
  interpreter work unchanged with either backend.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.config import is_loopback_url
from app.llm.base import LLMError, LLMHealth, LLMProvider, LLMResponseError, LLMResult, LLMUnavailable
from app.llm.ollama_provider import _extract_json, _short_error
from app.utils.timeutil import Stopwatch

NVIDIA_NIM_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_KEYS_PAGE = "https://build.nvidia.com"

# Catalog entries that are not chat models (embedding, safety, reward, parsing, retrieval, image generation).
_NON_CHAT = re.compile(r"embed|guard|safety|reward|parse|retriever|rerank|diffusion|topic-control|clip|nemoretriever", re.I)
_THINK = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)

RATE_LIMIT_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 20.0


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    try:
        wait = float(retry_after) if retry_after is not None else 0.0
    except ValueError:
        wait = 0.0
    return min(MAX_RETRY_WAIT_SECONDS, wait if wait > 0 else 2.0 * (2 ** attempt))


# Reasoning models spend tokens on hidden "thinking" before the answer; leave room for both.
MAX_TOKENS = 4096


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks some reasoning models put in the answer text."""
    return _THINK.sub("", text or "").strip()


class CloudProvider(LLMProvider):
    name = "cloud"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: SecretStr | None,
        *,
        timeout: float = 120.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (base_url.lower().startswith("https://") or is_loopback_url(base_url)):
            raise LLMError(f"Refusing cloud endpoint {base_url!r}: it must use HTTPS.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport
        self._sleep = sleep

    @property
    def has_key(self) -> bool:
        return bool(self._api_key and self._api_key.get_secret_value().strip())

    def _client(self, timeout: float | None = None) -> httpx.Client:
        if not self.has_key:
            raise LLMUnavailable("No cloud API key is configured. Add PROOFLOOP_CLOUD_API_KEY to .env and restart ProofLoop.")
        assert self._api_key is not None
        return httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout or self.timeout, connect=10.0),
            headers={"Authorization": f"Bearer {self._api_key.get_secret_value().strip()}", "Accept": "application/json"},
            trust_env=False,
            transport=self._transport,
        )

    # ----------------------------------------------------------------- catalog
    def list_models(self) -> list[str]:
        """Live list of chat-capable model ids. The NIM catalog is public: no key is sent and no user data."""
        try:
            with httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(15, connect=10.0), trust_env=False,
                              transport=self._transport) as client:
                response = client.get("/models", headers={"Accept": "application/json"})
                response.raise_for_status()
                data = response.json().get("data") or []
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise LLMUnavailable(f"Could not read the model catalog at {self.base_url} ({type(exc).__name__}).") from exc
        return sorted({str(m.get("id")) for m in data if m.get("id") and not _NON_CHAT.search(str(m.get("id")))})

    # ----------------------------------------------------------------- health
    def health(self) -> LLMHealth:
        health = LLMHealth(provider=self.name, endpoint=self.base_url, reachable=False, model=self.model)
        if not self.has_key:
            health.message = (f"No cloud API key configured. Create a free key at {NVIDIA_KEYS_PAGE}, put it in .env as "
                              "PROOFLOOP_CLOUD_API_KEY and restart ProofLoop.")
            return health
        try:
            with self._client(timeout=15) as client:
                response = client.get("/models")
        except httpx.HTTPError as exc:
            health.message = f"The cloud endpoint {self.base_url} is not reachable ({type(exc).__name__}). Check your internet connection."
            return health
        if response.status_code in (401, 403):
            health.message = f"The cloud API rejected the key (HTTP {response.status_code}). Check PROOFLOOP_CLOUD_API_KEY."
            return health
        if response.status_code >= 400:
            health.message = f"The cloud endpoint returned HTTP {response.status_code}."
            return health
        health.reachable = True
        try:
            ids = {str(m.get("id")) for m in (response.json().get("data") or [])}
        except (ValueError, AttributeError):
            ids = set()
        # Some endpoints do not list every model; an empty list is not proof that the model is missing.
        health.model_available = (self.model in ids) if ids else True
        health.message = (f"Cloud endpoint reachable; model '{self.model}' is available." if health.model_available
                          else f"Cloud endpoint reachable, but model '{self.model}' is not in its catalog any more "
                               "(NVIDIA retires models). Choose a current one on Settings -> Model.")
        return health

    # ------------------------------------------------------------ translation
    @staticmethod
    def _to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Ollama-style messages -> OpenAI chat format (tool calls need ids that tool results refer to)."""
        out: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        counter = 0
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    fn = c.get("function") or {}
                    counter += 1
                    call_id = f"call_{counter}"
                    pending_ids.append(call_id)
                    args = fn.get("arguments")
                    calls.append({"id": call_id, "type": "function",
                                  "function": {"name": fn.get("name"),
                                               "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
                out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
            elif role == "tool":
                call_id = pending_ids.pop(0) if pending_ids else f"call_orphan_{counter}"
                out.append({"role": "tool", "tool_call_id": call_id, "content": m.get("content") or ""})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    @staticmethod
    def _from_openai(message: dict[str, Any]) -> dict[str, Any]:
        calls = []
        for c in message.get("tool_calls") or []:
            fn = c.get("function") or {}
            raw = fn.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
            except ValueError:
                args = {}
            calls.append({"function": {"name": fn.get("name"), "arguments": args if isinstance(args, dict) else {}}})
        return {"role": "assistant", "content": strip_reasoning(message.get("content") or ""), "tool_calls": calls}

    # ------------------------------------------------------------------- call
    def _complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, max_tokens: int) -> tuple[dict, str]:
        payload: dict[str, Any] = {"model": self.model, "messages": self._to_openai(messages), "temperature": 0,
                                   "max_tokens": max_tokens, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                with self._client() as client:
                    response = client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                raise LLMError(f"The cloud model did not answer within {self.timeout:.0f}s.") from exc
            except httpx.HTTPError as exc:
                raise LLMUnavailable(f"Could not reach the cloud endpoint ({type(exc).__name__}).") from exc
            if response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
                break
            # Free tiers are rate-limited per minute: wait (honouring Retry-After) and try again.
            self._sleep(_retry_delay(response.headers.get("retry-after"), attempt))
        status = response.status_code
        if status in (401, 403):
            raise LLMUnavailable("The cloud API rejected the key. Check PROOFLOOP_CLOUD_API_KEY in .env.")
        if status == 404:
            raise LLMUnavailable(f"Model '{self.model}' is not available at the cloud endpoint.")
        if status == 429:
            raise LLMError(f"The cloud API rate limit was still reached after {RATE_LIMIT_RETRIES} retries (free tier: about "
                           "40 requests/minute). Wait a minute and try again.")
        if status >= 400:
            try:
                body = response.json()
                detail = str(body.get("detail") or body.get("error") or body)[:300]
            except ValueError:
                detail = response.text[:300]
            if tools and "tool" in detail.lower():
                raise LLMError(f"Model '{self.model}' does not support tool calling on this endpoint. "
                               "Choose a model whose build.nvidia.com page lists function calling, such as nvidia/nemotron-3.5-lightning-30b-a3b.")
            raise LLMError(f"The cloud API returned HTTP {status}: {detail}")
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"] or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError("The cloud API returned an unexpected response envelope.") from exc
        if (choice.get("finish_reason") == "length" and not message.get("tool_calls")
                and not strip_reasoning(message.get("content") or "")):
            raise LLMError(f"Model '{self.model}' used its whole output budget ({max_tokens} tokens) on reasoning without "
                           "answering. Try again or choose a faster model.")
        return message, data.get("model") or self.model

    def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        """Same contract as :meth:`OllamaProvider.chat_with_tools`."""
        message, model = self._complete(messages, tools, max_tokens=MAX_TOKENS)
        return self._from_openai(message), model

    def generate(
        self, prompt: str, *, system: str | None = None, response_schema: type[BaseModel] | None = None
    ) -> LLMResult:
        timer = Stopwatch()
        sys_text = system or ""
        if response_schema is not None:
            sys_text += ("\n\nReply with ONLY a JSON object (no prose, no code fences) that matches this JSON schema:\n"
                         + json.dumps(response_schema.model_json_schema()))
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys_text.strip()}] if sys_text.strip() else []
        messages.append({"role": "user", "content": prompt})
        attempts, last_error = 0, ""
        while attempts <= self.max_retries:
            attempts += 1
            message, model = self._complete(messages, None, max_tokens=MAX_TOKENS)
            content = strip_reasoning(message.get("content") or "")
            if response_schema is None:
                return LLMResult(text=content, model=model, latency_ms=timer.ms, attempts=attempts)
            try:
                parsed = response_schema.model_validate_json(_extract_json(content))
                return LLMResult(text=content, parsed=parsed, model=model, latency_ms=timer.ms, attempts=attempts)
            except (ValidationError, ValueError) as exc:
                last_error = _short_error(exc)
                messages.append({"role": "assistant", "content": content[:4000]})
                messages.append({"role": "user", "content": "Your previous reply did not match the required JSON schema "
                                 f"({last_error}). Reply again with ONLY a valid JSON object matching the schema."})
        raise LLMResponseError(f"The cloud model returned malformed structured output {attempts} time(s): {last_error}")
