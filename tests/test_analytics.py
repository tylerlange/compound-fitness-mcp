import pytest

from strength_coach import analytics, db
from strength_coach.canonical import Canonicalizer
from strength_coach.models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_STANDARD,
    SET_TYPE_WARMUP,
)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    yield c
    c.close()


def _ingest(conn, sessions, source="macrofactor", program=None):
    parsed = ParsedImport(source, "f", sessions=sessions, program_snapshot=program)
    return db.ingest(conn, parsed, Canonicalizer(conn).resolve)


def _sess(date, sets, source="macrofactor", workout="A"):
    return NormalizedSession(date=date, source=source, workout=workout, sets=[
        NormalizedSet(e, i, weight_lb=w, reps=r, rir=rir, set_type=t)
        for i, (e, w, r, rir, t) in enumerate(sets)])


def test_estimate_1rm_rir_adjusted():
    # 225x5 to failure (RIR 0): plain Epley = 225 * (1 + 5/30) = 262.5
    assert analytics.estimate_1rm(225, 5, 0) == 262.5
    # same set with 2 in reserve estimates higher (effective 7 reps):
    # 225 * (1 + 7/30) = 277.5
    assert analytics.estimate_1rm(225, 5, 2) == pytest.approx(277.5, abs=0.1)
    # unknown RIR falls back to plain Epley
    assert analytics.estimate_1rm(225, 5, None) == 262.5


