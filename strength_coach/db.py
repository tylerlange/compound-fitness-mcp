"""SQLite storage layer.

Dedup strategy is session-level replace: a session is identified by
``(source, date, workout)``. Re-importing an overlapping window (or a file
where a past workout was edited in the source app) replaces that session's sets
wholesale from the new parse. That makes imports idempotent and lets
corrections propagate, without needing a fragile per-set natural key.

``source_import_id`` records which import last touched a session, so
``remove_import`` can delete exactly the sessions an import currently owns.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

from .models import NormalizedSession, ParsedImport

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    filename TEXT,
    imported_at TEXT NOT NULL,
    session_count INTEGER DEFAULT 0,
    set_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exercises (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS exercise_aliases (
    alias TEXT NOT NULL,
    source TEXT NOT NULL,
    exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    PRIMARY KEY (alias, source)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    activity_type TEXT NOT NULL DEFAULT 'strength',
    source TEXT NOT NULL,
    workout TEXT NOT NULL DEFAULT '',
    source_session_key TEXT NOT NULL DEFAULT '',
    duration_s INTEGER,
    notes TEXT,
    confidence TEXT NOT NULL DEFAULT 'high',
    source_import_id INTEGER REFERENCES imports(id),
    created_at TEXT NOT NULL,
    UNIQUE (source, date, workout, source_session_key)
);

CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    exercise_id INTEGER REFERENCES exercises(id),
    set_type TEXT NOT NULL DEFAULT 'standard',
    weight_lb REAL,
    reps INTEGER,
    rir REAL,
    seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session_metrics (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    value REAL,
    unit TEXT
);

CREATE TABLE IF NOT EXISTS session_streams (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    metric TEXT NOT NULL,
    unit TEXT,
    sample_seconds INTEGER,
    offsets_json TEXT NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY (session_id, metric)
);

CREATE TABLE IF NOT EXISTS program_snapshots (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    structure_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS away_periods (
    id INTEGER PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coaching_notes (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    unit TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(date, source, metric)
);

CREATE INDEX IF NOT EXISTS idx_sets_session ON sets(session_id);
CREATE INDEX IF NOT EXISTS idx_sets_exercise ON sets(exercise_id);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON daily_metrics(date);
"""

# Resolver: (raw_exercise_name, source) -> exercise_id. Supplied by canonical.py.
ExerciseResolver = Callable[[str, str], int]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with WAL + foreign keys on. read_only opens via URI so
    the query path physically cannot write."""
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Writers include the agent's write tools AND the standalone importer
    # process, so concurrent writes to the same file are realistic. Wait on a held lock instead of erroring out
    # immediately (Python's implicit 5s default is short for a full poll cycle).
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        # v1 -> v2 added session_streams and nothing else; the executescript
        # above is the whole migration, so just record the new version.
        conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def record_import(conn: sqlite3.Connection, source: str, filename: str) -> int:
    cur = conn.execute(
        "INSERT INTO imports (source, filename, imported_at) VALUES (?, ?, ?)",
        (source, filename, _utcnow()),
    )
    return cur.lastrowid


def _upsert_session(
    conn: sqlite3.Connection,
    session: NormalizedSession,
    resolver: ExerciseResolver,
    import_id: int,
) -> dict:
    """Insert or replace one session and its sets. Returns a small detail dict:
    ``{sets, is_new, date, workout}``. ``is_new`` distinguishes a session this
    import created from one it merely re-confirmed (already present, replaced in
    place) — so the caller can report "1 new, 2 already logged" instead of a
    misleading raw file count."""
    existing = conn.execute(
        """SELECT id FROM sessions
           WHERE source=? AND date=? AND workout=? AND source_session_key=?""",
        (session.source, session.date, session.workout, session.source_session_key),
    ).fetchone()
    is_new = existing is None

    if existing:
        session_id = existing["id"]
        # Replace children; new parse is authoritative for this session.
        conn.execute("DELETE FROM sets WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM session_metrics WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM session_streams WHERE session_id=?", (session_id,))
        conn.execute(
            "UPDATE sessions SET duration_s=?, notes=?, source_import_id=? WHERE id=?",
            (session.duration_s, session.notes, import_id, session_id),
        )
    else:
        cur = conn.execute(
            """INSERT INTO sessions
               (date, activity_type, source, workout, source_session_key,
                duration_s, notes, source_import_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.date,
                session.activity_type,
                session.source,
                session.workout,
                session.source_session_key,
                session.duration_s,
                session.notes,
                import_id,
                _utcnow(),
            ),
        )
        session_id = cur.lastrowid

    for s in session.sets:
        exercise_id = resolver(s.exercise_raw, session.source)
        conn.execute(
            """INSERT INTO sets
               (session_id, exercise_id, set_type, weight_lb, reps, rir, seq)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, exercise_id, s.set_type, s.weight_lb, s.reps, s.rir, s.seq),
        )
    for metric, value, unit in session.metrics:
        conn.execute(
            "INSERT INTO session_metrics (session_id, metric, value, unit) VALUES (?, ?, ?, ?)",
            (session_id, metric, value, unit),
        )
    for metric, unit, sample_s, offsets, values in session.streams:
        conn.execute(
            """INSERT INTO session_streams
               (session_id, metric, unit, sample_seconds, offsets_json, values_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, metric, unit, sample_s,
             json.dumps(offsets), json.dumps(values)),
        )
    return {
        "sets": len(session.sets),
        "is_new": is_new,
        "date": session.date,
        "workout": session.workout,
    }


