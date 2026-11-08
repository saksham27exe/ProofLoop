"""Builds the configured LLM provider (local Ollama by default, cloud only after consent) and reports status."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import Settings
from app.llm.base import LLMHealth, LLMProvider
from app.llm.cloud_provider import CloudProvider
from app.llm.ollama_provider import OllamaProvider

if TYPE_CHECKING:
    from app.services.runtime_settings import RuntimeSettings

RECOMMENDED_MODELS = [
    {"name": "qwen2.5:3b", "download": "~1.9 GB", "ram": "8 GB+", "note": "Recommended default: small, good at JSON."},
    {"name": "llama3.2:3b", "download": "~2.0 GB", "ram": "8 GB+", "note": "Alternative small model."},
    {"name": "qwen2.5:7b", "download": "~4.7 GB", "ram": "16 GB+", "note": "More robust on free-form phrasing."},
]

RECOMMENDED_CLOUD_MODELS = [
    {"name": "meta/llama-3.3-70b-instruct", "note": "Recommended default: strong tool calling and JSON."},
    {"name": "meta/llama-3.1-70b-instruct", "note": "Alternative 70B model with tool calling."},
    {"name": "meta/llama-3.1-8b-instruct", "note": "Faster and lighter; less reliable on complex tasks."},
]


class ModelManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def provider(self, model: str | None = None) -> OllamaProvider:
        """The local Ollama provider."""
        return OllamaProvider(
            self.settings.ollama_url,
            model or self.settings.ollama_model,
            timeout=self.settings.ollama_timeout_seconds,
            max_retries=self.settings.ollama_max_retries,
            allow_remote=self.settings.allow_remote_ollama,
        )

    def cloud_provider(self, model: str | None = None) -> CloudProvider:
        return CloudProvider(
            self.settings.cloud_base_url,
            model or self.settings.cloud_model,
            self.settings.cloud_api_key,
            timeout=self.settings.cloud_timeout_seconds,
            max_retries=self.settings.cloud_max_retries,
        )

    def for_runtime(self, rt: RuntimeSettings) -> LLMProvider:
        """The provider selected in the runtime settings. Cloud only when the admin switched to it (with consent)."""
        if rt.llm_backend == "cloud":
            return self.cloud_provider(rt.cloud_model)
        return self.provider(rt.ollama_model)

    def health(self, model: str | None = None) -> LLMHealth:
        return self.provider(model).health()

    def health_for_runtime(self, rt: RuntimeSettings) -> LLMHealth:
        return self.for_runtime(rt).health()
