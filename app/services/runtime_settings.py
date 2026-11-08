"""User-adjustable runtime settings (stored in the local app database).

Switching the model backend to ``cloud`` is gated: the request must carry ``cloud_consent=true``
(the administrator accepted :data:`app.llm.cloud_notice.CLOUD_NOTICE`), and the acceptance is
recorded - who, when, which notice version - in the settings table and the audit log.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import repositories as repo
from app.llm.cloud_notice import CLOUD_NOTICE_VERSION
from app.utils.timeutil import iso, utcnow

_KEYS = ("ollama_model", "interpreter_mode", "llm_enabled", "llm_narrative_enabled", "llm_backend", "cloud_model")
_MODEL_PATTERN = r"^[A-Za-z0-9._:/\-]+$"
CONSENT_KEY = "runtime.cloud_consent"

Backend = Literal["local", "cloud"]


class RuntimeSettings(BaseModel):
    ollama_model: str = Field(min_length=1, max_length=100)
    interpreter_mode: Literal["auto", "llm", "rules"]
    llm_enabled: bool
    llm_narrative_enabled: bool
    llm_backend: Backend = "local"
    cloud_model: str = Field("meta/llama-3.3-70b-instruct", min_length=1, max_length=100)


class RuntimeSettingsUpdate(BaseModel):
    ollama_model: str | None = Field(None, min_length=1, max_length=100, pattern=_MODEL_PATTERN)
    interpreter_mode: Literal["auto", "llm", "rules"] | None = None
    llm_enabled: bool | None = None
    llm_narrative_enabled: bool | None = None
    llm_backend: Backend | None = None
    cloud_model: str | None = Field(None, min_length=1, max_length=100, pattern=_MODEL_PATTERN)
    cloud_consent: bool | None = Field(None, description="Required (true) when switching the backend to 'cloud'.")


class ConsentRequired(Exception):
    """Switching to the cloud backend without accepting the notice (or without an API key)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def load_runtime(session: Session, settings: Settings) -> RuntimeSettings:
    defaults = {
        "ollama_model": settings.ollama_model,
        "interpreter_mode": settings.interpreter_mode,
        "llm_enabled": settings.llm_enabled,
        "llm_narrative_enabled": settings.llm_narrative_enabled,
        # Never taken from .env: the cloud can only be enabled through the consent flow.
        "llm_backend": "local",
        "cloud_model": settings.cloud_model,
    }
    values = {k: repo.get_setting(session, f"runtime.{k}", defaults[k]) for k in _KEYS}
    return RuntimeSettings(**values)


def cloud_consent(session: Session) -> dict[str, Any] | None:
    return repo.get_setting(session, CONSENT_KEY, None)


def save_runtime(session: Session, update: RuntimeSettingsUpdate, actor: str, settings: Settings) -> list[str]:
    current = load_runtime(session, settings)
    if update.llm_backend == "cloud" and current.llm_backend != "cloud":
        if update.cloud_consent is not True:
            raise ConsentRequired("cloud_consent_required",
                                  "Switching to a cloud model requires accepting the cloud-data notice (cloud_consent=true).")
        key = settings.cloud_api_key
        if not (key and key.get_secret_value().strip()):
            raise ConsentRequired("cloud_api_key_missing",
                                  "No cloud API key is configured. Add PROOFLOOP_CLOUD_API_KEY to .env and restart ProofLoop.")
        record = {"accepted_by": actor, "accepted_at": iso(utcnow()), "notice_version": CLOUD_NOTICE_VERSION,
                  "endpoint": settings.cloud_base_url}
        repo.set_setting(session, CONSENT_KEY, record, actor)
        repo.audit(session, actor, "cloud_llm_consent_accepted",
                   f"notice {CLOUD_NOTICE_VERSION}; endpoint {settings.cloud_base_url}; data will leave this machine")
    changed = []
    for key, value in update.model_dump(exclude_none=True, exclude={"cloud_consent"}).items():
        repo.set_setting(session, f"runtime.{key}", value, actor)
        changed.append(f"{key}={value}")
    if update.llm_backend == "local" and current.llm_backend == "cloud":
        repo.audit(session, actor, "cloud_llm_disabled", "switched back to the local model")
    if changed:
        repo.audit(session, actor, "settings_changed", ", ".join(changed))
    return changed
