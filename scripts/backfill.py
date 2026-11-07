#!/usr/bin/env python3
"""Backfill the training store from one or more export files.

Usage:
    python scripts/backfill.py --db PATH [--reset] FILE [FILE ...]

Applies the default aliases plus $COACH_DATA_DIR/aliases.yaml (or the
aliases.yaml next to --db when COACH_DATA_DIR is unset), then ingests each file (source auto-
detected). Reports per-source counts. File paths are passed in, never
hardcoded, so no host-specific paths live in the repo.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strength_coach import aliases, db  # noqa: E402
from strength_coach.adapters import detect  # noqa: E402
from strength_coach.canonical import Canonicalizer, seed_aliases  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--reset", action="store_true", help="delete the db first")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()

    if args.reset and os.path.exists(args.db):
        os.remove(args.db)

    data_dir = os.environ.get("COACH_DATA_DIR") or os.path.dirname(os.path.abspath(args.db))
    try:
        mapping = aliases.load(data_dir)
    except aliases.AliasFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    conn = db.connect(args.db)
    db.init_schema(conn)
    seed_aliases(conn, mapping)

    canon = Canonicalizer(conn)
    for path in args.files:
        parsed = detect.parse_file(path)
        summary = db.ingest(conn, parsed, canon.resolve)
        print(f"{summary['source']:12s} sessions={summary['sessions']:5d} "
              f"sets={summary['sets']:6d}  ({os.path.basename(path)})")

    if canon.newly_created:
        print(f"\n{len(canon.newly_created)} new canonical exercises created.")
    if canon.suggestions:
        print("Near-match merge suggestions:")
        for new, existing in canon.suggestions.items():
            print(f"  '{new}'  ~  '{existing}'")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
