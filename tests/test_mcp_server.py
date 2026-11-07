"""Light smoke test for agent/mcp_server.py.

Not a behavior eval (see evals/) -- just confirms the MCP server module
imports cleanly, registers the tool set the agent depends on, and that
calling one read tool against a throwaway DB returns a sane structure.
Never touches the real training.db.
"""
import importlib.util
import json
import os
from datetime import date, timedelta

import pytest

from strength_coach import db
from strength_coach.canonical import Canonicalizer
from strength_coach.models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_STANDARD,
)

MCP_SERVER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent", "mcp_server.py",
)

EXPECTED_TOOLS = {
    "get_training_consistency",
    "get_training_overview",
    "get_exercise_summary",
    "compare_exercise_periods",
    "get_exercise_trend",
    "get_recent_sets",
    "get_recent_workouts",
    "get_cardio_summary",
    "get_recovery_status",
    "list_exercises",
    "list_imports",
    "get_program_changes",
    "get_gym_equipment",
    "get_coaching_profile",
    "get_context",
    "set_away_period",
    "remove_away_period",
    "add_coaching_note",
    "remove_coaching_note",
    "merge_exercises",
    "remove_import",
}

def unwrap(text):
    """Strip the untrusted_data envelope every read tool returns."""
    assert text.count("<untrusted_data>") == 1
    return text.split("<untrusted_data>\n", 1)[1].rsplit("\n</untrusted_data>", 1)[0]


def _load_mcp_server():
    # mcp_server.py puts the repo root on sys.path itself before importing
    # strength_coach, so it loads the same way the agent subprocess does.
    spec = importlib.util.spec_from_file_location("sc_mcp_server", MCP_SERVER_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def sc_mcp():
    return _load_mcp_server()


def test_expected_tools_registered(sc_mcp):
    names = {t.name for t in sc_mcp.mcp._tool_manager.list_tools()}
    assert EXPECTED_TOOLS <= names


def test_get_training_consistency_against_temp_db(sc_mcp, tmp_path, monkeypatch):
    # Temp copy of the DB shape -- never the real training.db.
    temp_db = tmp_path / "training.db"
    conn = db.connect(str(temp_db))
    db.init_schema(conn)
    sessions = [
        NormalizedSession("2026-01-01", "macrofactor", workout="A", sets=[
            NormalizedSet("Squat", 0, weight_lb=225, reps=5, rir=1,
                          set_type=SET_TYPE_STANDARD)]),
        NormalizedSession("2026-01-08", "macrofactor", workout="B", sets=[
            NormalizedSet("Squat", 0, weight_lb=230, reps=5, rir=1,
                          set_type=SET_TYPE_STANDARD)]),
    ]
    db.ingest(conn, ParsedImport("macrofactor", "f", sessions=sessions),
              Canonicalizer(conn).resolve)
    conn.close()

    monkeypatch.setattr(sc_mcp, "DB_PATH", str(temp_db))

    result = json.loads(unwrap(sc_mcp.get_training_consistency()))

    assert isinstance(result, dict)
    assert "error" not in result
    assert result.get("status") != "no_data"
    assert "days_since_last" in result


def _cardio_session(date_, activity_type="walk", key="k1", metrics=None):
    return NormalizedSession(
        date_, "wearable", activity_type=activity_type, workout="Walk",
        source_session_key=key, duration_s=1800, metrics=metrics or [])


def test_get_cardio_summary_against_temp_db(sc_mcp, tmp_path, monkeypatch):
    temp_db = tmp_path / "training.db"
    conn = db.connect(str(temp_db))
    db.init_schema(conn)
    db.ingest(
        conn,
        ParsedImport("wearable", "f", sessions=[
            # Relative date: the tool reads a trailing window from today, so a
            # fixed date silently ages out of it.
            _cardio_session((date.today() - timedelta(days=7)).isoformat(), metrics=[
                ("distance_m", 2500.0, "m"), ("calories", 200.0, "kcal")]),
        ]),
        Canonicalizer(conn).resolve,
    )
    conn.close()
    monkeypatch.setattr(sc_mcp, "DB_PATH", str(temp_db))

    result = json.loads(unwrap(sc_mcp.get_cardio_summary()))

    assert isinstance(result, dict)
    assert "error" not in result
    assert result["session_count"] == 1
    assert result["total_distance_mi"] == round(2500.0 / 1609.344, 2)


def test_get_cardio_summary_no_data_is_not_an_error(sc_mcp, tmp_path, monkeypatch):
    temp_db = tmp_path / "training.db"
    conn = db.connect(str(temp_db))
    db.init_schema(conn)
    conn.close()
    monkeypatch.setattr(sc_mcp, "DB_PATH", str(temp_db))

    result = json.loads(unwrap(sc_mcp.get_cardio_summary()))

    assert "error" not in result
    assert result["status"] == "no_data"


def test_get_recovery_status_against_temp_db(sc_mcp, tmp_path, monkeypatch):
    # get_recovery_status has no injectable "today" (it's a live tool, not a
    # pure function) -- use a date near actual wall-clock now, not a fixed
    # date, so this can't go stale relative to whenever the suite runs.
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()

    temp_db = tmp_path / "training.db"
    conn = db.connect(str(temp_db))
    db.init_schema(conn)
    db.upsert_daily_metric(conn, recent, "wearable", "resting_hr", 53, "bpm")
    conn.close()
    monkeypatch.setattr(sc_mcp, "DB_PATH", str(temp_db))

    result = json.loads(unwrap(sc_mcp.get_recovery_status()))

    assert isinstance(result, dict)
    assert "error" not in result
    assert result["metrics"]["resting_hr"]["latest_value"] == 53


def test_get_context_reports_strength_and_all_activity_consistency(
    sc_mcp, tmp_path, monkeypatch
):
    temp_db = tmp_path / "training.db"
    conn = db.connect(str(temp_db))
    db.init_schema(conn)
    db.ingest(
        conn,
        ParsedImport("macrofactor", "f", sessions=[
            NormalizedSession("2026-07-01", "macrofactor", workout="A", sets=[
                NormalizedSet("Squat", 0, weight_lb=225, reps=5, rir=1,
                              set_type=SET_TYPE_STANDARD)]),
        ]),
        Canonicalizer(conn).resolve,
    )
    db.ingest(
        conn,
        ParsedImport("wearable", "f", sessions=[_cardio_session("2026-07-08")]),
        Canonicalizer(conn).resolve,
    )
    conn.close()
    monkeypatch.setattr(sc_mcp, "DB_PATH", str(temp_db))

    ctx = sc_mcp.get_context()

    assert "strength-only" in ctx
    assert "all logged activity" in ctx
    assert "error" not in ctx.lower()
