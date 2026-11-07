import sqlite3

import pytest

from strength_coach import db
from strength_coach.canonical import Canonicalizer
from strength_coach.models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_STANDARD,
)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    yield c
    c.close()


def _resolver(conn):
    return Canonicalizer(conn).resolve


def _session(date, workout, sets, source="macrofactor"):
    return NormalizedSession(
        date=date,
        source=source,
        workout=workout,
        sets=[
            NormalizedSet(exercise_raw=e, seq=i, weight_lb=w, reps=r,
                          set_type=t)
            for i, (e, w, r, t) in enumerate(sets)
        ],
    )


def test_schema_initialized(conn):
    v = conn.execute("SELECT version FROM schema_meta").fetchone()["version"]
    assert v == db.SCHEMA_VERSION
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "sets", "exercises", "exercise_aliases",
            "imports", "session_metrics", "program_snapshots"} <= names


def test_ingest_counts(conn):
    parsed = ParsedImport(source="macrofactor", filename="a.xlsx", sessions=[
        _session("2026-01-01", "A", [("Squat", 225, 5, SET_TYPE_STANDARD),
                                     ("Squat", 225, 5, SET_TYPE_STANDARD)]),
        _session("2026-01-03", "B", [("Bench", 185, 5, SET_TYPE_STANDARD)]),
    ])
    summary = db.ingest(conn, parsed, _resolver(conn))
    assert summary["sessions"] == 2
    assert summary["sets"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM sets").fetchone()["c"] == 3


def test_reingest_upserts_changed_value(conn):
    # First import: squat logged at 225.
    db.ingest(conn, ParsedImport("macrofactor", "a.xlsx", [
        _session("2026-01-01", "A", [("Squat", 225, 5, SET_TYPE_STANDARD)])]),
        _resolver(conn))
    # Re-import same session, weight corrected to 235.
    db.ingest(conn, ParsedImport("macrofactor", "a2.xlsx", [
        _session("2026-01-01", "A", [("Squat", 235, 5, SET_TYPE_STANDARD)])]),
        _resolver(conn))

    rows = conn.execute("SELECT weight_lb FROM sets").fetchall()
    assert len(rows) == 1, "re-import must not duplicate the set"
    assert rows[0]["weight_lb"] == 235, "correction must propagate"


def test_reingest_overlapping_window(conn):
    db.ingest(conn, ParsedImport("macrofactor", "wk1.xlsx", [
        _session("2026-01-01", "A", [("Squat", 225, 5, SET_TYPE_STANDARD)])]),
        _resolver(conn))
    # Overlapping window: old session again + a new one.
    db.ingest(conn, ParsedImport("macrofactor", "wk2.xlsx", [
        _session("2026-01-01", "A", [("Squat", 225, 5, SET_TYPE_STANDARD)]),
        _session("2026-01-08", "A", [("Squat", 230, 5, SET_TYPE_STANDARD)])]),
        _resolver(conn))

    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM sets").fetchone()["c"] == 2


def test_remove_import_only_owned_sessions(conn):
    s1 = db.ingest(conn, ParsedImport("macrofactor", "i1.xlsx", [
        _session("2026-01-01", "A", [("Squat", 225, 5, SET_TYPE_STANDARD)])]),
        _resolver(conn))
    # Second import re-touches the same session (takes ownership) + adds one.
    db.ingest(conn, ParsedImport("macrofactor", "i2.xlsx", [
        _session("2026-01-01", "A", [("Squat", 225, 5, SET_TYPE_STANDARD)]),
        _session("2026-01-08", "A", [("Squat", 230, 5, SET_TYPE_STANDARD)])]),
        _resolver(conn))

    # Removing the first import should delete nothing: import 2 now owns the
    # only session import 1 had touched.
    res = db.remove_import(conn, s1["import_id"])
    assert res["sessions_removed"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2


def test_read_only_connection_cannot_write(conn, tmp_path):
    db.ingest(conn, ParsedImport("macrofactor", "a.xlsx", [
        _session("2026-01-01", "A", [("Squat", 225, 5, SET_TYPE_STANDARD)])]),
        _resolver(conn))
    ro = db.connect(str(tmp_path / "t.db"), read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO imports (source, imported_at) VALUES ('x','y')")
    ro.close()


# --- daily_metrics (wearable) -------------------------------------------


def test_daily_metrics_table_exists(conn):
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "daily_metrics" in names


def test_upsert_daily_metric_inserts(conn):
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "resting_hr", 53, "bpm")
    rows = db.get_daily_metrics(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-07-20"
    assert row["source"] == "wearable"
    assert row["metric"] == "resting_hr"
    assert row["value"] == 53
    assert row["unit"] == "bpm"


def test_upsert_daily_metric_overwrites_same_key(conn):
    # Same (date, source, metric): a re-pull with a corrected value overwrites
    # in place -- idempotent, no duplicate row.
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "hrv_rmssd", 80.0, "ms")
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "hrv_rmssd", 92.1, "ms")
    rows = db.get_daily_metrics(conn)
    assert len(rows) == 1
    assert rows[0]["value"] == 92.1


def test_upsert_daily_metric_distinct_metrics_same_day(conn):
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "resting_hr", 53, "bpm")
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "hrv_rmssd", 92.1, "ms")
    db.upsert_daily_metric(
        conn, "2026-07-20", "wearable", "sleep_minutes_asleep", 414, "min")
    rows = db.get_daily_metrics(conn)
    assert {r["metric"] for r in rows} == {
        "resting_hr", "hrv_rmssd", "sleep_minutes_asleep"}


def test_get_daily_metrics_since_filters(conn):
    db.upsert_daily_metric(conn, "2026-07-01", "wearable", "resting_hr", 55, "bpm")
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "resting_hr", 53, "bpm")
    rows = db.get_daily_metrics(conn, since="2026-07-15")
    assert [r["date"] for r in rows] == ["2026-07-20"]


def test_get_daily_metrics_ordered_by_date_then_metric(conn):
    db.upsert_daily_metric(conn, "2026-07-21", "wearable", "resting_hr", 54, "bpm")
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "hrv_rmssd", 92.1, "ms")
    db.upsert_daily_metric(conn, "2026-07-20", "wearable", "resting_hr", 53, "bpm")
    rows = db.get_daily_metrics(conn)
    assert [(r["date"], r["metric"]) for r in rows] == [
        ("2026-07-20", "hrv_rmssd"),
        ("2026-07-20", "resting_hr"),
        ("2026-07-21", "resting_hr"),
    ]
