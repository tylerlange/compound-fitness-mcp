"""The preview/apply gate on remove_import and merge_exercises.

Rejections must never write, and a valid flow must write exactly the rows the
preview listed. The clock is injected, so expiry needs no sleeping."""
import re

import pytest

from strength_coach import synthetic
from strength_coach.confirm import ConfirmationGate

from tests.helpers import dump, make_server, preview_of


@pytest.fixture
def srv(tmp_path, monkeypatch):
    return make_server(tmp_path, monkeypatch)


def _injected_import_id(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT id FROM imports WHERE filename=?",
            (synthetic.INJECTED_FILENAME,)).fetchone()[0]
    finally:
        conn.close()


# --- unit: the gate itself ----------------------------------------------------

def test_gate_token_is_random_and_unique():
    g = ConfirmationGate(ttl_s=60, clock=lambda: 0.0)
    toks = {g.issue("t", {"a": 1}, {"ids": [1]})["confirm_token"] for _ in range(50)}
    assert len(toks) == 50
    assert all(len(t) >= 30 for t in toks)


def test_gate_valid_then_reuse_rejected():
    g = ConfirmationGate(ttl_s=60, clock=lambda: 0.0)
    t = g.issue("t", {"a": 1}, {"ids": [1]})["confirm_token"]
    assert g.consume("t", {"a": 1}, {"ids": [1]}, t) is None
    assert "already used" in g.consume("t", {"a": 1}, {"ids": [1]}, t)


def test_gate_unknown_and_empty_token_rejected():
    g = ConfirmationGate(ttl_s=60, clock=lambda: 0.0)
    assert g.consume("t", {}, {}, "guess") is not None
    assert g.consume("t", {}, {}, "") is not None


def test_gate_token_bound_to_tool_and_args_and_plan():
    g = ConfirmationGate(ttl_s=60, clock=lambda: 0.0)
    t = g.issue("t", {"a": 1}, {"ids": [1]})["confirm_token"]
    assert "does not match" in g.consume("other", {"a": 1}, {"ids": [1]}, t)
    t = g.issue("t", {"a": 1}, {"ids": [1]})["confirm_token"]
    assert "does not match" in g.consume("t", {"a": 2}, {"ids": [1]}, t)
    t = g.issue("t", {"a": 1}, {"ids": [1]})["confirm_token"]
    assert "changed since" in g.consume("t", {"a": 1}, {"ids": [1, 2]}, t)


def test_gate_expiry_boundary():
    now = [0.0]
    g = ConfirmationGate(ttl_s=10, clock=lambda: now[0])
    t = g.issue("t", {}, {})["confirm_token"]
    now[0] = 9.9
    assert g.consume("t", {}, {}, t) is None
    t = g.issue("t", {}, {})["confirm_token"]
    now[0] = 9.9 + 10
    assert "expired" in g.consume("t", {}, {}, t)


def test_gate_ttl_from_env(monkeypatch):
    monkeypatch.setenv("COACH_CONFIRM_TTL_S", "42")
    assert ConfirmationGate().ttl_s == 42
    monkeypatch.setenv("COACH_CONFIRM_TTL_S", "garbage")
    assert ConfirmationGate().ttl_s == 600


def test_gate_pending_store_is_bounded():
    g = ConfirmationGate(ttl_s=60, clock=lambda: 0.0)
    for i in range(500):
        g.issue("t", {"i": i}, {})
    assert len(g._pending) <= 100


# --- remove_import ------------------------------------------------------------

def test_remove_import_preview_writes_nothing(srv):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    before = dump(db_path)
    out = preview_of(m.remove_import(iid))
    assert out["status"] == "preview_only_nothing_changed"
    assert out["confirm_token"]
    assert out["preview"]["session_ids"]
    assert out["preview"]["set_ids"]
    assert dump(db_path) == before


def test_remove_import_without_valid_token_fails_and_writes_nothing(srv):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    before = dump(db_path)
    for bad in ("not-a-real-token", "AAAA", "x" * 32):
        assert m.remove_import(iid, confirm_token=bad).startswith("Refused")
    assert dump(db_path) == before


def test_remove_import_token_with_different_args_fails(srv):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    other = iid - 1
    token = preview_of(m.remove_import(iid))["confirm_token"]
    before = dump(db_path)
    msg = m.remove_import(other, confirm_token=token)
    assert msg.startswith("Refused") and "does not match" in msg
    assert dump(db_path) == before
    # The mismatched attempt burned the token: the right args now fail too.
    assert m.remove_import(iid, confirm_token=token).startswith("Refused")
    assert dump(db_path) == before


def test_remove_import_token_from_other_tool_fails(srv):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    token = preview_of(m.merge_exercises(
        synthetic.INJECTED_EXERCISE, "Squat"))["confirm_token"]
    before = dump(db_path)
    assert m.remove_import(iid, confirm_token=token).startswith("Refused")
    assert dump(db_path) == before


def test_remove_import_reused_token_fails(srv):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    token = preview_of(m.remove_import(iid))["confirm_token"]
    assert m.remove_import(iid, confirm_token=token).startswith("Removed import")
    after = dump(db_path)
    msg = m.remove_import(iid, confirm_token=token)
    assert msg.startswith("Refused") and "already used" in msg
    assert dump(db_path) == after


def test_remove_import_expired_token_fails(srv):
    m, clock, db_path = srv
    iid = _injected_import_id(db_path)
    token = preview_of(m.remove_import(iid))["confirm_token"]
    clock.advance(601)
    before = dump(db_path)
    msg = m.remove_import(iid, confirm_token=token)
    assert msg.startswith("Refused") and "expired" in msg
    assert dump(db_path) == before