def ingest(
    conn: sqlite3.Connection, parsed: ParsedImport, resolver: ExerciseResolver
) -> dict:
    """Ingest a parsed import. Returns a summary dict that separates genuinely
    new sessions from ones already in the store (re-confirmed in place), since a
    rolling export (e.g. MacroFactor's) carries recent sessions on every pull."""
    import_id = record_import(conn, parsed.source, parsed.filename)
    set_total = 0
    new_sessions: list[dict] = []
    updated_sessions: list[dict] = []
    for session in parsed.sessions:
        res = _upsert_session(conn, session, resolver, import_id)
        set_total += res["sets"]
        entry = {"date": res["date"], "workout": res["workout"]}
        (new_sessions if res["is_new"] else updated_sessions).append(entry)

    if parsed.program_snapshot is not None:
        import json

        conn.execute(
            "INSERT INTO program_snapshots (source, imported_at, structure_json) VALUES (?, ?, ?)",
            (parsed.source, _utcnow(), json.dumps(parsed.program_snapshot)),
        )

    conn.execute(
        "UPDATE imports SET session_count=?, set_count=? WHERE id=?",
        (len(parsed.sessions), set_total, import_id),
    )
    conn.commit()
    # Fold the WAL back into the main db so read-only openers (the query tools)
    # aren't racing a large write-ahead log / missing -shm index right after an
    # import. Best-effort: a checkpoint failure must not fail the import.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
    return {
        "import_id": import_id,
        "source": parsed.source,
        "sessions": len(parsed.sessions),  # total in file (kept for compat)
        "sets": set_total,
        "new_count": len(new_sessions),
        "updated_count": len(updated_sessions),
        "new_sessions": new_sessions,
        "updated_sessions": updated_sessions,
    }


def add_away_period(
    conn: sqlite3.Connection, start_date: str, end_date: str, reason: str = ""
) -> int:
    """Record a planned/known away period (vacation, travel, illness) so the
    consistency metric doesn't penalize the gap. Dates are ISO YYYY-MM-DD."""
    cur = conn.execute(
        "INSERT INTO away_periods (start_date, end_date, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (start_date, end_date, reason, _utcnow()),
    )
    conn.commit()
    return cur.lastrowid


def remove_away_period(conn: sqlite3.Connection, period_id: int) -> int:
    n = conn.execute(
        "DELETE FROM away_periods WHERE id = ?", (period_id,)
    ).rowcount
    conn.commit()
    return n


def get_away_periods(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, start_date, end_date, reason FROM away_periods "
        "ORDER BY start_date")]


def add_coaching_note(
    conn: sqlite3.Connection, note: str, kind: str = ""
) -> int:
    """Persist something the coach should remember across conversations — a
    preference for how the user wants to be coached, feedback on the analysis, or
    durable context (a goal, an event to peak for). ``kind`` is a free label
    (e.g. preference / feedback / goal / context)."""
    cur = conn.execute(
        "INSERT INTO coaching_notes (created_at, kind, note) VALUES (?, ?, ?)",
        (_utcnow(), kind, note),
    )
    conn.commit()
    return cur.lastrowid


