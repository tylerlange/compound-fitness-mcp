# AGENTS.md

Rules for an agent changing this code (humans: README.md).

## Command

- Run `make check` before you call any change done. It runs ruff (pinned to
  0.8.2), the offline test suite including the eval checkers, and the
  identifier scan. Install its deps with `pip install -r requirements-dev.txt`.
- Do not "fix" lint by upgrading ruff; newer versions enable rules this repo
  has not adopted.
- After a persona or tool-docstring change, run `python evals/run.py` if a
  model key and running agent are available; otherwise say it was not run.

## Invariants (each exists for a reason; keep it)

- No LLM in the importer or write path (`importer/`, `scripts/backfill.py`,
  adapters, `db.ingest`). Training data must be reproducible from exports;
  a model-parsed row is unauditable.
- Destructive tools (`remove_import`, `merge_exercises`) stay two-step in code:
  preview returns a server-side token, apply must present it. Do not add a
  flag, default or tool that skips the preview. The persona's "ask first" is
  not a control; the gate is.
- Read tools open the store `mode=ro` (`_read_json`); file-only read tools
  return through `_wrap`. Never `_rw()` in a read tool.
- Tool output is untrusted data: exercise names, workout labels, filenames and
  profile text come from user files. Keep every read tool's output inside the
  `untrusted_data` envelope, and never build a path, query or command from it.
- Persona behavior rules (no invented numbers, abbreviation handling,
  dual-implement caveat, no readiness score, confirm before destructive calls)
  are checked by `evals/run.py`. Change the persona and the eval together;
  never loosen a checker to make a failing eval pass.
- `bin/coach context` and the `get_context` tool both build from
  `strength_coach/context.py`. Keep one code path, or the inspection command
  lies.
- `DEFAULT_ALIASES` holds only unambiguous naming variants. Merging different
  variations is a per-user decision and goes in the user's `aliases.yaml`.
- A malformed `aliases.yaml` must raise, never be skipped: a dropped alias
  silently splits one lift into two trends.
- Derived numbers (e1RM, trends, consistency) are computed at query time.
  Do not store them.
- Deterministic data only in tests: `strength_coach/synthetic.py` with a fixed
  seed. No network, no model key.

## Never

- Never commit instance data: anything from a `COACH_DATA_DIR`, `.env`, a
  `*.db`, `demo-data/`, or `.identifiers.local`. Tests and fixtures are
  synthetic and obviously fictional.
- Never add a real person's details, gear, routine, dates or accounts to code,
  tests, examples or docs. Examples stay fictional and generic.
- Never push without the scan passing: `python scripts/scan_identifiers.py`
  (also run by `make check` and the pre-push hook from `make hooks`).
- Never put a secret in code, a fixture, a log line or `.env.example`.
- REVIEW: Never add a dependency to `strength_coach/` without a stated reason;
  the core package depends only on openpyxl.

Adapter contract: docs/ADAPTERS.md. Decisions and reasons: docs/ARCHITECTURE.md.
