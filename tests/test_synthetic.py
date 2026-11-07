"""The synthetic dataset is deterministic and exercises the main tool paths."""
import json
import sqlite3

import pytest

from strength_coach import analytics, db, synthetic


def _fingerprint(path):
    conn = sqlite3.connect(path)
    try:
        sessions = conn.execute(
            "SELECT date, source, workout, activity_type, duration_s "
            "FROM sessions ORDER BY date, workout, source_session_key").fetchall()
        sets = conn.execute(
            "SELECT s.date, e.canonical_name, st.seq, st.weight_lb, st.reps, st.rir "
            "FROM sets st JOIN sessions s ON s.id = st.session_id "
            "JOIN exercises e ON e.id = st.exercise_id "
            "ORDER BY s.date, s.workout, st.seq").fetchall()
        daily = conn.execute(
            "SELECT date, metric, value FROM daily_metrics "
            "ORDER BY date, metric").fetchall()
        return sessions, sets, daily
    finally:
        conn.close()


@pytest.fixture(scope="module")
def synth_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("synth") / "training.db")
    synthetic.build(path)
    return path


def test_same_seed_is_identical(synth_db, tmp_path):
    other = str(tmp_path / "again.db")
    synthetic.build(other)
    assert _fingerprint(other) == _fingerprint(synth_db)


def test_different_seed_differs(synth_db, tmp_path):
    other = str(tmp_path / "other.db")
    synthetic.build(other, seed=8)
    assert _fingerprint(other) != _fingerprint(synth_db)


def test_summary_counts(tmp_path):
    summary = synthetic.build(str(tmp_path / "t.db"))
    assert summary["strength_sessions"] > 100
    assert summary["cardio_sessions"] > 10
    assert summary["end"] == synthetic.DEFAULT_END


def test_tools_run_against_synthetic_store(synth_db):
    conn = db.connect(synth_db, read_only=True)
    try:
        cons = analytics.training_consistency(conn)
        assert cons.get("status") != "no_data"
        assert cons["away_periods"]  # the trip is recorded

        a = analytics.exercise_summary(conn, "Squat", "2019-01-01", "2019-12-31")
        b = analytics.exercise_summary(conn, "Squat", "2026-01-01", "2026-12-31")
        assert "error" not in a and "error" not in b
        assert json.dumps(b) != json.dumps(a)

        assert analytics.cardio_summary(
            conn, 90, today=synthetic.DEFAULT_END)["session_count"] > 0
        assert analytics.recovery_status(
            conn, 14, today=synthetic.DEFAULT_END)["metrics"]
        assert analytics.program_diff(conn)["exercise_changes"]
    finally:
        conn.close()


def test_rdl_abbreviation_resolves_to_romanian_deadlift(synth_db):
    conn = db.connect(synth_db, read_only=True)
    try:
        assert analytics.find_exercise(conn, "rdl") == "Romanian Deadlift"
        assert analytics.find_exercise(conn, "Single Leg RDL") == "Single Leg RDL"
    finally:
        conn.close()


def test_incline_dumbbell_press_is_flagged_dual_implement(synth_db):
    conn = db.connect(synth_db, read_only=True)
    try:
        res = analytics.exercise_summary(conn, "Incline Dumbbell Press")
        assert res.get("weight_convention_caveat")
    finally:
        conn.close()


def test_no_hack_squat_in_synthetic_store(synth_db):
    conn = db.connect(synth_db, read_only=True)
    try:
        assert analytics.find_exercise(conn, "Hack Squat") is None
    finally:
        conn.close()
