"""Create ProofLoop's local database and seed the sandbox environments.

    python scripts/initialize_db.py            # create if missing (safe to re-run)
    python scripts/initialize_db.py --reset    # DELETE all local data and start fresh (asks first)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database.connection import Database  # noqa: E402
from app.environments.registry import EnvironmentRegistry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete ALL local ProofLoop data first")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()
    settings = get_settings()

    if args.reset:
        print(f"This permanently deletes {settings.data_dir} (accounts, executions, reports, sandboxes, the practice "
              "store, run backups and the list of connected data sources). Your connected databases and folders "
              "themselves are not touched.")
        if not args.yes and input("Type DELETE to continue: ").strip() != "DELETE":
            print("Cancelled.")
            return 1
        if settings.data_dir.exists():
            shutil.rmtree(settings.data_dir)
            print("Local data removed.")

    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.create_all()
    db.dispose()
    EnvironmentRegistry(settings.sandbox_dir, settings.sample_data_dir).initialize()
    print(f"Database ready:  {settings.db_path}")
    print(f"Sandboxes ready: {settings.sandbox_dir}")
    print("Create your administrator account on first sign-in at http://localhost:8501")
    return 0


if __name__ == "__main__":
    sys.exit(main())
