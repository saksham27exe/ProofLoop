"""Shared pytest fixtures. Every test gets an isolated temporary data directory."""

from __future__ import annotations

import os
import tempfile
import warnings
from pathlib import Path

# Make sure importing app.main never touches the developer's real ./data folder.
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="proofloop-tests-"))
os.environ.setdefault("PROOFLOOP_DATA_DIR", str(_SESSION_TMP))
os.environ.setdefault("PROOFLOOP_PASSWORD_ITERATIONS", "1000")
os.environ.setdefault("PROOFLOOP_OLLAMA_URL", "http://127.0.0.1:9")  # nothing listens here
warnings.filterwarnings("ignore", category=DeprecationWarning)

import pytest  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database.connection import Database  # noqa: E402
from app.environments.registry import EnvironmentRegistry  # noqa: E402
from app.llm.model_manager import ModelManager  # noqa: E402
from app.services.execution_service import ExecutionService  # noqa: E402
from app.services.runtime_settings import RuntimeSettings  # noqa: E402

ADMIN_PASSWORD = "Admin-Pass-123"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", password_iterations=1000, ollama_url="http://127.0.0.1:9",
                 interpreter_mode="rules", cloud_api_key=None)  # never pick up a developer's real key
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def registry(settings: Settings) -> EnvironmentRegistry:
    reg = EnvironmentRegistry(settings.sandbox_dir, settings.sample_data_dir)
    reg.initialize()
    return reg


@pytest.fixture
def service(settings: Settings, db: Database, registry: EnvironmentRegistry) -> ExecutionService:
    runtime = RuntimeSettings(ollama_model="qwen2.5:3b", interpreter_mode="rules", llm_enabled=False,
                              llm_narrative_enabled=False)
    return ExecutionService(settings, db, registry, ModelManager(settings), runtime_override=runtime)


@pytest.fixture
def run_task(service: ExecutionService):
    """Create, run and verify a task; return the report."""

    def _run(instruction: str, environment: str = "database", fault: str = "none", reset: bool = True) -> dict:
        exec_id = service.create_execution(instruction=instruction, environment=environment, fault_mode=fault,
                                           reset_sandbox=reset)
        return service.run_and_verify(exec_id, approved=True, force=True)

    return _run


@pytest.fixture
def client(settings: Settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers(client) -> dict[str, str]:
    r = client.post("/auth/bootstrap", json={"username": "admin", "password": ADMIN_PASSWORD, "full_name": "Admin"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
