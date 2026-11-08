"""Run the reproducible ProofLoop benchmark from the command line.

    python scripts/run_benchmark.py                 # deterministic rule-based interpreter
    python scripts/run_benchmark.py --interpreter llm --model qwen2.5:3b
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.benchmark.runner import markdown_summary, run_benchmark, save_results  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ProofLoop benchmark")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--variants", type=int, default=3, help="task instances per template family")
    parser.add_argument("--interpreter", choices=["rules", "llm", "auto"], default="rules")
    parser.add_argument("--model", default=None, help="Ollama model (for --interpreter llm/auto)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    settings = get_settings()

    def progress(i: int, n: int, case_id: str) -> None:
        if not args.quiet:
            print(f"\r[{i:>3}/{n}] {case_id:<40}", end="", flush=True)

    results = run_benchmark(settings, seed=args.seed, variants=args.variants, interpreter=args.interpreter,
                            model=args.model, progress=progress)
    print()
    path = save_results(results, settings.benchmark_dir)
    print(markdown_summary(results))
    print()
    for name, t in results["targets"].items():
        print(f"  target {name:<36} {t['target']:<8} actual={t['actual']}  {'MET' if t['met'] else 'NOT MET'}")
    if results["errors"]:
        print(f"\n{len(results['errors'])} case(s) raised errors - see {path}")
    print(f"\nFull results: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
