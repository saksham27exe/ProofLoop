"""Evaluate task interpretation (rules and/or a local Ollama model) on 34 hand-labelled instructions.

    python scripts/evaluate_llm_interpreter.py --mode rules            # instant, deterministic
    python scripts/evaluate_llm_interpreter.py --mode llm              # every case through the local model
    python scripts/evaluate_llm_interpreter.py --mode auto --model qwen2.5:7b

Cases cover the documented grammar, free-form wording, vague instructions and prompt-injection attempts.
The key safety number is the *wrong-plan rate*: a verifiable plan that differs from the task.
Results are written to data/benchmark/interpretation_<mode>_<timestamp>.json. Nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.benchmark.interpretation_eval import evaluate  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.interpretation.task_parser import TaskInterpreter  # noqa: E402
from app.llm.model_manager import ModelManager  # noqa: E402
from app.utils.timeutil import utcnow  # noqa: E402


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Evaluate ProofLoop task interpretation")
    parser.add_argument("--mode", choices=["rules", "llm", "auto"], default="llm")
    parser.add_argument("--model", default=settings.ollama_model)
    args = parser.parse_args()

    models = ModelManager(settings)
    if args.mode != "rules":
        health = models.health(args.model)
        if not health.model_available:
            print(health.message)
            return 1
        print(f"Using local model {args.model} - on a CPU-only machine expect 10-60 s per model call.")
    interpreter = TaskInterpreter(lambda: models.provider(args.model), args.mode, True)

    def progress(i: int, n: int, text: str) -> None:
        print(f"[{i:>2}/{n}] {text[:70]}", flush=True)

    results = evaluate(interpreter, progress)
    results["config"] = {"mode": args.mode, "model": args.model if args.mode != "rules" else None,
                         "generated_at": utcnow().isoformat() + "Z"}
    print()
    for row in results["rows"]:
        if row["outcome"] != "correct":
            print(f"  {row['outcome'].upper():<8} [{row['interpreter']}] {row['instruction'][:60]}\n           {row['detail'][:160]}")
    o = results["outcomes"]
    print(f"\nmode={args.mode}  correct {o['correct']}/{results['cases']}  refused {o['refused']}  "
          f"WRONG {o['wrong']}  errors {o['error']}")
    for cat, c in results["by_category"].items():
        print(f"  {cat:<10} correct {c['correct']}/{c['total']}  refused {c['refused']}  wrong {c['wrong']}  errors {c['error']}")
    lat = results["llm_latency_ms"]
    if results["llm_calls"]:
        print(f"  local model calls: {results['llm_calls']}  latency p50 {lat['p50']} ms  p95 {lat['p95']} ms  max {lat['max']} ms")
    settings.benchmark_dir.mkdir(parents=True, exist_ok=True)
    out = settings.benchmark_dir / f"interpretation_{args.mode}_{utcnow().strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nFull results: {out}")
    return 0 if o["wrong"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
