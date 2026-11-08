"""LLM provider abstraction: local Ollama (default) and an opt-in cloud provider (NVIDIA NIM)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class LLMError(Exception):
    """Base class for LLM errors (message is safe to show to users)."""


class LLMUnavailable(LLMError):
    """The runtime, endpoint or model is not reachable / installed."""


class LLMResponseError(LLMError):
    """The model kept returning output that failed schema validation."""


class LLMResult(BaseModel):
    text: str
    parsed: Any = None
    model: str
    latency_ms: int
    attempts: int = 1


class ModelInfo(BaseModel):
    name: str
    size_bytes: int | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    family: str | None = None


class LLMHealth(BaseModel):
    provider: str
    endpoint: str
    reachable: bool
    version: str | None = None
    model: str
    model_available: bool = False
    available_models: list[ModelInfo] = Field(default_factory=list)
    message: str = ""


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def generate(
        self, prompt: str, *, system: str | None = None, response_schema: type[BaseModel] | None = None
    ) -> LLMResult:
        """Run a completion. With ``response_schema`` the output is validated
        and returned in ``LLMResult.parsed``."""

    @abstractmethod
    def health(self) -> LLMHealth: ...

    def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        """One turn of native tool calling (Ollama-style messages). Returns (assistant message, model name)."""
        raise LLMError(f"Provider {self.name!r} does not support tool calling.")