def get_coaching_notes(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, created_at, kind, note FROM coaching_notes "
        "ORDER BY id DESC LIMIT ?", (limit,))]


def remove_coaching_note(conn: sqlite3.Connection, note_id: int) -> int:
    n = conn.execute(
        "DELETE FROM coaching_notes WHERE id = ?", (note_id,)
    ).rowcount
    conn.commit()
    return n


def upsert_daily_metric(
    conn: sqlite3.Connection,
    date: str,
    source: str,
    metric: str,
    value: Optional[float],
    unit: Optional[str] = None,
) -> int:
    """Insert or update one daily recovery/wearable scalar (resting HR, HRV,
    sleep minutes, ...). Identity is ``(date, source, metric)`` -- a re-pull
    of the same day (the poller re-pulls a trailing window every run, see
    importer/wearable_poller.py) overwrites the value in place, so this is
    idempotent by construction with no separate dedup bookkeeping needed.
    Returns the row id."""
    conn.execute(
        """INSERT INTO daily_metrics (date, source, metric, value, unit, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, source, metric) DO UPDATE SET
             value=excluded.value, unit=excluded.unit""",
        (date, source, metric, value, unit, _utcnow()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM daily_metrics WHERE date=? AND source=? AND metric=?",
        (date, source, metric),
    ).fetchone()
    return row["id"]


def get_daily_metrics(
    conn: sqlite3.Connection, since: Optional[str] = None
) -> list[dict]:
    """All daily recovery/wearable metrics, optionally from an ISO date
    onward. Ordered by date then metric."""
    if since:
        rows = conn.execute(
            "SELECT id, date, source, metric, value, unit FROM daily_metrics "
            "WHERE date >= ? ORDER BY date, metric",
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, date, source, metric, value, unit FROM daily_metrics "
            "ORDER BY date, metric"
        ).fetchall()
    return [dict(r) for r in rows]


def plan_remove_import(conn: sqlite3.Connection, import_id: int) -> dict:
    """Read-only description of exactly what `remove_import` would delete, by
    row id. Never writes, so it is safe on a read-only connection. Raises
    ValueError if the import does not exist."""
    imp = conn.execute(
        "SELECT id, source, filename, imported_at FROM imports WHERE id=?",
        (import_id,)).fetchone()
    if imp is None:
        raise ValueError(f"no import with id {import_id}")
    sessions = conn.execute(
        "SELECT id, date, workout, source FROM sessions "
        "WHERE source_import_id=? ORDER BY id", (import_id,)).fetchall()
    session_ids = [r["id"] for r in sessions]
    set_ids: list[int] = []
    metric_rows = 0
    stream_rows = 0
    for sid in session_ids:
        set_ids += [r["id"] for r in conn.execute(
            "SELECT id FROM sets WHERE session_id=? ORDER BY id", (sid,))]
        metric_rows += conn.execute(
            "SELECT COUNT(*) FROM session_metrics WHERE session_id=?",
            (sid,)).fetchone()[0]
        stream_rows += conn.execute(
            "SELECT COUNT(*) FROM session_streams WHERE session_id=?",
            (sid,)).fetchone()[0]
    return {
        "action": "remove_import",
        "import": dict(imp),
        "session_ids": session_ids,
        "sessions": [dict(r) for r in sessions],
        "set_ids": set_ids,
        "session_metric_rows": metric_rows,
        "session_stream_rows": stream_rows,
    }


def remove_import(conn: sqlite3.Connection, import_id: int) -> dict:
    """Delete the sessions an import currently owns (last touched). Sessions a
    later import has since re-touched are left alone."""
    rows = conn.execute(
        "SELECT id FROM sessions WHERE source_import_id=?", (import_id,)
    ).fetchall()
    session_ids = [r["id"] for r in rows]
    for sid in session_ids:
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))  # cascades to sets/metrics
    conn.execute("DELETE FROM imports WHERE id=?", (import_id,))
    conn.commit()
    return {"import_id": import_id, "sessions_removed": len(session_ids)}