def test_remove_import_valid_flow_writes_exactly_previewed_rows(srv):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    prev = preview_of(m.remove_import(iid))
    token, plan = prev["confirm_token"], prev["preview"]
    before = dump(db_path)
    assert m.remove_import(iid, confirm_token=token).startswith("Removed import")
    after = dump(db_path)

    gone_sessions = {r[0] for r in before["sessions"]} - {r[0] for r in after["sessions"]}
    gone_sets = {r[0] for r in before["sets"]} - {r[0] for r in after["sets"]}
    assert gone_sessions == set(plan["session_ids"])
    assert gone_sets == set(plan["set_ids"])
    assert {r[0] for r in before["imports"]} - {r[0] for r in after["imports"]} == {iid}
    # Everything else is untouched.
    for t in ("exercises", "exercise_aliases", "away_periods", "coaching_notes",
              "daily_metrics", "program_snapshots"):
        assert after[t] == before[t]
    assert after["sessions"] == [r for r in before["sessions"]
                                 if r[0] not in gone_sessions]
    assert after["sets"] == [r for r in before["sets"] if r[0] not in gone_sets]


def test_remove_import_stale_preview_is_refused(srv):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    token = preview_of(m.remove_import(iid))["confirm_token"]
    # Another writer attaches one more session to the import after the preview.
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (date, source, workout, source_session_key, "
        "created_at, source_import_id) VALUES ('2026-01-01','liftin','late','k2','x',?)",
        (iid,))
    conn.commit()
    conn.close()
    before = dump(db_path)
    msg = m.remove_import(iid, confirm_token=token)
    assert msg.startswith("Refused") and "changed since" in msg
    assert dump(db_path) == before


def test_remove_import_unknown_id_issues_no_token(srv):
    m, _, db_path = srv
    before = dump(db_path)
    out = m.remove_import(99999)
    assert out.startswith("Could not preview")
    assert "confirm_token" not in out
    assert dump(db_path) == before


# --- merge_exercises ----------------------------------------------------------

def test_merge_preview_writes_nothing(srv):
    m, _, db_path = srv
    before = dump(db_path)
    out = preview_of(m.merge_exercises(synthetic.INJECTED_EXERCISE, "Squat"))
    assert out["confirm_token"]
    assert len(out["preview"]["set_ids"]) == 3
    assert dump(db_path) == before


def test_merge_invalid_requests_issue_no_token(srv):
    m, _, _ = srv
    assert "confirm_token" not in m.merge_exercises("Squat", "Squat")
    assert "confirm_token" not in m.merge_exercises("Nope", "Squat")


def test_merge_rejections_write_nothing(srv):
    m, clock, db_path = srv
    frm = synthetic.INJECTED_EXERCISE
    before = dump(db_path)
    assert m.merge_exercises(frm, "Squat", confirm_token="forged").startswith("Refused")
    token = preview_of(m.merge_exercises(frm, "Squat"))["confirm_token"]
    assert m.merge_exercises(frm, "Bench Press", confirm_token=token).startswith("Refused")
    token = preview_of(m.merge_exercises(frm, "Squat"))["confirm_token"]
    clock.advance(1000)
    assert "expired" in m.merge_exercises(frm, "Squat", confirm_token=token)
    assert dump(db_path) == before


def test_merge_valid_flow_writes_exactly_previewed_rows(srv):
    m, _, db_path = srv
    frm = synthetic.INJECTED_EXERCISE
    prev = preview_of(m.merge_exercises(frm, "Squat"))
    plan, token = prev["preview"], prev["confirm_token"]
    before = dump(db_path)
    assert m.merge_exercises(frm, "Squat", confirm_token=token).startswith("Merged")
    after = dump(db_path)

    into_id = plan["into"]["id"]
    moved = {r[0] for r in after["sets"] if r[2] == into_id} - \
            {r[0] for r in before["sets"] if r[2] == into_id}
    assert moved == set(plan["set_ids"])
    assert {r[0] for r in before["exercises"]} - {r[0] for r in after["exercises"]} \
        == set(plan["exercise_ids_deleted"])
    # Untouched: every set not in the plan is identical.
    untouched = [r for r in before["sets"] if r[0] not in set(plan["set_ids"])]
    assert [r for r in after["sets"] if r[0] not in set(plan["set_ids"])] == untouched
    assert after["sessions"] == before["sessions"]
    assert m.merge_exercises(frm, "Squat", confirm_token=token).startswith("Refused")


# --- audit log ----------------------------------------------------------------

def test_audit_log_records_preview_reject_apply(srv, tmp_path):
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    m.remove_import(iid, confirm_token="forged")
    token = preview_of(m.remove_import(iid))["confirm_token"]
    m.remove_import(iid, confirm_token=token)
    phases = re.findall(r'"phase": "(\w+)"', (tmp_path / "audit.jsonl").read_text())
    assert phases == ["rejected", "preview", "applied"]


# --- the limit, stated as a test ----------------------------------------------

def test_gate_alone_does_not_prove_a_human_said_yes(srv):
    """Documents the residual risk: a caller that makes both calls itself gets
    through. The gate guarantees a preview and exact-match apply; the human
    yes is a prompt-level control and is not testable offline."""
    m, _, db_path = srv
    iid = _injected_import_id(db_path)
    token = preview_of(m.remove_import(iid))["confirm_token"]
    assert m.remove_import(iid, confirm_token=token).startswith("Removed import")
