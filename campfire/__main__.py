"""Campfire operator command line."""

import argparse
import json
import sys


def parser():
    command = argparse.ArgumentParser(prog="python -m campfire")
    subcommands = command.add_subparsers(dest="command")
    subcommands.add_parser("serve", help="migrate and run the Campfire server")
    subcommands.add_parser("migrate", help="apply pending database migrations offline")
    backup = subcommands.add_parser("backup", help="create a consistent backup directory")
    backup.add_argument("destination")
    verify = subcommands.add_parser("verify-backup", help="verify a backup without changing data")
    verify.add_argument("source")
    restore = subcommands.add_parser("restore", help="restore a verified backup while offline")
    restore.add_argument("source")
    restore.add_argument("--confirm", action="store_true",
                         help="confirm replacement of the configured database and uploads")
    subcommands.add_parser("check-config", help="validate configuration and exit")
    return command


def main(argv=None):
    arguments = parser().parse_args(argv)
    selected = arguments.command or "serve"
    try:
        from .config import DB_PATH, UPLOAD_DIR, validate_configuration
        if selected == "serve":
            from .http import main as serve
            serve()
            return 0
        if selected == "check-config":
            validate_configuration()
            print("configuration is safe")
            return 0
        if selected == "migrate":
            from .database import initialize_database
            from .instance_lock import operation_lock
            with operation_lock(DB_PATH, exclusive=True, blocking=False):
                version = initialize_database()
            print(f"database schema is at version {version}")
            return 0
        from .operations import create_backup, restore_backup, verify_backup
        if selected == "backup":
            manifest = create_backup(arguments.destination, DB_PATH, UPLOAD_DIR)
        elif selected == "verify-backup":
            manifest = verify_backup(arguments.source)
        elif selected == "restore":
            if not arguments.confirm:
                raise RuntimeError("restore requires --confirm because it replaces live data")
            manifest = restore_backup(arguments.source, DB_PATH, UPLOAD_DIR)
        else:  # argparse owns the available choices
            raise RuntimeError(f"unknown command: {selected}")
        print(json.dumps({"ok": True, "format": manifest["format"],
                          "created_at": manifest["created_at"],
                          "schema_version": manifest["schema_version"]}, sort_keys=True))
        return 0
    except (OSError, RuntimeError) as failure:
        print(f"campfire: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

