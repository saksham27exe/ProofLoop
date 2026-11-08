"""Application configuration.

All configuration comes from environment variables prefixed with
``PROOFLOOP_`` (or a local ``.env`` file). Nothing here enables any network
destination other than loopback: the Ollama URL is validated to be a local
address unless the operator explicitly opts out. The optional cloud model
(NVIDIA NIM) is configured here, but it can only be *activated* by an
administrator on the Settings page after accepting the cloud-data notice.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

InterpreterMode = Literal["auto", "llm", "rules"]


def is_loopback_url(url: str) -> bool:
    """True when ``url`` targets this machine only (localhost / 127.0.0.0/8 / ::1)."""
    try:
        host = urlparse(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROOFLOOP_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage -----------------------------------------------------------
    data_dir: Path = PROJECT_ROOT / "data"
    sample_data_dir: Path = PROJECT_ROOT / "sample_data"

    # Backend -------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Local LLM (Ollama) --------------------------------------------------
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b"
    ollama_timeout_seconds: float = Field(180.0, gt=0, le=1800)
    ollama_max_retries: int = Field(2, ge=0, le=5)
    llm_enabled: bool = True
    interpreter_mode: InterpreterMode = "auto"
    llm_narrative_enabled: bool = False
    allow_remote_ollama: bool = False

    # Cloud LLM (opt-in, NVIDIA NIM / OpenAI-compatible) -------------------
    # Having a key here does NOT enable the cloud: an admin must switch the backend and accept the notice.
    cloud_base_url: str = "https://integrate.api.nvidia.com/v1"
    cloud_model: str = "meta/llama-3.3-70b-instruct"
    cloud_api_key: SecretStr | None = None
    cloud_timeout_seconds: float = Field(120.0, gt=0, le=600)
    cloud_max_retries: int = Field(2, ge=0, le=5)

    # Authentication ------------------------------------------------------
    session_ttl_hours: int = Field(12, ge=1, le=168)
    password_iterations: int = Field(310_000, ge=1_000)
    max_failed_logins: int = Field(5, ge=1, le=50)
    lockout_minutes: int = Field(5, ge=1, le=1440)

    log_level: str = "INFO"

    @field_validator("data_dir", "sample_data_dir", mode="after")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value)

    @model_validator(mode="after")
    def _privacy_guard(self) -> Settings:
        if not self.allow_remote_ollama and not is_loopback_url(self.ollama_url):
            raise ValueError(
                "PROOFLOOP_OLLAMA_URL must point to this machine (localhost / 127.0.0.1). "
                "ProofLoop never sends prompts or execution data off the machine. "
                "Set PROOFLOOP_ALLOW_REMOTE_OLLAMA=true only for a host you fully control."
            )
        if not (self.cloud_base_url.lower().startswith("https://") or is_loopback_url(self.cloud_base_url)):
            raise ValueError("PROOFLOOP_CLOUD_BASE_URL must use HTTPS.")
        return self

    # Derived paths -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "proofloop.db"

    @property
    def sandbox_dir(self) -> Path:
        return self.data_dir / "sandbox"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def benchmark_dir(self) -> Path:
        return self.data_dir / "benchmark"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.sandbox_dir, self.reports_dir, self.logs_dir, self.benchmark_dir):
            path.mkdir(parents=True, exist_ok=True)

    def public_view(self) -> dict[str, object]:
        """Non-secret configuration suitable for display in the dashboard."""
        return {
            "data_dir": str(self.data_dir),
            "database_path": str(self.db_path),
            "sandbox_dir": str(self.sandbox_dir),
            "api_bind": f"{self.api_host}:{self.api_port}",
            "ollama_url": self.ollama_url,
            "ollama_default_model": self.ollama_model,
            "ollama_timeout_seconds": self.ollama_timeout_seconds,
            "ollama_max_retries": self.ollama_max_retries,
            "allow_remote_ollama": self.allow_remote_ollama,
            "cloud_base_url": self.cloud_base_url,
            "cloud_default_model": self.cloud_model,
            "cloud_api_key_configured": bool(self.cloud_api_key and self.cloud_api_key.get_secret_value().strip()),
            "session_ttl_hours": self.session_ttl_hours,
            "max_failed_logins": self.max_failed_logins,
            "lockout_minutes": self.lockout_minutes,
            "telemetry": "disabled (none implemented)",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
