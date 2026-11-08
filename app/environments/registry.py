"""Registry of the sandbox environments available to ProofLoop."""

from __future__ import annotations

from pathlib import Path

from app.environments.api_environment import ApiEnvironment
from app.environments.base import Environment
from app.environments.database_environment import DatabaseEnvironment
from app.environments.file_environment import FileEnvironment


class EnvironmentRegistry:
    def __init__(self, sandbox_dir: Path, sample_data_dir: Path) -> None:
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        self._envs: dict[str, Environment] = {
            "database": DatabaseEnvironment(sandbox_dir, sample_data_dir),
            "api": ApiEnvironment(sandbox_dir, sample_data_dir),
            "file": FileEnvironment(sandbox_dir, sample_data_dir),
        }

    def initialize(self) -> None:
        for env in self._envs.values():
            env.ensure_initialized()

    BUILTIN = ("database", "api", "file")

    def get(self, name: str) -> Environment:
        try:
            return self._envs[name]
        except KeyError:
            raise KeyError(f"Unknown environment {name!r}; expected one of {sorted(self._envs)}") from None

    def register(self, env: Environment) -> None:
        """Add or replace a connected data source."""
        if env.name in self.BUILTIN:
            raise ValueError(f"{env.name!r} is reserved for a built-in sandbox")
        self._envs[env.name] = env

    def unregister(self, name: str) -> None:
        if name in self.BUILTIN:
            raise ValueError("Built-in sandboxes cannot be removed")
        self._envs.pop(name, None)

    def is_builtin(self, name: str) -> bool:
        return name in self.BUILTIN

    def names(self) -> list[str]:
        return list(self._envs)

    def all(self) -> list[Environment]:
        return list(self._envs.values())
