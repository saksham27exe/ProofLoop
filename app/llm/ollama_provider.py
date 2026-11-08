"""Ollama provider - local inference over the localhost HTTP API.

Privacy guarantees enforced here (not just documented):

* The endpoint must be a loopback address unless explicitly overridden.
* ``trust_env=False`` so system proxy variables can never route prompts
  through a proxy server.
* There is no fallback to any remote/cloud provider. If Ollama is down the
  caller receives :class:`LLMUnavailable` and decides what to do (ProofLoop
  falls back to its *local* rule-based interpreter).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.config import is_loopback_url
from app.llm.base import (
    LLMError,
    LLMHealth,
    LLMProvider,
    LLMResponseError,
    LLMResult,
    LLMUnavailable,
    ModelInfo,
)
from app.utils.timeutil import Stopwatch


def model_matches(requested: str, available: str) -> bool:
    def norm(name: str) -> str:
        return name if ":" in name else f"{name}:latest"

    return norm(requested.strip()) == norm(available.strip())


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 120.0,
        max_retries: int = 2,
        allow_remote: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not allow_remote and not is_loopback_url(base_url):
            raise LLMError(f"Refusing non-local Ollama endpoint {base_url!r}: ProofLoop only performs local inference.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout or self.timeout, connect=5.0),
            trust_env=False,
            transport=self._transport,
        )

    # ----------------------------------------------------------------- health
    def list_models(self) -> list[ModelInfo]:
        try:
            with self._client(timeout=10) as client:
                response = client.get("/api/tags")
                response.raise_for_status()
                data = response.json()
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"Ollama is not running at {self.base_url}. Start it with 'ollama serve'.") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Could not list Ollama models: {exc}") from exc
        models = []
        for m in data.get("models", []) or []:
            details = m.get("details") or {}
            models.append(
                ModelInfo(
                    name=m.get("name") or m.get("model") or "?",
                    size_bytes=m.get("size"),
                    parameter_size=details.get("parameter_size"),
                    quantization=details.get("quantization_level"),
                    family=details.get("family"),
                )
            )
        return models

    def health(self) -> LLMHealth:
        health = LLMHealth(provider=self.name, endpoint=self.base_url, reachable=False, model=self.model)
        try:
            with self._client(timeout=5) as client:
                version = client.get("/api/version")
                version.raise_for_status()
                health.version = version.json().get("version")
            health.reachable = True
            health.available_models = self.list_models()
        except (httpx.HTTPError, LLMUnavailable, ValueError):
            health.message = (
                f"Ollama is not reachable at {self.base_url}. Install it from https://ollama.com and start it, "
                "or keep using verification-only mode (rule-based interpretation)."
            )
            return health
        health.model_available = any(model_matches(self.model, m.name) for m in health.available_models)
        if health.model_available:
            health.message = f"Ollama {health.version} is running and model '{self.model}' is installed."
        else:
            health.message = (
                f"Ollama {health.version} is running but model '{self.model}' is not installed. "
                f"Download it with: ollama pull {self.model}"
            )
        return health

    # --------------------------------------------------------------- generate
    def _chat(self, messages: list[dict[str, str]], schema: dict[str, Any] | None) -> tuple[str, str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Deterministic decoding; num_predict caps a runaway generation on slow CPU-only machines.
            "options": {"temperature": 0, "seed": 7, "num_predict": 512},
            "keep_alive": "10m",
        }
        if schema is not None:
            payload["format"] = schema
        try:
            with self._client() as client:
                response = client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"Ollama is not running at {self.base_url}.") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"Ollama did not answer within {self.timeout:.0f}s (the model may still be loading - try again)."
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        if response.status_code == 404:
            raise LLMUnavailable(f"Model '{self.model}' is not installed. Download it with: ollama pull {self.model}")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("error", "")
            except ValueError:
                detail = response.text[:200]
            raise LLMError(f"Ollama returned HTTP {response.status_code}: {detail}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError("Ollama returned a non-JSON response envelope.") from exc
        content = ((data.get("message") or {}).get("content")) or data.get("response") or ""
        return content, data.get("model") or self.model

    def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        """One turn of native tool calling. Returns (assistant message, model name).

        The message may contain ``tool_calls`` ([{"function": {"name", "arguments"}}]) and/or ``content``.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0, "seed": 7, "num_predict": 768},
            "keep_alive": "10m",
        }
        try:
            with self._client() as client:
                response = client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"Ollama is not running at {self.base_url}.") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"Ollama did not answer within {self.timeout:.0f}s.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        if response.status_code == 404:
            raise LLMUnavailable(f"Model '{self.model}' is not installed. Download it with: ollama pull {self.model}")
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", "")
            except ValueError:
                detail = response.text[:200]
            if "does not support tools" in str(detail):
                raise LLMError(f"Model '{self.model}' does not support tool calling. Choose a tool-capable model "
                               "such as qwen2.5:3b, qwen2.5:7b or llama3.1:8b.")
            raise LLMError(f"Ollama returned HTTP {response.status_code}: {detail}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError("Ollama returned a non-JSON response envelope.") from exc
        return data.get("message") or {}, data.get("model") or self.model

    def generate(
        self, prompt: str, *, system: str | None = None, response_schema: type[BaseModel] | None = None
    ) -> LLMResult:
        timer = Stopwatch()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        schema = response_schema.model_json_schema() if response_schema else None
        attempts = 0
        last_error = ""
        while attempts <= self.max_retries:
            attempts += 1
            content, model = self._chat(messages, schema)
            if response_schema is None:
                return LLMResult(text=content, model=model, latency_ms=timer.ms, attempts=attempts)
            try:
                parsed = response_schema.model_validate_json(_extract_json(content))
                return LLMResult(text=content, parsed=parsed, model=model, latency_ms=timer.ms, attempts=attempts)
            except (ValidationError, ValueError) as exc:
                last_error = _short_error(exc)
                messages.append({"role": "assistant", "content": content[:4000]})
                messages.append(
                    {
                        "role": "user",
                        "content": "Your previous reply did not match the required JSON schema "
                        f"({last_error}). Reply again with ONLY a valid JSON object matching the schema.",
                    }
                )
        raise LLMResponseError(
            f"The local model returned malformed structured output {attempts} time(s): {last_error}"
        )


def _extract_json(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    candidate = text[start : end + 1]
    json.loads(candidate)  # raises ValueError on malformed JSON
    return candidate


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()[:3]
        return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errs)
    return str(exc)[:200]
