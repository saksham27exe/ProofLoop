"""Check that this machine is ready to run ProofLoop. Exit code 0 = ready.

    python scripts/check_environment.py
"""

from __future__ import annotations

import importlib
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check that this machine is ready to run ProofLoop")
    parser.add_argument("--ui-port", type=int, default=8501)
    args = parser.parse_args()
    problems = 0
    v = sys.version_info
    if v >= (3, 11):
        print(f"{OK} Python {v.major}.{v.minor}.{v.micro}")
    else:
        print(f"{FAIL} Python {v.major}.{v.minor} found - ProofLoop needs Python 3.11 or newer")
        problems += 1

    for module in ("fastapi", "uvicorn", "sqlalchemy", "pydantic", "pydantic_settings", "httpx", "streamlit", "altair", "pandas"):
        try:
            mod = importlib.import_module(module)
            print(f"{OK} {module} {getattr(mod, '__version__', '')}")
        except Exception as exc:  # noqa: BLE001
            print(f"{FAIL} {module} is not importable ({exc}). Run: pip install -r requirements.txt")
            problems += 1
    if problems:
        return 1

    from app.config import get_settings
    from app.llm.model_manager import ModelManager

    settings = get_settings()
    print(f"{OK} Data directory: {settings.data_dir}")
    if len(str(ROOT)) > 150:
        print(f"{WARN} The project path is long ({len(str(ROOT))} chars). Windows' 260-character limit can break "
              "Python packages; move the folder closer to the drive root or enable long paths.")
    print(f"{OK} Database file {'exists' if settings.db_path.exists() else 'will be created on first start'}")

    for port, name in ((settings.api_port, "API"), (args.ui_port, "dashboard")):
        print(f"{OK if port_free(port) else WARN} Port {port} ({name}) {'is free' if port_free(port) else 'is already in use'}")

    health = ModelManager(settings).health()
    if not health.reachable:
        print(f"{WARN} Ollama not reachable at {settings.ollama_url} - ProofLoop will run in verification-only mode.")
    elif not health.model_available:
        print(f"{WARN} Ollama {health.version} running, model '{settings.ollama_model}' not installed "
              f"(optional): ollama pull {settings.ollama_model}")
    else:
        print(f"{OK} Ollama {health.version} with model '{settings.ollama_model}'")
    print("\nReady." if not problems else "\nNot ready.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
