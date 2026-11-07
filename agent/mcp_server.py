"""MCP server exposing the coaching tools over stdio.

Spawned by coach.py as a subprocess. Every tool wraps a function in the
`strength_coach` core package; no analytics logic lives here.

--- THE PATTERN (read this before adding a tool) ---
1. Pick the library function in `strength_coach/analytics.py` (or `db.py` /
   `canonical.py`) that does the work. Never reimplement logic here.
2. Add a `@mcp.tool()` function below:
     - name = the tool name the model sees (see docs/TOOLS.md).
     - args = whatever the library function needs beyond `conn`, typed, with
       defaults matching the library function's defaults.
     - docstring = becomes the tool description the model sees.
     - body (read tools) = call `_read_json(lambda c: ...)` and return it.
       body (write tools) = open `_rw()`, call the library function, return
       a short prose confirmation, close the connection in `finally`, and
       catch any exception into a readable error string instead of raising.
3. Read tools only ever open `_ro()` (read-only URI, physically
   non-writable). Write tools open `_rw()` (read-write plus schema init),
   which needs the data directory to be writable.
4. Everything a read tool returns is wrapped by `_wrap` (see "Tool output is
   data" below). Destructive write tools go through the preview/apply gate in
   `strength_coach/confirm.py`; see `_gated`.
5. Never raise out of a tool. A raised exception surfaces as a noisy
   MCP-protocol error instead of a turn the model can recover from. Read
   tools return `{"error": ...}` as JSON via `_read_json`; write tools
   return a plain-text error string.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# The core package lives one directory up from agent/. COACH_CORE_DIR
# overrides that for deployments that mount the package elsewhere.
sys.path.insert(
    0, os.environ.get("COACH_CORE_DIR", str(Path(__file__).resolve().parent.parent)))
from strength_coach import analytics  # noqa: E402
from strength_coach import config as scconfig  # noqa: E402
from strength_coach import context  # noqa: E402
from strength_coach import db as scdb  # noqa: E402
from strength_coach.canonical import merge_exercise, plan_merge_exercise  # noqa: E402
from strength_coach.confirm import ConfirmationGate  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

# Resolved for real in main(); tests rebind these per server instance.
DATA_DIR = os.environ.get("COACH_DATA_DIR", "")
DB_PATH = os.path.join(DATA_DIR, "training.db")

AUDIT_PATH = os.environ.get("COACH_AUDIT_LOG", os.path.join(DATA_DIR, "audit.jsonl"))

mcp = FastMCP("fitness-coach-tools")

# Server-side token store for destructive tools. Lives in this process only:
# restarting the server invalidates every outstanding token.
GATE = ConfirmationGate()

# --- Tool output is data -----------------------------------------------------
# The envelope, the read-only connection and the grounding bundle live in
# strength_coach/context.py so `bin/coach context` prints exactly what these
# tools return. Module-level names stay here so tests can rebind DATA_DIR,
# DB_PATH and _rw per server instance.
DATA_NOTICE = context.DATA_NOTICE
_wrap = context.wrap


def _audit(tool: str, phase: str, args: dict) -> None:
    """Append one line per destructive-tool call (preview, applied, rejected).
    Best effort: an unwritable log never blocks or fails a tool."""
    try:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "tool": tool, "phase": phase, "args": args}) + "\n")
    except OSError:
        pass


def _ro() -> sqlite3.Connection:
    return context.ro_connect(DB_PATH)


def _rw() -> sqlite3.Connection:
    """Read-write connection for write tools. Opens the DB file directly
    (not the read-only URI) and ensures the schema exists. Fails if the
    underlying file is not writable (for example a read-only bind mount)."""
    conn = scdb.connect(DB_PATH)
    scdb.init_schema(conn)
    return conn


def _json_raw(fn) -> str:
    return context.json_raw(_ro, fn)


def _read_json(fn) -> str:
    return _wrap(_json_raw(fn))


def _read_data_file(filename: str, label: str) -> str:
    return context.read_data_file(DATA_DIR, filename, label)


@mcp.tool()
def get_training_consistency() -> str:
    "Consistency metrics -- the #1 KPI. Days since last, current/longest weekly streak (away-bridged), days trained in last 28, week-by-week. Strength sessions only."
    return _read_json(analytics.training_consistency)


@mcp.tool()
def get_training_overview(window_days: int = 180) -> str:
    """A complete "how am I doing / zoom out / talk trends and gaps" answer
    in ONE call. Returns consistency, per-lift direction over the window (with
    best weight / est. 1RM / reps in that window), and the largest training
    gaps. Use this for broad "how am I progressing", "zoom out N days", or
    "what should I focus on" questions instead of chaining many tool calls.
    `window_days` defaults to 180. Dual-implement lifts carry a caveat -- don't
    read their weight change as a real strength change."""
    return _read_json(lambda c: analytics.training_overview(c, window_days))


@mcp.tool()
def get_exercise_summary(
    exercise: str, start_date: str = None, end_date: str = None
) -> str:
    """Best literal weight lifted AND best estimated 1RM (RIR-adjusted, a
    formula not an actual max) for one exercise, optionally within a
    YYYY-MM-DD date window. Present both numbers distinctly to the user."""
    return _read_json(
        lambda c: analytics.exercise_summary(c, exercise, start_date, end_date))


@mcp.tool()
def compare_exercise_periods(
    exercise: str, period_a_start: str, period_a_end: str,
    period_b_start: str, period_b_end: str,
) -> str:
    """Compare one exercise across two YYYY-MM-DD date windows (e.g. 2019
    vs 2026). Returns literal and estimated-1RM bests for each period."""
    return _read_json(lambda c: analytics.compare_periods(
        c, exercise, (period_a_start, period_a_end),
        (period_b_start, period_b_end)))


@mcp.tool()
def get_exercise_trend(exercise: str) -> str:
    """Overall direction (increasing/stable/decreasing) of an exercise's
    best-per-session estimated 1RM across its whole history."""
    return _read_json(lambda c: analytics.trend(c, exercise))


@mcp.tool()
def get_cardio_summary(window_days: int = 90) -> str:
    """Non-strength activity (walks/runs/hikes/bike/other cardio, from a
    wearable) over a trailing window -- session count, distance (miles),
    elevation gain (feet), calories, and duration per activity type plus a
    combined total and the most recent activity date. Aggregate only; for a
    single best session use get_activity_records. `window_days` defaults to 90."""
    return _read_json(lambda c: analytics.cardio_summary(c, window_days))


@mcp.tool()
def get_activity_records(
    metric: str = "distance", activity_type: str = None,
    window_days: int = None, limit: int = 5,
) -> str:
    """Best individual (single-session) cardio activities, ranked by `metric`:
    "distance" (miles), "duration" (minutes), or "elevation" (feet). Use this
    for any "what was my longest / biggest / most ___" question -- it returns
    the actual sessions, unlike get_cardio_summary which only aggregates.
    `activity_type` filters to one bucket (e.g. "run", "hike", "walk"); omit
    for all cardio. `window_days` omitted = all-time. Each record includes
    distance/elevation/duration/HR so the top result comes with full context."""
    types = [activity_type] if activity_type else None
    return _read_json(lambda c: analytics.activity_records(
        c, metric=metric, activity_types=types,
        window_days=window_days, limit=limit))


@mcp.tool()
def get_recovery_status(window_days: int = 14) -> str:
    """Recent recovery signals (sleep, resting heart rate, HRV) from a
    wearable -- latest value plus a trailing-window average per metric. No
    synthetic readiness score (the user doesn't want one) -- use latest-vs-average
    and the trend, not a single day, to inform a hard-session or deload
    call. `window_days` defaults to 14."""
    return _read_json(lambda c: analytics.recovery_status(c, window_days))


@mcp.tool()
def get_recent_sets(
    exercise: str = None, start_date: str = None,
    end_date: str = None, limit: int = 200,
) -> str:
    """Raw working sets (date, exercise, weight, reps, RIR, source),
    optionally filtered by exercise and/or YYYY-MM-DD window. Use to reason
    over the actual sets when a summary isn't enough. Warmups excluded."""
    return _read_json(
        # most recent, not oldest
        lambda c: analytics.fetch_sets(c, exercise, start_date, end_date)[-limit:])


@mcp.tool()
def get_recent_workouts(count: int = 12) -> str:
    """The last `count` logged workouts IN FULL -- each session's date, workout
    name, and per-exercise working sets (weight/reps/RIR) -- in one call. Use
    this whenever the user wants to review a recent training block: "look at my
    last N workouts", "are my accessories optimized", "what should I change",
    "any low-hanging fruit", program/exercise-selection critique. Pair it with
    get_context (goals + gym) so your advice fits the user. `count` defaults to 12."""
    return _read_json(lambda c: analytics.recent_workouts(c, count))


@mcp.tool()
def list_exercises() -> str:
    """List every tracked exercise with its working-set count and date
    range. Use to see what data exists before answering questions."""
    return _read_json(analytics.list_tracked_exercises)


@mcp.tool()
def list_imports() -> str:
    """List every import batch (id, source, filename, date, counts), newest
    first. Call this to find the import id to pass to remove_import."""
    return _read_json(analytics.list_imports)


@mcp.tool()
def get_program_changes() -> str:
    """Structural changes between the two most recent MacroFactor program
    snapshots: workouts and exercises added or removed."""
    return _read_json(analytics.program_diff)


@mcp.tool()
def get_gym_equipment() -> str:
    """The user's gym configuration -- available equipment and practical load
    ceilings (for example a dumbbell rack that tops out at a given weight).
    Call this before recommending exercises or programming so suggestions fit
    what they actually have."""
    return _wrap(_read_data_file("gym.md", "gym configuration"))


def _profile_text() -> str:
    return context.profile_text(DATA_DIR, _ro)


@mcp.tool()
def get_coaching_profile() -> str:
    """The user's coaching profile plus the running coaching notes -- who they are,
    what they are optimizing for, and everything they have told you to remember about
    how to coach them. Call this at the start of a coaching conversation to
    ground your advice and honor their standing preferences."""
    return _wrap(_profile_text())


@mcp.tool()
def get_context() -> str:
    """Everything needed to ground a substantive coaching conversation, in
    one call: coaching profile + notes, gym equipment, current consistency
    (strength-only AND all-activity), and recent program changes. Call this
    ONCE at the start of a real coaching question (not a quick lookup)
    instead of calling the individual context tools separately."""
    return _wrap(context.bundle(DATA_DIR, _ro))


@mcp.tool()
def set_away_period(start_date: str, end_date: str, reason: str = "") -> str:
    """Record a planned/known away period (vacation, travel, illness) so the
    consistency metric doesn't penalize the gap -- those weeks are excluded
    and the streak bridges across them. Dates are ISO YYYY-MM-DD. Call this
    when the user mentions upcoming or past time away from training."""
    try:
        conn = _rw()
        try:
            pid = scdb.add_away_period(conn, start_date, end_date, reason)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return f"Could not log away period: {e}"
    return (f"Logged away period #{pid}: {start_date} to {end_date}"
            f"{f' ({reason})' if reason else ''}. It won't count against "
            "your consistency.")


@mcp.tool()
def remove_away_period(period_id: int) -> str:
    """Remove a previously logged away period by its id (see the away_periods
    list in get_training_consistency)."""
    try:
        conn = _rw()
        try:
            n = scdb.remove_away_period(conn, period_id)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return f"Could not remove away period: {e}"
    return (f"Removed away period {period_id}." if n
            else f"No away period {period_id} found.")


@mcp.tool()
def add_coaching_note(note: str, kind: str = "") -> str:
    """Remember something across conversations: how the user wants to be coached
    (e.g. 'keep readouts short', 'more detail on one lift'), feedback on your
    analysis, or durable context (a goal, an event to peak for). Record it
    whenever they state a preference or share something worth remembering.
    `kind` is a free label: preference / feedback / goal / context."""
    try:
        conn = _rw()
        try:
            nid = scdb.add_coaching_note(conn, note, kind)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return f"Could not save coaching note: {e}"
    return f"Noted (#{nid}). I'll remember that going forward."


@mcp.tool()
def remove_coaching_note(note_id: int) -> str:
    """Forget a coaching note by its id (shown in the coaching-notes list in
    get_coaching_profile). Use when a preference or context no longer holds."""
    try:
        conn = _rw()
        try:
            n = scdb.remove_coaching_note(conn, note_id)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return f"Could not remove note: {e}"
    return (f"Removed note {note_id}." if n else f"No note {note_id} found.")


def _gated(tool: str, args: dict, plan_fn, apply_fn, confirm_token: str) -> str:
    """Preview/apply gate shared by the destructive tools.

    No token: open the DB READ-ONLY, describe exactly what would change, issue
    a token, write nothing. With a token: re-plan against current data on a
    read-only connection, check the token (known, unexpired, unused, same tool
    and arguments, same row ids), and only then open a writable connection. Every rejection
    returns before any write connection exists."""
    if not confirm_token:
        try:
            conn = _ro()
            try:
                plan = plan_fn(conn)
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            _audit(tool, "preview_error", args)
            return f"Could not preview {tool}: {e}"
        ids = _plan_ids(plan)
        issued = GATE.issue(tool, args, ids)
        _audit(tool, "preview", args)
        out = {
            "status": "preview_only_nothing_changed",
            **issued,
            "next_step": (
                "Show the user this preview, including the affected row ids "
                "and counts, and ask for an explicit yes. Only after they "
                f"say yes, call {tool} again with the same arguments and "
                "confirm_token set to the value above."),
            "preview": plan,
        }
        return _wrap(json.dumps(out))
    # Apply step. Validate on a READ-ONLY connection first, so every rejection
    # returns before any write connection exists (opening one can itself
    # write: WAL pragma, schema init).
    try:
        conn = _ro()
        try:
            plan = plan_fn(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        # The target no longer exists or cannot be planned. Burn the token and
        # report why it is unusable; nothing was written.
        why = GATE.consume(tool, args, {}, confirm_token)
        _audit(tool, "rejected", args)
        return f"Refused, nothing changed: {why or e}."
    ids = _plan_ids(plan)
    why = GATE.consume(tool, args, ids, confirm_token)
    if why:
        _audit(tool, "rejected", args)
        return f"Refused, nothing changed: {why}."
    try:
        conn = _rw()
        try:
            # Re-plan on the write connection: refuse if rows moved in the
            # gap between validation and the write.
            if _plan_ids(plan_fn(conn)) != ids:
                _audit(tool, "rejected", args)
                return ("Refused, nothing changed: stored data changed "
                        "during apply; request a new preview.")
            result = apply_fn(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        _audit(tool, "apply_error", args)
        return f"Could not run {tool}: {e}"
    _audit(tool, "applied", args)
    return result


def _plan_ids(plan: dict) -> dict:
    """The row-id part of a plan: what the token fingerprints."""
    return {k: v for k, v in plan.items()
            if k.endswith("ids") or k in ("from", "into", "import", "aliases")}


@mcp.tool()
def merge_exercises(from_name: str, into_name: str, confirm_token: str = "") -> str:
    """DESTRUCTIVE, two steps. Merge one canonical exercise into another
    (e.g. combine a duplicate name): repoints all its sets and deletes the
    now-empty exercise. Call first WITHOUT confirm_token: nothing changes and
    you get a preview (row ids) plus a one-time token. Show the user the
    preview and get an explicit yes, then call again with the same arguments
    and confirm_token. The token expires and works once."""
    args = {"from_name": from_name, "into_name": into_name}

    def apply(conn):
        res = merge_exercise(conn, from_name, into_name)
        return (f"Merged '{res['merged']}' into '{res['into']}' "
                f"({res['sets_repointed']} sets repointed).")

    return _gated("merge_exercises", args,
                  lambda c: plan_merge_exercise(c, from_name, into_name),
                  apply, confirm_token)


@mcp.tool()
def remove_import(import_id: int, confirm_token: str = "") -> str:
    """DESTRUCTIVE, two steps. Remove a previously imported batch by its
    import id (undo a bad import); find the id with list_imports. Only deletes
    sessions that import still owns; sessions a later import re-touched are
    kept. Call first WITHOUT confirm_token: nothing changes and you get a
    preview (row ids) plus a one-time token. Show the user the preview and get
    an explicit yes, then call again with the same arguments and
    confirm_token. The token expires and works once."""
    args = {"import_id": import_id}

    def apply(conn):
        res = scdb.remove_import(conn, import_id)
        return (f"Removed import {import_id}: "
                f"{res['sessions_removed']} sessions deleted.")

    return _gated("remove_import", args,
                  lambda c: scdb.plan_remove_import(c, import_id),
                  apply, confirm_token)


# --- Not exposed as tools: imports and wearable sync ---
# Importing export files is deliberately NOT a model-callable tool. Files
# land in a watched folder and a standalone deterministic process ingests them
# (importer/watcher.py, scripts/backfill.py), so no model output ever sits in
# the write path for training data.
#
# The destructive tools (remove_import, merge_exercises) are gated in code:
# a preview call issues a server-side token and a second call must present it
# (see _gated and strength_coach/confirm.py). The code guarantees a preview
# happens and that apply matches it. That the user actually said yes is still
# a persona-level instruction, because the agent relays the answer.

def main() -> int:
    global DATA_DIR, DB_PATH, AUDIT_PATH
    try:
        DATA_DIR = scconfig.require()
    except scconfig.ConfigError as e:
        print(f"fitness-coach-tools: {e}", file=sys.stderr)
        return 2
    DB_PATH = os.path.join(DATA_DIR, scconfig.DB_FILENAME)
    AUDIT_PATH = os.environ.get("COACH_AUDIT_LOG", os.path.join(DATA_DIR, "audit.jsonl"))
    mcp.run()  # stdio transport
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
