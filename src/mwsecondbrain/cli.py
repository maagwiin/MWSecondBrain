"""Local administration only; passwords never travel through HTTP or argv."""

import argparse
import getpass
import sys

from .auth import Auth
from .config import Settings
from .db import Database


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mwsb")
    parser.add_argument("command", choices=("init-password", "reset-password"))
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        auth = Auth(Database(settings.state_dir))
        password = getpass.getpass("New password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match")
        auth.set_password(password, require_uninitialized=args.command == "init-password")
    except (ValueError, EOFError, KeyboardInterrupt) as error:
        print(str(error) or "Password update cancelled", file=sys.stderr)
        return 1
    print("Password updated. Existing sessions revoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
