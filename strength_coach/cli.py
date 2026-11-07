"""`bin/coach`: set up, validate and inspect an instance.

    bin/coach init [--dir PATH] [--example NAME]   scaffold a data directory
    bin/coach check [--dir PATH]                   validate it, with fixes
    bin/coach context [--dir PATH]                 print what the coach receives
    bin/coach names [--dir PATH | --db FILE]       raw exercise names, for aliases.yaml

None of these call a model or touch the network.
"""
import argparse
import difflib
import os
import shutil
import sys

from . import aliases
from . import config
from . import context
from . import db as scdb
from .canonical import normalize

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
TEMPLATES = os.path.join(PKG, "templates")
EXAMPLES = os.path.join(REPO, "examples")
DEFAULT_INIT_DIR = "~/.local/share/fitness-coach"


def _examples() -> list:
    if not os.path.isdir(EXAMPLES):
        return []
    return sorted(d for d in os.listdir(EXAMPLES)
                  if os.path.isfile(os.path.join(EXAMPLES, d, config.PROFILE)))


def _inside_repo(path: str) -> bool:
    """True if `path` is inside this git checkout. Instance data there is one
    `git add -A` away from being published, so init refuses it."""
    if not os.path.exists(os.path.join(REPO, ".git")):
        return False
    real, repo = os.path.realpath(path), os.path.realpath(REPO)
    return real == repo or real.startswith(repo + os.sep)


def cmd_init(a) -> int:
    target = os.path.abspath(os.path.expanduser(
        a.dir or os.environ.get("COACH_DATA_DIR") or DEFAULT_INIT_DIR))
    if _inside_repo(target):
        print(f"error: {target} is inside the repository checkout. Instance data "
              "must live outside it so it can never be committed. Pass --dir "
              f"with a path outside {REPO}.", file=sys.stderr)
        return 2
    if a.example:
        if a.example not in _examples():
            print(f"error: no example named {a.example!r}. Available: "
                  f"{', '.join(_examples()) or 'none'}.", file=sys.stderr)
            return 2
        src = {n: os.path.join(EXAMPLES, a.example, n) for n in (config.PROFILE, config.GYM)}
    else:
        src = {n: os.path.join(TEMPLATES, n) for n in (config.PROFILE, config.GYM)}
    src[aliases.FILENAME] = os.path.join(TEMPLATES, aliases.FILENAME)

    os.makedirs(target, mode=0o700, exist_ok=True)
    for name, path in src.items():
        dest = os.path.join(target, name)
        if os.path.exists(dest):
            print(f"kept     {dest} (exists, not overwritten)")
            continue
        shutil.copyfile(path, dest)
        print(f"created  {dest}")
    print(f"\nNext:\n  export COACH_DATA_DIR={target}\n"
          f"  edit the files above (every 'REPLACE:' line)\n"
          f"  bin/coach check")
    return 0


def _resolve(a) -> str:
    return config.data_dir(a.dir)


def cmd_check(a) -> int:
    d = _resolve(a)
    rep = config.check(d)
    model = os.environ.get("COACH_MODEL", config.DEFAULT_MODEL)
    for p in config.model_problems(model):
        rep.warnings.append(p + " (needed only to run the agent; tools and "
                            "`bin/coach context` work without it)")
    print(f"data directory: {d}")
    for line in rep.lines():
        print(line)
    print("OK" if rep.ok else "FAILED")
    return 0 if rep.ok else 1


def cmd_context(a) -> int:
    d = config.require(a.dir)
    sys.stdout.write(context.as_sent(d) + "\n")
    return 0


def name_report(conn, cutoff: float = 0.75) -> dict:
    """Every canonical exercise with the raw names each app exported for it,
    plus canonical pairs similar enough that they may be one lift. Raw names
    matter because the defaults already fold some variants together; listing
    only canonical names would hide exactly what aliases.yaml is for."""
    rows = conn.execute(
        """SELECT e.canonical_name AS canonical, a.source AS source, a.alias AS raw,
                  (SELECT COUNT(*) FROM sets s WHERE s.exercise_id = e.id) AS sets
           FROM exercises e LEFT JOIN exercise_aliases a
             ON a.exercise_id = e.id AND a.source != '*'
           ORDER BY e.canonical_name, a.source, a.alias""").fetchall()
    out: dict = {}
    for r in rows:
        entry = out.setdefault(r["canonical"], {"sets": r["sets"], "raw": []})
        if r["raw"]:
            entry["raw"].append((r["source"], r["raw"]))
    names = sorted(n for n, e in out.items() if e["sets"])
    norm = {n: normalize(n) for n in names}
    similar = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ta, tb = set(norm[a].split()), set(norm[b].split())
            ratio = difflib.SequenceMatcher(None, norm[a], norm[b]).ratio()
            if ratio >= cutoff or (ta and tb and (ta <= tb or tb <= ta)):
                similar.append((a, b))
    return {"exercises": out, "similar": similar}


def cmd_names(a) -> int:
    if a.db:
        path = a.db
    else:
        path = os.path.join(config.data_dir(a.dir), config.DB_FILENAME)
    if not os.path.exists(path):
        print(f"error: no store at {path}. Import an export first "
              "(python scripts/backfill.py --db PATH FILE).", file=sys.stderr)
        return 2
    conn = scdb.connect(path, read_only=True)
    try:
        rep = name_report(conn)
    finally:
        conn.close()
    print("Exercises, with the raw name each app exported:")
    for name, e in sorted(rep["exercises"].items()):
        if not e["sets"]:
            continue
        print(f"  {name}  ({e['sets']} sets)")
        for source, raw in e["raw"]:
            if raw != name:
                print(f"      {source}: {raw}")
    print("\nPossibly the same lift (add `Name: Canonical` to aliases.yaml only if they are):")
    for x, y in rep["similar"] or []:
        print(f"  {x}  ~  {y}")
    if not rep["similar"]:
        print("  none found")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="coach", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="scaffold a data directory (never overwrites)")
    p.add_argument("--dir", help=f"target (default: $COACH_DATA_DIR, else {DEFAULT_INIT_DIR})")
    p.add_argument("--example", help="start from examples/NAME instead of blank templates")
    p.set_defaults(fn=cmd_init)
    for name, fn, text in (
            ("check", cmd_check, "validate the data directory"),
            ("context", cmd_context, "print the get_context bundle exactly as the model receives it")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--dir", help="data directory (default: $COACH_DATA_DIR)")
        p.set_defaults(fn=fn)
    p = sub.add_parser("names", help="list raw exercise names per app and likely duplicates")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dir", help="data directory (default: $COACH_DATA_DIR)")
    g.add_argument("--db", help="a store file, e.g. a throwaway probe import")
    p.set_defaults(fn=cmd_names)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except config.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
