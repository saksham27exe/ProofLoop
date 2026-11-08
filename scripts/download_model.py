"""Download a local model through Ollama - only after explicit confirmation.

    python scripts/download_model.py                 # default model from settings
    python scripts/download_model.py --model llama3.2:3b
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.llm.model_manager import RECOMMENDED_MODELS, ModelManager  # noqa: E402


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=settings.ollama_model)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    health = ModelManager(settings).health(args.model)
    if not health.reachable:
        print(f"Ollama is not running at {settings.ollama_url}.")
        print("Install it from https://ollama.com/download, start it, then re-run this script.")
        return 1
    if health.model_available:
        print(f"Model '{args.model}' is already installed. Nothing to do.")
        return 0

    info = next((m for m in RECOMMENDED_MODELS if m["name"] == args.model), None)
    size = info["download"] if info else "several GB"
    print(f"Model '{args.model}' is not installed. Download size: {size} (one-time internet access).")
    if not args.yes and input("Download it now? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Skipped. ProofLoop keeps working in verification-only mode.")
        return 0

    print("Downloading through the local Ollama runtime...")
    with httpx.Client(base_url=settings.ollama_url, timeout=None, trust_env=False) as client, client.stream(
        "POST", "/api/pull", json={"model": args.model, "stream": True}
    ) as response:
        last = ""
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            if "error" in event:
                print(f"\nError: {event['error']}")
                return 1
            status = event.get("status", "")
            total, done = event.get("total"), event.get("completed")
            msg = f"{status} {done * 100 // total}%" if total and done else status
            if msg != last:
                print(f"\r{msg:<70}", end="", flush=True)
                last = msg
    print(f"\nModel '{args.model}' installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
