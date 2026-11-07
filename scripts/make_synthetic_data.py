#!/usr/bin/env python3
"""Generate a synthetic data directory: training.db, gym.md, coaching-profile.md.

Usage:
    python scripts/make_synthetic_data.py --out DIR [--end YYYY-MM-DD|today]
                                          [--seed N] [--with-injection]
                                          [--example NAME]

Everything written is fabricated. Same seed and end date give identical rows.
--with-injection adds records whose text fields carry prompt-injection
payloads, for the injection eval and tests.
The profile and gym files are copied from examples/NAME (default home-3day,
the example whose routine the synthetic sessions follow).
Point COACH_DATA_DIR at the output directory to run the agent, tools and evals
against it.
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strength_coach import synthetic  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True,
                    help="directory to write (a throwaway dir, e.g. ./demo-data)")
    ap.add_argument("--end", default=synthetic.DEFAULT_END,
                    help="last date of data, or 'today'")
    ap.add_argument("--seed", type=int, default=synthetic.DEFAULT_SEED)
    ap.add_argument("--with-injection", action="store_true",
                    help="add records carrying prompt-injection payloads")
    ap.add_argument("--example", default="home-3day",
                    help="examples/NAME to copy the profile and gym from")
    args = ap.parse_args()
    example_dir = os.path.join(ROOT, "examples", args.example)
    if not os.path.isdir(example_dir):
        ap.error(f"no example {args.example!r} under examples/")

    end = synthetic.today_iso() if args.end == "today" else args.end
    os.makedirs(args.out, exist_ok=True)
    summary = synthetic.build(
        os.path.join(args.out, "training.db"), end=end, seed=args.seed,
        inject=args.with_injection)
    for name in ("gym.md", "coaching-profile.md"):
        shutil.copyfile(os.path.join(example_dir, name),
                        os.path.join(args.out, name))
    print(f"wrote synthetic data to {args.out}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
