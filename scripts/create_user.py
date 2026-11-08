"""Create a ProofLoop user (or the first admin) from the command line.

    python scripts/create_user.py --username alice --role analyst
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth.service import AuthError, AuthService  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database.connection import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--full-name", default="")
    parser.add_argument("--role", choices=["viewer", "analyst", "admin"], default="analyst")
    args = parser.parse_args()
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm password: "):
        print("Passwords do not match.")
        return 1
    settings = get_settings()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    db.create_all()
    auth = AuthService(settings)
    try:
        with db.session() as s:
            if auth.needs_bootstrap(s):
                auth.bootstrap_admin(s, args.username, password, args.full_name)
                print(f"Created first administrator '{args.username}'.")
            else:
                auth.create_user(s, args.username, password, args.full_name, args.role, actor="cli")
                print(f"Created {args.role} '{args.username}'.")
    except AuthError as err:
        print(f"Error: {err.message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