def test_fetch_sets_excludes_warmups_by_default(conn):
    _ingest(conn, [_sess("2026-01-01", [
        ("Squat", 135, 5, None, SET_TYPE_WARMUP),
        ("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    working = analytics.fetch_sets(conn, exercise="Squat")
    assert len(working) == 1 and working[0]["weight_lb"] == 225
    allsets = analytics.fetch_sets(conn, exercise="Squat", include_warmups=True)
    assert len(allsets) == 2


def test_exercise_summary_literal_and_estimated(conn):
    _ingest(conn, [
        _sess("2026-01-01", [("Bench", 185, 5, 2, SET_TYPE_STANDARD)]),
        _sess("2026-01-08", [("Bench", 205, 3, 0, SET_TYPE_STANDARD)], workout="B"),
    ])
    summ = analytics.exercise_summary(conn, "Bench")
    assert summ["n_sets"] == 2
    assert summ["heaviest_weight_lb"] == 205  # literal
    # estimate distinguishes the higher-e1RM set and labels it as an estimate
    assert "estimated" in summ["best_est_1rm_detail"]["note"]


def test_compare_periods_cross_source(conn):
    # Old source (stronglifts) names it differently; canonicalization + seed
    # is what makes the comparison line up. Here names normalize equal.
    _ingest(conn, [_sess("2019-06-01", [("Bench Press", 185, 5, None,
            SET_TYPE_STANDARD)], source="stronglifts")], source="stronglifts")
    _ingest(conn, [_sess("2026-06-01", [("bench press", 225, 5, 1,
            SET_TYPE_STANDARD)], source="macrofactor")], source="macrofactor")
    cmp = analytics.compare_periods(
        conn, "Bench Press", ("2019-01-01", "2019-12-31"),
        ("2026-01-01", "2026-12-31"))
    assert cmp["period_a"]["heaviest_weight_lb"] == 185
    assert cmp["period_b"]["heaviest_weight_lb"] == 225


def test_trend_direction(conn):
    sessions = [
        _sess(f"2026-0{m}-01", [("Squat", 200 + m * 10, 5, 1, SET_TYPE_STANDARD)],
              workout=f"W{m}")
        for m in range(1, 7)
    ]
    _ingest(conn, sessions)
    t = analytics.trend(conn, "Squat")
    assert t["direction"] == "increasing"
    assert t["n_sessions"] == 6


def test_find_exercise_case_insensitive(conn):
    _ingest(conn, [_sess("2026-01-01", [("Bench Press", 185, 5, 1, SET_TYPE_STANDARD)])])
    # user/model supplies a different case; must still resolve
    assert analytics.find_exercise(conn, "bench press") == "Bench Press"
    assert analytics.find_exercise(conn, "BENCH PRESS") == "Bench Press"
    summ = analytics.exercise_summary(conn, "bench press")
    assert summ["n_sets"] == 1 and summ["exercise"] == "Bench Press"


def test_summary_no_match_suggests(conn):
    # a fuzzy-but-unresolvable name (word not contiguous, not normalized-equal)
    # -> no_match + did-you-mean
    _ingest(conn, [_sess("2026-01-01", [("Barbell Back Squat", 225, 5, 1,
            SET_TYPE_STANDARD)])])
    summ = analytics.exercise_summary(conn, "barbell squat")
    assert summ.get("no_match") is True
    assert "Barbell Back Squat" in summ["did_you_mean"]


def test_find_exercise_resolves_plural_via_singular(conn):
    # D1: depluralize + exact match resolves the common loose case in one shot
    _ingest(conn, [_sess("2026-01-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    assert analytics.find_exercise(conn, "squats") == "Squat"  # plural -> singular exact
    assert analytics.find_exercise(conn, "SQUAT") == "Squat"
    summ = analytics.exercise_summary(conn, "squats")
    assert summ.get("no_match") is None and summ["n_sets"] == 1


def test_find_exercise_does_not_substring_bind_wrong_lift(conn):
    # D1 regression: a short/abbrev query must NOT bind a different lift just
    # because it's a unique substring of that lift's name.
    _ingest(conn, [_sess("2026-01-01", [
        ("Romanian Deadlift", 185, 5, 1, SET_TYPE_STANDARD),
        ("Single Leg RDL", 45, 8, 1, SET_TYPE_STANDARD),
        ("Bicep Curl (Dumbbell)", 30, 10, 1, SET_TYPE_STANDARD),
        ("Incline Dumbbell Curl", 25, 10, 1, SET_TYPE_STANDARD)])])
    # "rdl" is a unique substring of "Single Leg RDL" but means Romanian Deadlift
    assert analytics.find_exercise(conn, "rdl") != "Single Leg RDL"
    assert analytics.find_exercise(conn, "rdl") is None  # no exact/alias -> did-you-mean
    # "dumbbell curl" must not silently bind the Incline variant
    assert analytics.find_exercise(conn, "dumbbell curl") != "Incline Dumbbell Curl"
    # the correctly-named lift still resolves exactly
    assert analytics.find_exercise(conn, "romanian deadlift") == "Romanian Deadlift"


def test_find_exercise_ambiguous_stays_no_match(conn):
    # D1: two squat-like lifts -> "squat" is ambiguous -> None + did-you-mean
    _ingest(conn, [_sess("2026-01-01", [
        ("Barbell Back Squat", 225, 5, 1, SET_TYPE_STANDARD),
        ("Front Squat", 185, 5, 1, SET_TYPE_STANDARD)])])
    assert analytics.find_exercise(conn, "squat") is None
    summ = analytics.exercise_summary(conn, "squat")
    assert summ.get("no_match") is True
    assert set(summ["did_you_mean"]) >= {"Barbell Back Squat", "Front Squat"}


def test_find_exercise_normalized_and_alias(conn):
    # D1: normalized match (punctuation/spacing) resolves in one shot
    _ingest(conn, [_sess("2026-01-01", [("Bench Press (Barbell)", 185, 5, 1,
            SET_TYPE_STANDARD)])])
    assert analytics.find_exercise(conn, "bench press barbell") == "Bench Press (Barbell)"
    assert analytics.find_exercise(conn, "BENCH PRESS (BARBELL)") == "Bench Press (Barbell)"


def test_find_exercise_symmetric_plural(conn):
    # D1 cycle-2: depluralize works BOTH directions across a trailing "s"
    _ingest(conn, [_sess("2026-01-01", [
        ("Calf Raises", 200, 12, 1, SET_TYPE_STANDARD),   # plural canonical
        ("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])        # singular canonical
    assert analytics.find_exercise(conn, "calf raise") == "Calf Raises"   # singular query
    assert analytics.find_exercise(conn, "squats") == "Squat"             # plural query


def test_find_exercise_resolves_abbrev_via_alias(conn):
    # D1 cycle-2: a seeded abbreviation alias resolves to the RIGHT lift, so a
    # bare "rdl" doesn't fall through to did-you-mean (where the wrong variant
    # would rank first).
    from strength_coach.canonical import seed_aliases
    _ingest(conn, [_sess("2026-01-01", [
        ("Romanian Deadlift", 185, 5, 1, SET_TYPE_STANDARD),
        ("Single Leg RDL", 45, 8, 1, SET_TYPE_STANDARD)])])
    seed_aliases(conn, {"RDL": "Romanian Deadlift"})
    assert analytics.find_exercise(conn, "rdl") == "Romanian Deadlift"
    assert analytics.find_exercise(conn, "RDL") == "Romanian Deadlift"


def test_bodyweight_summary_uses_reps(conn):
    # Pull-ups: mostly unloaded (realistic ~10-20% weighted), one weighted set.
    _ingest(conn, [_sess("2026-01-01", [
        ("Pull-Up", None, 8, None, SET_TYPE_STANDARD),
        ("Pull-Up", None, 10, None, SET_TYPE_STANDARD),
        ("Pull-Up", None, 12, None, SET_TYPE_STANDARD),
        ("Pull-Up", None, 9, None, SET_TYPE_STANDARD),
        ("Pull-Up", 25, 5, None, SET_TYPE_STANDARD)])])
    summ = analytics.exercise_summary(conn, "Pull-Up")
    assert summ["is_bodyweight"] is True
    assert summ["best_reps"] == 12
    assert summ["heaviest_added_weight_lb"] == 25
    assert "best_est_1rm_lb" not in summ  # weight-centric e1RM not surfaced


def test_bodyweight_trend_uses_reps(conn):
    sessions = [
        _sess(f"2026-0{m}-01", [("Push Up", None, 10 + m * 3, None,
              SET_TYPE_STANDARD)], workout=f"W{m}")
        for m in range(1, 7)
    ]
    _ingest(conn, sessions)
    t = analytics.trend(conn, "Push Up")
    assert t["metric"] == "reps"
    assert t["direction"] == "increasing"


def test_e1rm_excludes_high_rep_sets(conn):
    # A 20-rep back-off set has a higher raw Epley estimate than the top single,
    # but must NOT be chosen as the strength peak.
    _ingest(conn, [_sess("2026-01-01", [
        ("Deadlift", 405, 2, 0, SET_TYPE_STANDARD),   # e1RM 432
        ("Deadlift", 300, 20, 0, SET_TYPE_STANDARD)])])  # raw e1RM 500, capped out
    summ = analytics.exercise_summary(conn, "Deadlift")
    assert summ["best_est_1rm_detail"]["reps"] == 2
    assert summ["best_est_1rm_lb"] == analytics.estimate_1rm(405, 2, 0)


def test_list_imports(conn):
    _ingest(conn, [_sess("2026-01-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    imports = analytics.list_imports(conn)
    assert len(imports) == 1
    assert imports[0]["set_count"] == 1


def test_training_consistency(conn):
    # trained the last 3 consecutive weeks, then a gap, then one older week
    _ingest(conn, [
        _sess("2026-06-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)], workout="A"),
        _sess("2026-06-08", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)], workout="B"),
        _sess("2026-06-15", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)], workout="C"),
        _sess("2026-04-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)], workout="D"),
    ])
    c = analytics.training_consistency(conn, today="2026-06-16")
    assert c["last_session"] == "2026-06-15"
    assert c["days_since_last"] == 1
    assert c["current_streak_weeks"] == 3
    assert c["longest_streak_weeks"] == 3
    assert c["days_trained_last_28"] == 3


def test_training_consistency_no_data(conn):
    assert analytics.training_consistency(conn)["status"] == "no_data"


def test_away_period_bridges_streak(conn):
    # trained 2 weeks, then a 13-day trip covering the next ~2 weeks, then
    # trained again. The trip must not break the streak or count against the user.
    _ingest(conn, [
        _sess("2026-03-02", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)], workout="A"),
        _sess("2026-03-09", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)], workout="B"),
        _sess("2026-03-30", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)], workout="C"),
    ])
    # without the away period, the 03-16 and 03-23 weeks are misses
    before = analytics.training_consistency(conn, today="2026-03-31")
    assert before["current_streak_weeks"] == 1  # only the 03-30 week

    db.add_away_period(conn, "2026-03-12", "2026-03-24", "travel")
    after = analytics.training_consistency(conn, today="2026-03-31")
    # 03-30 active, 03-23 away(skip), 03-16 away(skip), 03-09 active, 03-02 active
    assert after["current_streak_weeks"] == 3
    assert after["away_periods"][0]["start"] == "2026-03-12"


def test_currently_away_flag(conn):
    _ingest(conn, [_sess("2026-07-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    db.add_away_period(conn, "2026-07-10", "2026-07-20", "trip")
    c = analytics.training_consistency(conn, today="2026-07-15")
    assert c["currently_away"] is True


def test_program_diff(conn):
    prog_v1 = {"workouts": {"Workout A": ["Squat", "Bench"], "Workout B": ["Deadlift"]}}
    prog_v2 = {"workouts": {"Workout A": ["Squat", "Bench", "Incline Press"],
                            "Workout C": ["Pull Up"]}}
    _ingest(conn, [], program=prog_v1)
    _ingest(conn, [], program=prog_v2)
    diff = analytics.program_diff(conn)
    assert diff["workouts_added"] == ["Workout C"]
    assert diff["workouts_removed"] == ["Workout B"]
    assert diff["exercise_changes"]["Workout A"]["added"] == ["Incline Press"]


def test_is_dual_implement():
    # dumbbell + dual-cable (incl. cable fly/crossover) -> flagged
    for name in ["Chest-Supported Elbows-Out Dumbbell Row", "Dual Cable Bayesian Curl",
                 "Horizontal Cable Fly", "High-To-Low Cable Fly", "Cable Crossover",
                 "Incline Dumbbell Press", "Lateral Raise (Dumbbell)"]:
        assert analytics.is_dual_implement(name), name
    # single-implement / unilateral -> NOT flagged (per-hand == total)
    for name in ["One Arm Dumbbell Row", "Dumbbell Pullover", "Single Arm Lat Pulldown",
                 "Barbell Back Squat", "Close Grip Bench Press", "Cable Face Pull",
                 "Cable Rope Triceps Pushdown"]:
        assert not analytics.is_dual_implement(name), name


def test_dual_implement_caveat_surfaced(conn):
    _ingest(conn, [_sess("2026-01-01", [
        ("Dumbbell Row", 50, 8, 1, SET_TYPE_STANDARD)])])
    s = analytics.exercise_summary(conn, "Dumbbell Row")
    assert "weight_convention_caveat" in s
    t = analytics.trend(conn, "Dumbbell Row")
    # trend needs >=3 sessions for a direction, but the caveat rides along anyway
    assert "weight_convention_caveat" in t


def test_dual_implement_trend_is_not_comparable(conn):
    # D2: whole-history trend on a dual-implement lift must NOT emit a cross-year
    # pct_change; it's marked not_comparable, with the caveat attached.
    for d, w in [("2024-01-01", 40), ("2024-06-01", 45), ("2025-01-01", 45),
                 ("2026-01-01", 70), ("2026-06-01", 90)]:
        _ingest(conn, [_sess(d, [("Incline Dumbbell Press", w, 8, 1, SET_TYPE_STANDARD)])])
    t = analytics.trend(conn, "Incline Dumbbell Press")
    assert t["direction"] == "not_comparable"
    assert "pct_change" not in t
    assert "weight_convention_caveat" in t
    # a barbell lift with the same shape still reports a real pct_change
    for d, w in [("2024-01-02", 135), ("2025-01-02", 185), ("2026-01-02", 225)]:
        _ingest(conn, [_sess(d, [("Barbell Bench Press", w, 5, 1, SET_TYPE_STANDARD)])])
    bt = analytics.trend(conn, "Barbell Bench Press")
    assert bt["direction"] == "increasing" and "pct_change" in bt


def test_dual_implement_overview_keeps_within_window_numbers(conn):
    # D2: within-window figures are kept (real within a period); caveat attached.
    for d, w in [("2026-04-01", 60), ("2026-05-01", 70), ("2026-06-01", 80),
                 ("2026-06-20", 90)]:
        _ingest(conn, [_sess(d, [("Dumbbell Row", w, 8, 1, SET_TYPE_STANDARD)])])
    ov = analytics.training_overview(conn, window_days=180, today="2026-07-01")
    row = next(e for e in ov["exercises"] if e["exercise"] == "Dumbbell Row")
    assert row["dual_implement"] is True
    assert "weight_convention_caveat" in row
    # window number retained (progression within a period is real)
    assert row["direction"] == "increasing"
    assert row["best_weight_lb_in_window"] == 90


def test_session_readout_one_call(conn):
    # older history so all-time bests + trend have context
    _ingest(conn, [_sess("2026-05-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _ingest(conn, [_sess("2026-06-01", [("Squat", 235, 5, 1, SET_TYPE_STANDARD)])])
    _ingest(conn, [_sess("2026-07-01", [
        ("Squat", 245, 5, 1, SET_TYPE_STANDARD),
        ("Dumbbell Row", 90, 10, 0, SET_TYPE_STANDARD)])])
    r = analytics.session_readout(conn)
    assert r["date"] == "2026-07-01"
    assert "consistency" in r  # lead metric bundled
    names = {e["exercise"] for e in r["exercises"]}
    assert names == {"Squat", "Dumbbell Row"}
    squat = next(e for e in r["exercises"] if e["exercise"] == "Squat")
    assert squat["top_set"]["weight_lb"] == 245
    assert squat["all_time_best_weight_lb"] == 245
    assert squat["trend"] == "increasing"
    # dual-implement flagged + caveat present
    row = next(e for e in r["exercises"] if e["exercise"] == "Dumbbell Row")
    assert row["dual_implement"] is True
    assert "weight_convention_caveat" in r


def test_session_readout_explicit_date(conn):
    _ingest(conn, [_sess("2026-06-01", [("Bench", 185, 5, 1, SET_TYPE_STANDARD)])])
    _ingest(conn, [_sess("2026-07-01", [("Squat", 245, 5, 1, SET_TYPE_STANDARD)])])
    r = analytics.session_readout(conn, "2026-06-01")
    assert r["date"] == "2026-06-01"
    assert {e["exercise"] for e in r["exercises"]} == {"Bench"}


def test_training_overview_one_call(conn):
    # 4 squat sessions across ~3 months, rising; one big gap
    for d, w in [("2026-04-01", 225), ("2026-04-08", 230),
                 ("2026-06-20", 240), ("2026-06-27", 245)]:
        _ingest(conn, [_sess(d, [("Squat", w, 5, 1, SET_TYPE_STANDARD)])])
    ov = analytics.training_overview(conn, window_days=180, today="2026-07-01")
    assert ov["sessions_in_window"] == 4
    assert "consistency" in ov
    sq = next(e for e in ov["exercises"] if e["exercise"] == "Squat")
    assert sq["n_sessions_in_window"] == 4
    assert sq["direction"] == "increasing"
    assert sq["best_weight_lb_in_window"] == 245
    # the 04-08 -> 06-20 gap (73 days) should surface
    assert any(g["days"] >= 70 for g in ov["notable_gaps"])


def test_training_overview_gap_excludes_away(conn):
    _ingest(conn, [_sess("2026-04-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _ingest(conn, [_sess("2026-04-08", [("Squat", 230, 5, 1, SET_TYPE_STANDARD)])])
    _ingest(conn, [_sess("2026-05-20", [("Squat", 235, 5, 1, SET_TYPE_STANDARD)])])
    # cover the 04-08 -> 05-20 gap with an away period
    db.add_away_period(conn, "2026-04-10", "2026-05-18", "trip")
    ov = analytics.training_overview(conn, window_days=180, today="2026-06-01")
    assert ov["notable_gaps"] == [], "an away-covered gap must not be flagged"


def test_ingest_new_vs_updated(conn):
    db.ingest(conn, ParsedImport("macrofactor", "wk1.xlsx", [
        _sess("2026-07-16", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])]),
        Canonicalizer(conn).resolve)
    # rolling re-export: 07-16 again (already logged) + 07-21 (new)
    summary = db.ingest(conn, ParsedImport("macrofactor", "wk2.xlsx", [
        _sess("2026-07-16", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)]),
        _sess("2026-07-21", [("Squat", 235, 5, 1, SET_TYPE_STANDARD)])]),
        Canonicalizer(conn).resolve)
    assert summary["new_count"] == 1
    assert summary["updated_count"] == 1
    assert summary["new_sessions"] == [{"date": "2026-07-21", "workout": "A"}]
    assert summary["updated_sessions"] == [{"date": "2026-07-16", "workout": "A"}]


# --- gap-closing tests added by test engineer (fabricated data only) ---


def test_session_readout_no_data(conn):
    # empty DB: MAX(date) is NULL -> no_data branch
    r = analytics.session_readout(conn)
    assert r == {"status": "no_data"}


def test_session_readout_no_session_on_date(conn):
    # DB non-empty, but the requested date has no logged session
    _ingest(conn, [_sess("2026-06-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    r = analytics.session_readout(conn, "2026-06-02")
    assert r["status"] == "no_session_on_date"
    assert r["date"] == "2026-06-02"


def test_session_readout_bodyweight_lift(conn):
    # bodyweight/rep-based lift: session_best_est_1rm must be None (no weighted
    # e1RM), all-time context comes from reps, trend metric is reps.
    _ingest(conn, [_sess("2026-07-01", [
        ("Pull-Up", None, 8, None, SET_TYPE_STANDARD),
        ("Pull-Up", None, 12, None, SET_TYPE_STANDARD),
        ("Pull-Up", None, 10, None, SET_TYPE_STANDARD)])])
    r = analytics.session_readout(conn)
    pu = next(e for e in r["exercises"] if e["exercise"] == "Pull-Up")
    assert pu["session_best_est_1rm_lb"] is None  # no weighted set to estimate from
    assert pu["top_set"]["reps"] == 12  # heaviest-then-reps top set (all unloaded)
    assert pu["all_time_best_reps"] == 12
    assert pu.get("all_time_best_weight_lb") is None
    assert "dual_implement" not in pu
    assert "weight_convention_caveat" not in r  # no dual-implement lifts


def test_training_overview_filters_under_three_sessions(conn):
    # Squat: 3 sessions in window (kept). Curl: 2 sessions (dropped by the <3 filter).
    for d, w in [("2026-06-01", 225), ("2026-06-08", 230), ("2026-06-15", 235)]:
        _ingest(conn, [_sess(d, [("Squat", w, 5, 1, SET_TYPE_STANDARD)])])
    for d, w in [("2026-06-02", 40), ("2026-06-09", 45)]:
        _ingest(conn, [_sess(d, [("Curl", w, 10, 1, SET_TYPE_STANDARD)], workout="B")])
    ov = analytics.training_overview(conn, window_days=180, today="2026-06-20")
    names = {e["exercise"] for e in ov["exercises"]}
    assert names == {"Squat"}, "an exercise with <3 sessions in window must be excluded"


def test_training_overview_empty_window(conn):
    # all data older than the window: nothing survives the date filter
    _ingest(conn, [_sess("2026-01-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    ov = analytics.training_overview(conn, window_days=30, today="2026-07-01")
    assert ov["sessions_in_window"] == 0
    assert ov["exercises"] == []
    assert ov["notable_gaps"] == []
    assert "consistency" in ov  # consistency is whole-history, still present


def test_training_overview_dual_implement_caveat(conn):
    for d, w in [("2026-06-01", 80), ("2026-06-08", 85), ("2026-06-15", 90)]:
        _ingest(conn, [_sess(d, [("Dumbbell Row", w, 10, 1, SET_TYPE_STANDARD)])])
    ov = analytics.training_overview(conn, window_days=180, today="2026-06-20")
    row = next(e for e in ov["exercises"] if e["exercise"] == "Dumbbell Row")
    assert row["dual_implement"] is True
    assert "weight_convention_caveat" in row


def test_training_overview_truncates_exercises_to_20(conn):
    # 21 exercises, each trained on the same 3 dates -> all qualify (>=3 sessions),
    # list must be capped at 20.
    dates = ["2026-06-01", "2026-06-08", "2026-06-15"]
    for d in dates:
        sets = [(f"Lift{i:02d}", 100 + i, 5, 1, SET_TYPE_STANDARD) for i in range(21)]
        _ingest(conn, [_sess(d, sets)])
    ov = analytics.training_overview(conn, window_days=180, today="2026-06-20")
    assert len(ov["exercises"]) == 20


def test_training_overview_truncates_gaps_to_5(conn):
    # 7 training days spaced 15 days apart -> 6 qualifying gaps (>=10 days),
    # notable_gaps must be capped at 5.
    dates = ["2026-01-06", "2026-01-21", "2026-02-05", "2026-02-20",
             "2026-03-07", "2026-03-22", "2026-04-06"]
    for d in dates:
        _ingest(conn, [_sess(d, [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    ov = analytics.training_overview(conn, window_days=180, today="2026-04-10")
    assert len(ov["notable_gaps"]) == 5
    assert all(g["days"] >= 10 for g in ov["notable_gaps"])


def test_top_set_all_warmup_returns_none():
    sets = [{"set_type": SET_TYPE_WARMUP, "weight_lb": 135, "reps": 5},
            {"set_type": SET_TYPE_WARMUP, "weight_lb": 185, "reps": 3}]
    assert analytics._top_set(sets) is None


def test_top_set_heaviest_then_reps_excludes_warmup():
    sets = [
        {"set_type": SET_TYPE_STANDARD, "weight_lb": 225, "reps": 5},
        {"set_type": SET_TYPE_STANDARD, "weight_lb": 225, "reps": 8},  # ties weight, more reps
        {"set_type": SET_TYPE_WARMUP, "weight_lb": 315, "reps": 1},    # heavier but warmup
    ]
    top = analytics._top_set(sets)
    assert top["weight_lb"] == 225 and top["reps"] == 8


def test_direction_branches():
    assert analytics._direction([100, 100])["direction"] == "insufficient_data"
    assert analytics._direction([100, 100])["n_sessions"] == 2
    assert analytics._direction([100, 100, 100])["direction"] == "stable"
    assert analytics._direction([300, 250, 150])["direction"] == "decreasing"
    assert analytics._direction([100, 110, 130])["direction"] == "increasing"
    # 2% change stays inside the default 3% deadband -> stable, not increasing
    assert analytics._direction([100, 100, 102])["direction"] == "stable"


def test_best_per_session_series_high_rep_and_bodyweight():
    sets = [
        {"date": "2026-01-01", "weight_lb": 225, "reps": 5, "rir": 0},
        {"date": "2026-01-01", "weight_lb": 300, "reps": 20, "rir": 0},  # >12 -> val 0
        {"date": "2026-01-08", "weight_lb": 135, "reps": 15, "rir": 0},  # >12 only -> 0
    ]
    # weighted: 01-01 keeps the <=12-rep set; 01-08 is all-high-rep -> dropped
    series = analytics._best_per_session_series(sets, bodyweight=False)
    assert series == [analytics.estimate_1rm(225, 5, 0)]
    # bodyweight: reps used regardless of the e1RM rep ceiling, oldest first
    assert analytics._best_per_session_series(sets, bodyweight=True) == [20, 15]


def test_ingest_multi_file_new_and_legacy_keys(conn):
    # two separate files: first all-new, second mixes an already-logged day with
    # a new one. Legacy `sessions`/`sets` keys keep file totals; new_count/
    # updated_count split by novelty.
    first = _ingest(conn, [_sess("2026-07-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    assert first["new_count"] == 1 and first["updated_count"] == 0
    assert first["sessions"] == 1 and first["sets"] == 1  # legacy compat keys

    second = _ingest(conn, [
        _sess("2026-07-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)]),   # already logged
        _sess("2026-07-08", [("Squat", 235, 5, 1, SET_TYPE_STANDARD)])])  # new
    assert second["new_count"] == 1 and second["updated_count"] == 1
    assert second["sessions"] == 2  # legacy: total sessions in the file, not just new


def test_recent_workouts(conn):
    _ingest(conn, [
        _sess("2026-06-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD),
                             ("Dumbbell Row", 90, 10, 0, SET_TYPE_STANDARD)], workout="A"),
        _sess("2026-06-03", [("Bench Press", 185, 5, 2, SET_TYPE_WARMUP),
                             ("Bench Press", 205, 5, 1, SET_TYPE_STANDARD)], workout="B"),
        _sess("2026-06-05", [("Deadlift", 315, 3, 2, SET_TYPE_STANDARD)], workout="C"),
    ])
    rw = analytics.recent_workouts(conn, count=2)
    assert rw["count"] == 2
    # most recent first
    assert [w["date"] for w in rw["workouts"]] == ["2026-06-05", "2026-06-03"]
    # warmups excluded; sets grouped per exercise
    bench = rw["workouts"][1]["exercises"]
    assert len(bench) == 1 and bench[0]["exercise"] == "Bench Press"
    assert bench[0]["sets"] == [{"weight_lb": 205, "reps": 5, "rir": 1}]
    # dual-implement lift flagged when present
    older = analytics.recent_workouts(conn, count=3)
    row = older["workouts"][2]["exercises"]  # 2026-06-01 session
    dumbbell = next(e for e in row if e["exercise"] == "Dumbbell Row")
    assert dumbbell["dual_implement"] is True


def test_recent_workouts_empty_db(conn):
    assert analytics.recent_workouts(conn)["status"] == "no_data"


# --- wearable: consistency split, cardio_summary, recovery_status -------


def _cardio_ingest(conn, date_, activity_type="walk", workout="Walk",
                    metrics=None, duration_s=1800, source="wearable",
                    key=None):
    """Ingest a cardio session directly (no `sets` -- mirrors what
    gh_normalize.normalize_exercise produces)."""
    session = NormalizedSession(
        date=date_, source=source, activity_type=activity_type, workout=workout,
        source_session_key=key or f"{source}-{date_}-{workout}",
        duration_s=duration_s, metrics=metrics or [])
    parsed = ParsedImport(source=source, filename="f", sessions=[session])
    return db.ingest(conn, parsed, Canonicalizer(conn).resolve)


def test_training_consistency_default_excludes_cardio(conn):
    _ingest(conn, [_sess("2026-07-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _cardio_ingest(conn, "2026-07-08")  # a walk, different day
    result = analytics.training_consistency(conn, today="2026-07-10")
    assert result["days_trained_last_28"] == 1  # strength-only default
    assert result["note"].startswith("strength sessions only")


def test_training_consistency_none_includes_all_activity(conn):
    _ingest(conn, [_sess("2026-07-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _cardio_ingest(conn, "2026-07-08")
    result = analytics.training_consistency(
        conn, today="2026-07-10", activity_types=None)
    assert result["days_trained_last_28"] == 2
    assert "all logged activity" in result["note"]


def test_training_consistency_explicit_filter(conn):
    _ingest(conn, [_sess("2026-07-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _cardio_ingest(conn, "2026-07-08", activity_type="walk")
    _cardio_ingest(conn, "2026-07-09", activity_type="hike", key="hike-1")
    result = analytics.training_consistency(
        conn, today="2026-07-10", activity_types=["walk", "hike"])
    assert result["days_trained_last_28"] == 2


def test_training_overview_default_excludes_cardio_from_gaps(conn):
    # Two strength sessions 20 days apart with a cardio session bridging the
    # gap -- default (strength-only) must still flag the gap.
    _ingest(conn, [_sess("2026-06-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _cardio_ingest(conn, "2026-06-10")
    _ingest(conn, [_sess("2026-06-21", [("Squat", 230, 5, 1, SET_TYPE_STANDARD)])])
    ov = analytics.training_overview(conn, window_days=60, today="2026-06-25")
    assert any(g["days"] >= 15 for g in ov["notable_gaps"])


def test_training_overview_all_activity_closes_the_gap(conn):
    _ingest(conn, [_sess("2026-06-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _cardio_ingest(conn, "2026-06-10")
    _ingest(conn, [_sess("2026-06-21", [("Squat", 230, 5, 1, SET_TYPE_STANDARD)])])
    ov = analytics.training_overview(
        conn, window_days=60, today="2026-06-25", activity_types=None)
    assert all(g["days"] < 15 for g in ov["notable_gaps"])


def test_cardio_summary_aggregates_by_activity_type(conn):
    _cardio_ingest(conn, "2026-07-01", activity_type="walk", workout="Walk",
                    metrics=[("distance_m", 2500.0, "m"), ("calories", 200.0, "kcal"),
                             ("avg_hr", 95.0, "bpm")],
                    duration_s=1800, key="walk-1")
    _cardio_ingest(conn, "2026-07-05", activity_type="hike", workout="Hike",
                    metrics=[("distance_m", 8000.0, "m"), ("calories", 600.0, "kcal"),
                             ("elevation_gain_m", 300.0, "m")],
                    duration_s=7200, key="hike-1")
    summary = analytics.cardio_summary(conn, window_days=30, today="2026-07-10")
    assert summary["session_count"] == 2
    assert summary["last_activity_date"] == "2026-07-05"
    types = {e["activity_type"]: e for e in summary["by_activity_type"]}
    assert types["walk"]["session_count"] == 1
    assert types["walk"]["distance_mi"] == round(2500.0 / 1609.344, 2)
    assert types["hike"]["distance_mi"] == round(8000.0 / 1609.344, 2)
    # elevation surfaced in feet; walk had none so it reads 0
    assert types["hike"]["elevation_gain_ft"] == round(300.0 * 3.28084)
    assert types["walk"]["elevation_gain_ft"] == 0
    assert summary["total_distance_mi"] == round(10500.0 / 1609.344, 2)
    assert summary["total_elevation_gain_ft"] == round(300.0 * 3.28084)
    assert summary["total_calories"] == 800.0
    assert summary["avg_heart_rate_bpm"] == 95.0  # only walk had avg_hr


def test_cardio_summary_excludes_strength_sessions(conn):
    _ingest(conn, [_sess("2026-07-01", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    summary = analytics.cardio_summary(conn, window_days=30, today="2026-07-10")
    assert summary["status"] == "no_data"


def test_cardio_summary_no_data(conn):
    summary = analytics.cardio_summary(conn)
    assert summary["status"] == "no_data"


def _seed_records(conn):
    # short run, long hike (biggest distance + elevation), medium walk (longest time)
    _cardio_ingest(conn, "2026-07-01", activity_type="run", workout="Run",
                    metrics=[("distance_m", 5000.0, "m"),
                             ("elevation_gain_m", 50.0, "m")],
                    duration_s=1500, key="run-1")
    _cardio_ingest(conn, "2026-07-05", activity_type="hike", workout="Hike",
                    metrics=[("distance_m", 20000.0, "m"),
                             ("elevation_gain_m", 900.0, "m")],
                    duration_s=9000, key="hike-1")
    _cardio_ingest(conn, "2026-07-08", activity_type="walk", workout="Walk",
                    metrics=[("distance_m", 3000.0, "m")],
                    duration_s=12000, key="walk-1")


def test_activity_records_ranks_by_distance(conn):
    _seed_records(conn)
    res = analytics.activity_records(conn, metric="distance", today="2026-07-10")
    top = res["records"][0]
    assert top["activity_type"] == "hike"
    assert top["distance_mi"] == round(20000.0 / 1609.344, 2)
    # ranked list is descending
    dists = [r["distance_mi"] for r in res["records"]]
    assert dists == sorted(dists, reverse=True)


def test_activity_records_duration_and_elevation_and_filter(conn):
    _seed_records(conn)
    # longest by time is the walk
    assert analytics.activity_records(
        conn, metric="duration", today="2026-07-10")["records"][0][
            "activity_type"] == "walk"
    # most elevation is the hike
    assert analytics.activity_records(
        conn, metric="elevation", today="2026-07-10")["records"][0][
            "activity_type"] == "hike"
    # activity_type filter narrows the pool
    runs = analytics.activity_records(
        conn, metric="distance", activity_types=["run"], today="2026-07-10")
    assert {r["activity_type"] for r in runs["records"]} == {"run"}
    # walk has no elevation metric -> dropped from an elevation ranking
    elev = analytics.activity_records(conn, metric="elevation", today="2026-07-10")
    assert all(r["activity_type"] != "walk" for r in elev["records"])


def test_activity_records_bad_metric_and_no_data(conn):
    assert "error" in analytics.activity_records(conn, metric="bogus")
    assert analytics.activity_records(
        conn, today="2026-07-10")["status"] == "no_data"


def test_cardio_bests_one_per_metric(conn):
    _seed_records(conn)
    bests = analytics.cardio_bests(conn)["all_time_bests"]
    assert set(bests) == {"distance", "duration", "elevation"}
    assert bests["distance"]["activity_type"] == "hike"
    assert bests["duration"]["activity_type"] == "walk"
    assert bests["elevation"]["activity_type"] == "hike"


def test_cardio_summary_window_excludes_old_sessions(conn):
    _cardio_ingest(conn, "2026-01-01", key="old-walk")
    summary = analytics.cardio_summary(conn, window_days=30, today="2026-07-10")
    assert summary["status"] == "no_data"


def test_recovery_status_latest_and_window_avg(conn):
    db.upsert_daily_metric(conn, "2026-07-01", "wearable", "resting_hr", 55, "bpm")
    db.upsert_daily_metric(conn, "2026-07-08", "wearable", "resting_hr", 53, "bpm")
    result = analytics.recovery_status(conn, window_days=14, today="2026-07-10")
    rhr = result["metrics"]["resting_hr"]
    assert rhr["latest_value"] == 53
    assert rhr["latest_date"] == "2026-07-08"
    assert rhr["window_avg"] == 54.0
    assert rhr["n_days"] == 2


def test_recovery_status_no_data(conn):
    result = analytics.recovery_status(conn)
    assert result["status"] == "no_data"


def test_recovery_status_window_excludes_old_metrics(conn):
    db.upsert_daily_metric(conn, "2026-01-01", "wearable", "hrv_rmssd", 80, "ms")
    result = analytics.recovery_status(conn, window_days=14, today="2026-07-10")
    assert result["status"] == "no_data"


def test_recovery_status_no_synthetic_readiness_score(conn):
    db.upsert_daily_metric(conn, "2026-07-08", "wearable", "resting_hr", 53, "bpm")
    result = analytics.recovery_status(conn, today="2026-07-10")
    assert "readiness" not in result
    assert "readiness_score" not in result


def test_session_readout_cardio_only_day_not_no_session(conn):
    _cardio_ingest(conn, "2026-07-05", activity_type="hike", workout="Hike",
                    metrics=[("distance_m", 8000.0, "m"), ("calories", 600.0, "kcal")],
                    duration_s=7200, key="hike-1")
    r = analytics.session_readout(conn, "2026-07-05")
    assert r.get("status") != "no_session_on_date"
    assert r["exercises"] == []
    assert len(r["cardio_sessions"]) == 1
    cs = r["cardio_sessions"][0]
    assert cs["activity_type"] == "hike"
    assert cs["duration_min"] == 120.0
    metric_names = {m["metric"] for m in cs["metrics"]}
    assert metric_names == {"distance_m", "calories"}


def test_session_readout_mixed_day_has_both(conn):
    _ingest(conn, [_sess("2026-07-05", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    _cardio_ingest(conn, "2026-07-05", activity_type="walk", workout="Walk",
                    metrics=[("distance_m", 2000.0, "m")], key="walk-mixed")
    r = analytics.session_readout(conn, "2026-07-05")
    assert {e["exercise"] for e in r["exercises"]} == {"Squat"}
    assert len(r["cardio_sessions"]) == 1


def test_recent_workouts_cardio_session_renders_metrics(conn):
    _cardio_ingest(conn, "2026-07-05", activity_type="run", workout="Run",
                    metrics=[("distance_m", 5000.0, "m"), ("calories", 400.0, "kcal")],
                    duration_s=1500, key="run-1")
    rw = analytics.recent_workouts(conn, count=5)
    entry = rw["workouts"][0]
    assert entry["activity_type"] == "run"
    assert "exercises" not in entry
    assert entry["duration_min"] == 25.0
    metric_names = {m["metric"] for m in entry["metrics"]}
    assert metric_names == {"distance_m", "calories"}


def test_recent_workouts_strength_session_unaffected(conn):
    _ingest(conn, [_sess("2026-07-05", [("Squat", 225, 5, 1, SET_TYPE_STANDARD)])])
    rw = analytics.recent_workouts(conn, count=5)
    entry = rw["workouts"][0]
    assert entry["activity_type"] == "strength"
    assert "metrics" not in entry
    assert entry["exercises"][0]["exercise"] == "Squat"
