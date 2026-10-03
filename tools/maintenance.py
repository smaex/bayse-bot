#!/usr/bin/env python3
"""Operator CLI for database hygiene. Dry-run unless ``--apply`` is passed.

    python tools/maintenance.py            # show what is wrong
    python tools/maintenance.py --apply    # fix it
    python tools/maintenance.py --purge-settings

Nothing here is required for the bot to run; it exists so an operator can see
the state of the record set before it starts corrupting decisions.
"""

from __future__ import annotations

import argparse
import logging
import sys

import config
import database
import maintenance as maint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually perform the repairs (default: report only)")
    parser.add_argument("--purge-settings", action="store_true",
                        help="drop references to strategies that no longer exist")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if not database.check_connection():
        print("DATABASE_URL is not reachable — nothing to check.")
        return 1

    database.init_db()
    report = maint.run(apply=args.apply)
    print(report.text())

    if args.purge_settings:
        changed = maint.purge_stale_settings()
        print(
            f"Purged stale strategy references from {changed} account(s). "
            f"Live strategies: {', '.join(config.ACTIVE_STRATEGIES)}"
        )
    elif not args.apply:
        print("\nNo changes were made. Re-run with --apply to perform them.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
