"""Query-time analytics over the normalized store.

Everything here is a pure read: derived numbers are computed on demand, never
stored, so the math can evolve without reprocessing history. Two strength
representations are kept deliberately distinct (by design): the *literal* weight
actually lifted, and an *estimated* 1RM (a formula, not a real lift).

Deliberately NOT here in v1: deload/phase inference and other heuristics with
no test oracle. They wait until there's trusted data to validate them against.
"""

import difflib
import json
import sqlite3
from datetime import date, timedelta
from typing import Optional

from .canonical import normalize

# Activity types that count as strength training (the schema default, and
# every existing adapter's output). Consistency's #1-KPI status assumes this
# set unless a caller explicitly asks for something broader -- see
# training_consistency/training_overview's `activity_types` param. Frozen so
# it's safe to use as a mutable-default-lookalike without the usual footgun.
STRENGTH_TYPES = frozenset({"strength"})

# Non-strength activity_type buckets a wearable exercise record can land in. Used by cardio_summary to select
# "everything that isn't strength" without hardcoding the bucket list twice.
CARDIO_TYPES = frozenset({"walk", "run", "hike", "bike", "spin", "cardio"})

# Metrics are stored SI (meters); the coach speaks imperial. Convert at the
# tool boundary so the model never has to (and never mis-converts).
_M_PER_MI = 1609.344
_FT_PER_M = 3.28084

# Epley reliability ceiling: 1RM estimates from sets above this rep count are
# unreliable, so they're excluded from "best estimated 1RM" selection.
E1RM_MAX_REPS = 12
# An exercise is treated as bodyweight/rep-based when few of its sets carry load.
_BODYWEIGHT_WEIGHTED_FRACTION = 0.3

# Dual-implement lifts (a dumbbell in each hand, or a cable in each hand) whose
# logged weight can mean either per-hand OR combined total. There is NO cleanly
# observable "switch date" in the data: within a period the numbers progress
# smoothly and are real; the usual evidence for total-load logging is that
# recent dumbbell values exceed the per-hand cap in the user's gym.md, so they
# can't be per-hand. The genuine risk is CROSS-YEAR comparison — older rows may have been
# per-hand while later ones are total. We can't reconstruct which convention any
# single row used, so the store keeps the raw value, flags the exercise, and
# callers suppress the cross-year trend number (keeping within-window figures).
#
# Classification is name-based and tuned against real exercise lists: the
# markers catch dumbbell and dual-CABLE lifts (cable fly/crossover — one cable
# per hand), while the single-implement exclusions drop lifts that are unilateral
# or use one bell held in both hands (per-hand == total, so no ambiguity). Rope
# "face pull" is single-implement and deliberately NOT matched. Extend the marker
# / exclusion sets when new exercises appear; substring matching alone is wrong
# in both directions (misses dual-cable, over-fires on one-arm/pullover).
_DUAL_IMPLEMENT_MARKERS = ("dumbbell", "dual cable", "cable fly", "cable crossover")
_SINGLE_IMPLEMENT_MARKERS = ("one arm", "single arm", "1 arm", "pullover")
_WEIGHT_CONVENTION_CAVEAT = (
    "Dumbbell/dual-cable lift: recent weight may be logged as combined total "
    "load (check whether values exceed the per-hand dumbbell cap in the gym "
    "profile; if they do, they can't be per-hand). Raw "
    "magnitudes -- especially compared across years -- may mix per-hand vs total "
    "conventions and are not reliably comparable. Within a consistent recent "
    "period the progression is real; do not present a cross-year change as a "
    "strength jump."
)


def is_dual_implement(exercise_name: str) -> bool:
    """Does this exercise load two independent implements (dumbbell-per-hand or
    cable-per-hand), making its logged weight ambiguous between per-hand and
    combined-total? Name-based, tuned against the real exercise list. Unilateral
    or single-bell lifts (one arm / pullover) return False — for them per-hand
    equals total, so there is no ambiguity."""
    n = (exercise_name or "").lower()
    if any(m in n for m in _SINGLE_IMPLEMENT_MARKERS):
        return False
    return any(m in n for m in _DUAL_IMPLEMENT_MARKERS)


def estimate_1rm(weight_lb: float, reps: int, rir: Optional[float] = None) -> float:
    """RIR-adjusted Epley estimate. Effective reps-to-failure = reps + RIR, so a
    set left with reps in reserve estimates a higher 1RM than the same weight x
    reps taken to failure. When RIR is unknown it's treated as 0 (i.e. plain
    Epley, assuming the set was near failure) — an estimate, clearly labeled as
    such wherever surfaced."""
    if weight_lb is None or reps is None:
        return 0.0
    effective = reps + (rir or 0)
    return round(weight_lb * (1 + effective / 30.0), 1)


def find_exercise(conn: sqlite3.Connection, name: str) -> Optional[str]:
    """Resolve a user/model-supplied name to a stored canonical name. Tries, for
    the query AND its loose singular ("squats" -> "squat"): (1) exact
    case-insensitive canonical match; (2) normalized-form match
    (punctuation/spacing/case) against canonical names AND known aliases. Returns
    None otherwise, so callers surface `suggest_exercises` as a did-you-mean.

    Deliberately does NOT resolve by substring or fuzzy match: both bind the
    WRONG lift too easily. "rdl" is a unique substring of "Single Leg RDL" (not
    of "Romanian Deadlift"), and "dumbbell curl" of "Incline Dumbbell Curl" -- a
    confident wrong number is worse than a no_match, and legacy tool-calling
    can't re-call in-turn to recover. Depluralize + exact/normalized still
    resolves the common loose cases ("squats" -> "Squat") in one shot without
    that risk; fuzzy stays in suggest_exercises for did-you-mean only."""
    if not name or not name.strip():
        return None
    id_to_canon = {r["id"]: r["canonical_name"]
                   for r in conn.execute("SELECT id, canonical_name FROM exercises")}
    canon_by_lower = {c.lower(): c for c in id_to_canon.values()}
    norm_index: dict[str, int] = {normalize(c): eid for eid, c in id_to_canon.items()}
    for r in conn.execute("SELECT alias, exercise_id FROM exercise_aliases"):
        norm_index.setdefault(normalize(r["alias"]), r["exercise_id"])
    stripped = name.strip()
    variants = [stripped]
    # try both directions across a trailing plural "s" -- canonicals are a
    # mix of singular and plural ("Squat" vs "Calf Raises")
    if stripped.lower().endswith("s"):
        variants.append(stripped[:-1])
    else:
        variants.append(stripped + "s")
    for cand in variants:
        exact = canon_by_lower.get(cand.lower())
        if exact:
            return exact
        hit = norm_index.get(normalize(cand))
        if hit in id_to_canon:
            return id_to_canon[hit]
    return None


def suggest_exercises(conn: sqlite3.Connection, name: str, n: int = 5) -> list[str]:
    """Nearest tracked exercise names for a miss — substring hits first, then
    fuzzy on the normalized form."""
    all_names = [r["canonical_name"] for r in conn.execute(
        "SELECT canonical_name FROM exercises")]
    lower = name.lower()
    # match the query and its loose singular (so 'squats' finds 'Barbell Back
    # Squat', 'dips' finds 'Dips')
    variants = {lower, lower[:-1]} if lower.endswith("s") else {lower}
    subs = [x for x in all_names if any(v and v in x.lower() for v in variants)]
    norm_map = {normalize(x): x for x in all_names}
    near = [norm_map[k] for k in difflib.get_close_matches(
        normalize(name), list(norm_map), n=n, cutoff=0.5)]
    out: list[str] = []
    for x in subs + near:
        if x not in out:
            out.append(x)
    return out[:n]


def fetch_sets(
    conn: sqlite3.Connection,
    exercise: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    include_warmups: bool = False,
) -> list[dict]:
    """Flexible read the query tool exposes: working sets (optionally an
    exercise / date window). Exercise match is case-insensitive. The model
    reasons over the returned rows."""
    q = [
        """SELECT s.date, s.source, s.workout, e.canonical_name AS exercise,
                  st.set_type, st.weight_lb, st.reps, st.rir, st.seq
           FROM sets st
           JOIN sessions s ON s.id = st.session_id
           LEFT JOIN exercises e ON e.id = st.exercise_id
           WHERE 1=1"""
    ]
    params: list = []
    if not include_warmups:
        q.append("AND st.set_type != 'warmup'")
    if exercise:
        q.append("AND lower(e.canonical_name) = lower(?)")
        params.append(exercise)
    if start:
        q.append("AND s.date >= ?")
        params.append(start)
    if end:
        q.append("AND s.date <= ?")
        params.append(end)
    q.append("ORDER BY s.date, s.workout, st.seq")
    return [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]


def list_tracked_exercises(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT e.canonical_name AS exercise, COUNT(*) AS n_sets,
                  MIN(s.date) AS first_date, MAX(s.date) AS last_date
           FROM sets st
           JOIN sessions s ON s.id = st.session_id
           JOIN exercises e ON e.id = st.exercise_id
           WHERE st.set_type != 'warmup'
           GROUP BY e.canonical_name
           ORDER BY n_sets DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def list_imports(conn: sqlite3.Connection) -> list[dict]:
    """Every import batch, newest first — so a bad import's id can be found and
    passed to remove_import."""
    rows = conn.execute(
        """SELECT id, source, filename, imported_at, session_count, set_count
           FROM imports ORDER BY id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def _is_bodyweight(sets: list[dict]) -> bool:
    if not sets:
        return False
    weighted = sum(1 for s in sets if s["weight_lb"])
    return weighted < len(sets) * _BODYWEIGHT_WEIGHTED_FRACTION


def _best_e1rm_set(sets: list[dict]) -> Optional[dict]:
    """Highest estimated-1RM set, considering only reps <= E1RM_MAX_REPS so a
    high-rep back-off set can't masquerade as a strength peak."""
    candidates = [
        s for s in sets
        if s["weight_lb"] and s["reps"] and s["reps"] <= E1RM_MAX_REPS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: estimate_1rm(
        s["weight_lb"], s["reps"], s["rir"]))


def exercise_summary(
    conn: sqlite3.Connection,
    exercise: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict:
    """Best performance for one exercise over an optional window. Weighted lifts
    report literal heaviest + estimated 1RM (from <=12-rep sets); bodyweight/
    rep-based lifts report best reps (and heaviest added load, if any)."""
    canonical = find_exercise(conn, exercise)
    if canonical is None:
        return {"exercise": exercise, "n_sets": 0, "no_match": True,
                "did_you_mean": suggest_exercises(conn, exercise)}

    sets = fetch_sets(conn, exercise=canonical, start=start, end=end)
    if not sets:
        return {"exercise": canonical, "n_sets": 0,
                "note": "no sets in this date range"}

    out = {
        "exercise": canonical,
        "n_sets": len(sets),
        "date_range": [sets[0]["date"], sets[-1]["date"]],
        "is_bodyweight": _is_bodyweight(sets),
    }
    if is_dual_implement(canonical):
        out["weight_convention_caveat"] = _WEIGHT_CONVENTION_CAVEAT

    if out["is_bodyweight"]:
        best_reps = max(sets, key=lambda s: (s["reps"] or 0))
        out["best_reps"] = best_reps["reps"]
        out["best_reps_detail"] = {
            "reps": best_reps["reps"], "added_weight_lb": best_reps["weight_lb"],
            "date": best_reps["date"]}
        weighted = [s for s in sets if s["weight_lb"]]
        if weighted:
            hw = max(weighted, key=lambda s: s["weight_lb"])
            out["heaviest_added_weight_lb"] = hw["weight_lb"]
            out["heaviest_added_weight_detail"] = {
                "weight_lb": hw["weight_lb"], "reps": hw["reps"], "date": hw["date"]}
        out["note"] = "bodyweight/rep-based; progress tracked by reps, not load"
        return out

    heaviest = max(sets, key=lambda s: (s["weight_lb"] or 0))
    out["heaviest_weight_lb"] = heaviest["weight_lb"]
    out["heaviest_weight_detail"] = {
        "weight_lb": heaviest["weight_lb"], "reps": heaviest["reps"],
        "date": heaviest["date"]}
    best = _best_e1rm_set(sets)
    if best is not None:
        out["best_est_1rm_lb"] = estimate_1rm(best["weight_lb"], best["reps"], best["rir"])
        out["best_est_1rm_detail"] = {
            "weight_lb": best["weight_lb"], "reps": best["reps"], "rir": best["rir"],
            "date": best["date"],
            "note": "estimated (RIR-adjusted Epley, <=12-rep sets only), "
                    "not an actual 1RM lift"}
    return out


def compare_periods(
    conn: sqlite3.Connection, exercise: str,
    period_a: tuple[str, str], period_b: tuple[str, str],
) -> dict:
    """Bests for the same exercise across two date windows — the 2026-vs-2019
    workhorse. Cross-source by construction, thanks to canonicalization."""
    return {
        "exercise": exercise,
        "period_a": {"window": list(period_a),
                     **exercise_summary(conn, exercise, *period_a)},
        "period_b": {"window": list(period_b),
                     **exercise_summary(conn, exercise, *period_b)},
    }


def _best_per_session_series(sets: list[dict], bodyweight: bool) -> list[float]:
    """Best value per training date, oldest first: estimated 1RM for weighted
    lifts (from <=12-rep sets), max reps for bodyweight lifts."""
    by_date: dict[str, float] = {}
    for s in sets:
        if bodyweight:
            val = s["reps"] or 0
        elif s["reps"] and s["reps"] <= E1RM_MAX_REPS:
            val = estimate_1rm(s["weight_lb"], s["reps"], s["rir"])
        else:
            val = 0
        by_date[s["date"]] = max(by_date.get(s["date"], 0), val)
    return [by_date[d] for d in sorted(by_date) if by_date[d] > 0]


def _direction(series: list[float], deadband: float = 0.03) -> dict:
    """First-third vs last-third mean with a deadband, so noise doesn't read as
    a trend. Returns direction + pct_change, or insufficient_data (<3 points)."""
    if len(series) < 3:
        return {"direction": "insufficient_data", "n_sessions": len(series)}
    third = max(1, len(series) // 3)
    early = sum(series[:third]) / third
    late = sum(series[-third:]) / third
    change = (late - early) / early if early else 0
    direction = "stable"
    if change > deadband:
        direction = "increasing"
    elif change < -deadband:
        direction = "decreasing"
    return {"direction": direction, "pct_change": round(change * 100, 1),
            "n_sessions": len(series)}


def trend(
    conn: sqlite3.Connection, exercise: str, deadband: float = 0.03
) -> dict:
    """Direction of best-per-session performance over the whole history:
    estimated 1RM for weighted lifts, max reps for bodyweight lifts."""
    canonical = find_exercise(conn, exercise)
    if canonical is None:
        return {"exercise": exercise, "direction": "no_match",
                "did_you_mean": suggest_exercises(conn, exercise)}
    sets = fetch_sets(conn, exercise=canonical)
    bodyweight = _is_bodyweight(sets)
    metric = "reps" if bodyweight else "est_1rm"
    series = _best_per_session_series(sets, bodyweight)
    out = {"exercise": canonical, "metric": metric, **_direction(series, deadband)}
    if is_dual_implement(canonical):
        # Whole-history trend spans possible per-hand<->total convention changes,
        # so the cross-year pct_change is meaningless. Mark it not_comparable and
        # drop the number rather than emit-and-caveat. (Bounded within-window
        # figures in training_overview are kept — those are within a period.)
        out["direction"] = "not_comparable"
        out.pop("pct_change", None)
        out["weight_convention_caveat"] = _WEIGHT_CONVENTION_CAVEAT
    return out


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())  # Monday of that week


def training_consistency(
    conn: sqlite3.Connection, weeks: int = 12, today: Optional[str] = None,
    activity_types: Optional[list] = STRENGTH_TYPES,
) -> dict:
    """Consistency metrics — the primary KPI. Counts distinct training *days*
    (a date with any logged session), so multiple sessions in a day count once.

    ``activity_types`` scopes which sessions count: omit it (the default) for
    the strength-only number that has always been the #1 KPI -- now that
    cardio sessions exist (wearable), leaving this unfiltered by default
    would silently let a walk inflate the lifting streak. Pass ``None``
    explicitly for the all-activity number instead (every logged session
    type). Pass an explicit list for anything narrower. Coach-facing callers
    report BOTH numbers -- see get_context in agent/mcp_server.py.
    """
    ref = date.fromisoformat(today) if today else date.today()
    q = "SELECT DISTINCT date FROM sessions"
    params: list = []
    if activity_types is not None:
        placeholders = ",".join("?" * len(activity_types))
        q += f" WHERE activity_type IN ({placeholders})"
        params.extend(activity_types)
    days = sorted({
        date.fromisoformat(r["date"]) for r in conn.execute(q, params)
    })
    if not days:
        return {"status": "no_data"}

    away = [
        (date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
        for r in conn.execute("SELECT start_date, end_date FROM away_periods")
    ]

    def in_away(d: date) -> bool:
        return any(s <= d <= e for s, e in away)

    def week_away(wk: date) -> bool:
        # any overlap excuses a week — an *inactive* week touched by travel is
        # bridged, not penalized (an active week counts regardless, since the
        # streak logic checks "active" first). This forgives the partial edge
        # weeks of a multi-day trip, which is the whole point.
        return any(in_away(wk + timedelta(days=i)) for i in range(7))

    last = days[-1]
    days_since_last = (ref - last).days
    active_weeks = {_week_start(d) for d in days}

    # current streak: walk back from the last active week; an active week counts,
    # an away week is skipped (bridges without counting or breaking), a genuine
    # miss ends it
    cur = 0
    w = _week_start(last)
    while True:
        if w in active_weeks:
            cur += 1
            w -= timedelta(weeks=1)
        elif week_away(w):
            w -= timedelta(weeks=1)
        else:
            break

    # longest streak over the training span, away weeks bridging runs
    longest = run = 0
    w = _week_start(days[0])
    end_w = _week_start(last)
    while w <= end_w:
        if w in active_weeks:
            run += 1
            longest = max(longest, run)
        elif not week_away(w):
            run = 0
        w += timedelta(weeks=1)

    this_week = _week_start(ref)
    per_week = {}
    for d in days:
        per_week[_week_start(d)] = per_week.get(_week_start(d), 0) + 1
    weekly_series = [
        {"week_of": (wk := this_week - timedelta(weeks=i)).isoformat(),
         "days_trained": per_week.get(wk, 0), "away": week_away(wk)}
        for i in range(weeks - 1, -1, -1)
    ]

    days_trained_last_28 = sum(1 for d in days if (ref - d).days < 28)
    # average over non-away weeks only, so a vacation doesn't drag it down
    non_away_recent = sum(
        1 for i in range(4) if not week_away(this_week - timedelta(weeks=i)))
    avg = round(days_trained_last_28 / non_away_recent, 1) if non_away_recent else None

    return {
        "as_of": ref.isoformat(),
        "last_session": last.isoformat(),
        "days_since_last": days_since_last,
        "currently_away": in_away(ref),
        "current_streak_weeks": cur,
        "longest_streak_weeks": longest,
        "days_trained_last_28": days_trained_last_28,
        "avg_days_per_week_last_28": avg,
        "weekly": weekly_series,
        "away_periods": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in away],
        "activity_types": (
            sorted(activity_types) if activity_types is not None else "all"),
        "note": (
            ("strength sessions only" if activity_types is not None
             and set(activity_types) == set(STRENGTH_TYPES)
             else "all logged activity types (strength + cardio)"
             if activity_types is None
             else f"filtered to activity_types={sorted(activity_types)}")
            + "; weeks inside an away period are excluded so travel doesn't "
              "penalize."
        ),
    }


def _top_set(sets: list[dict]) -> Optional[dict]:
    """Heaviest working set (by literal weight, then reps) from a list."""
    working = [s for s in sets if s["set_type"] != "warmup"]
    if not working:
        return None
    return max(working, key=lambda s: (s["weight_lb"] or 0, s["reps"] or 0))


def session_readout(
    conn: sqlite3.Connection, session_date: Optional[str] = None
) -> dict:
    """Everything needed to coach a just-imported session in ONE call — so the
    model never has to chain follow-up tool calls to give a real readout.

    For the given date (default: the most recent training date), returns the
    session(s) trained and, per exercise: this session's top working set (literal
    + estimated 1RM), the all-time best for context, and the whole-history trend
    direction. Also bundles the current consistency snapshot (the lead metric)
    and flags any dual-implement lifts whose weights aren't comparable over time.
    """
    last_row = conn.execute("SELECT MAX(date) AS d FROM sessions").fetchone()
    if last_row is None or last_row["d"] is None:
        return {"status": "no_data"}
    session_date = session_date or last_row["d"]

    # Query sessions directly (not just fetch_sets) so a cardio-only day --
    # zero rows in `sets` by design -- isn't reported as no_session_on_date.
    session_rows = conn.execute(
        "SELECT id, activity_type, workout, duration_s FROM sessions WHERE date=?",
        (session_date,),
    ).fetchall()
    if not session_rows:
        return {"status": "no_session_on_date", "date": session_date}

    day_sets = fetch_sets(conn, start=session_date, end=session_date)
    workouts = sorted(
        {s["workout"] for s in day_sets if s["workout"]}
        | {r["workout"] for r in session_rows if r["workout"]})
    by_ex: dict[str, list[dict]] = {}
    for s in day_sets:
        by_ex.setdefault(s["exercise"], []).append(s)

    exercises = []
    caveats = []
    for name, sets in by_ex.items():
        top = _top_set(sets)
        best_set = _best_e1rm_set(sets)
        entry = {
            "exercise": name,
            "sets_this_session": len([s for s in sets if s["set_type"] != "warmup"]),
            "top_set": None if top is None else {
                "weight_lb": top["weight_lb"], "reps": top["reps"],
                "rir": top["rir"], "set_type": top["set_type"]},
            "session_best_est_1rm_lb": None if best_set is None else estimate_1rm(
                best_set["weight_lb"], best_set["reps"], best_set["rir"]),
        }
        # all-time context + direction (whole history)
        summary = exercise_summary(conn, name)
        if not summary.get("no_match"):
            entry["all_time_best_weight_lb"] = summary.get("heaviest_weight_lb")
            entry["all_time_best_est_1rm_lb"] = summary.get("best_est_1rm_lb")
            entry["all_time_best_reps"] = summary.get("best_reps")
        entry["trend"] = trend(conn, name).get("direction")
        if is_dual_implement(name):
            entry["dual_implement"] = True
            if name not in caveats:
                caveats.append(name)
        exercises.append(entry)

    # Non-strength sessions on this date (e.g. a wearable walk/hike) --
    # they carry no `sets`, so render `session_metrics` instead of leaving
    # them invisible / an empty exercises list.
    cardio_sessions = []
    non_strength_ids = [
        r["id"] for r in session_rows if r["activity_type"] not in STRENGTH_TYPES]
    if non_strength_ids:
        placeholders = ",".join("?" * len(non_strength_ids))
        metric_rows = conn.execute(
            f"SELECT session_id, metric, value, unit FROM session_metrics "
            f"WHERE session_id IN ({placeholders})", non_strength_ids,
        ).fetchall()
        metrics_by_session: dict[int, list[dict]] = {}
        for m in metric_rows:
            metrics_by_session.setdefault(m["session_id"], []).append(
                {"metric": m["metric"], "value": m["value"], "unit": m["unit"]})
        for r in session_rows:
            if r["id"] not in non_strength_ids:
                continue
            cardio_sessions.append({
                "activity_type": r["activity_type"],
                "workout": r["workout"],
                "duration_min": (
                    round(r["duration_s"] / 60.0, 1) if r["duration_s"] else None),
                "metrics": metrics_by_session.get(r["id"], []),
            })

    out = {
        "date": session_date,
        "workouts": workouts,
        "consistency": training_consistency(conn),
        "exercises": exercises,
    }
    if cardio_sessions:
        out["cardio_sessions"] = cardio_sessions
    if caveats:
        out["weight_convention_caveat"] = {
            "exercises": caveats, "note": _WEIGHT_CONVENTION_CAVEAT}
    return out


def training_overview(
    conn: sqlite3.Connection, window_days: int = 180, today: Optional[str] = None,
    activity_types: Optional[list] = STRENGTH_TYPES,
) -> dict:
    """A complete "how am I doing / zoom out" answer in ONE call — consistency,
    per-lift direction over the window, gaps, and recent standouts — so the model
    can talk trends without chaining tool calls.

    ``window_days`` bounds the lookback (default 180). Per exercise (those with
    >= 3 sessions in the window) it returns the windowed trend direction plus the
    best literal weight / estimated 1RM / reps inside the window (exercise/set
    data only exists for strength sessions, so this part is unaffected by
    ``activity_types``). It also reports the largest gaps between training days
    (candidate missed stretches, away periods excluded) so "where are the gaps"
    is answerable directly.

    ``activity_types`` scopes the bundled consistency + the day-gap detection
    the same way as ``training_consistency`` (default: strength-only, the #1
    KPI; ``None`` for all logged activity).
    """
    ref = date.fromisoformat(today) if today else date.today()
    start = (ref - timedelta(days=window_days)).isoformat()

    consistency = training_consistency(conn, today=today, activity_types=activity_types)
    away = [
        (date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
        for r in conn.execute("SELECT start_date, end_date FROM away_periods")
    ]

    def spans_away(a: date, b: date) -> bool:
        return any(s <= b and a <= e for s, e in away)

    sets = fetch_sets(conn, start=start, end=ref.isoformat())
    by_ex: dict[str, list[dict]] = {}
    for s in sets:
        by_ex.setdefault(s["exercise"], []).append(s)

    exercises = []
    for name, ex_sets in by_ex.items():
        n_sessions = len({s["date"] for s in ex_sets})
        if n_sessions < 3:
            continue
        bodyweight = _is_bodyweight(ex_sets)
        series = _best_per_session_series(ex_sets, bodyweight)
        direction = _direction(series)
        entry = {
            "exercise": name,
            "n_sessions_in_window": n_sessions,
            "metric": "reps" if bodyweight else "est_1rm",
            "direction": direction.get("direction"),
            "pct_change": direction.get("pct_change"),
        }
        weighted = [s for s in ex_sets if s["weight_lb"]]
        if weighted:
            hw = max(weighted, key=lambda s: s["weight_lb"])
            entry["best_weight_lb_in_window"] = hw["weight_lb"]
        best = _best_e1rm_set(ex_sets)
        if best is not None:
            entry["best_est_1rm_lb_in_window"] = estimate_1rm(
                best["weight_lb"], best["reps"], best["rir"])
        if is_dual_implement(name):
            entry["dual_implement"] = True
            entry["weight_convention_caveat"] = _WEIGHT_CONVENTION_CAVEAT
        exercises.append(entry)

    # sort most-trained first so the model leads with the meaningful lifts
    exercises.sort(key=lambda e: e["n_sessions_in_window"], reverse=True)

    # gaps between consecutive training days in the window (away spans excluded)
    gap_q = "SELECT DISTINCT date FROM sessions WHERE date >= ? AND date <= ?"
    gap_params: list = [start, ref.isoformat()]
    if activity_types is not None:
        gap_placeholders = ",".join("?" * len(activity_types))
        gap_q += f" AND activity_type IN ({gap_placeholders})"
        gap_params.extend(activity_types)
    days = sorted({
        date.fromisoformat(r["date"]) for r in conn.execute(gap_q, gap_params)
    })
    gaps = []
    for a, b in zip(days, days[1:]):
        gap = (b - a).days
        if gap >= 10 and not spans_away(a + timedelta(days=1), b - timedelta(days=1)):
            gaps.append({"from": a.isoformat(), "to": b.isoformat(), "days": gap})
    gaps.sort(key=lambda g: g["days"], reverse=True)

    return {
        "window_days": window_days,
        "as_of": ref.isoformat(),
        "sessions_in_window": len(days),
        "consistency": consistency,
        "exercises": exercises[:20],
        "notable_gaps": gaps[:5],
        "note": (
            ("strength sessions only" if activity_types is not None
             and set(activity_types) == set(STRENGTH_TYPES)
             else "all logged activity types (strength + cardio)"
             if activity_types is None
             else f"filtered to activity_types={sorted(activity_types)}")
            + " for consistency + gaps. Per-lift trends use best-per-session "
              "est. 1RM (weighted) or reps (bodyweight) -- strength sessions "
              "only, since only they carry sets."
        ),
    }


def recent_workouts(conn: sqlite3.Connection, count: int = 12) -> dict:
    """The last `count` logged sessions in full, most recent first: each session's
    date + workout name, and per-exercise working sets (weight/reps/RIR). This is
    the raw material for reviewing a recent training block — accessory selection,
    volume, rep ranges, stalls, missing movements — in ONE call, so the model
    doesn't have to ground (get_context) and then discover it lacks the session
    detail. Warmups excluded; dual-implement lifts flagged (weight may be combined
    total, so don't read their raw load as directly comparable across time)."""
    rows = conn.execute(
        "SELECT id, date, workout, source, activity_type, duration_s FROM sessions "
        "ORDER BY date DESC, id DESC LIMIT ?", (count,)
    ).fetchall()
    if not rows:
        return {"status": "no_data"}
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    sets = conn.execute(
        f"""SELECT st.session_id, e.canonical_name AS exercise,
                   st.weight_lb, st.reps, st.rir, st.seq
            FROM sets st JOIN exercises e ON e.id = st.exercise_id
            WHERE st.session_id IN ({placeholders}) AND st.set_type != 'warmup'
            ORDER BY st.session_id, st.seq""",
        ids,
    ).fetchall()
    by_session: dict[int, list] = {}
    for s in sets:
        by_session.setdefault(s["session_id"], []).append(s)

    metric_rows = conn.execute(
        f"SELECT session_id, metric, value, unit FROM session_metrics "
        f"WHERE session_id IN ({placeholders})", ids,
    ).fetchall()
    metrics_by_session: dict[int, list] = {}
    for m in metric_rows:
        metrics_by_session.setdefault(m["session_id"], []).append(
            {"metric": m["metric"], "value": m["value"], "unit": m["unit"]})

    workouts = []
    for r in rows:
        entry = {"date": r["date"], "workout": r["workout"],
                 "activity_type": r["activity_type"]}
        if r["activity_type"] in STRENGTH_TYPES:
            ex_order: list[str] = []
            ex_map: dict[str, list] = {}
            for s in by_session.get(r["id"], []):
                if s["exercise"] not in ex_map:
                    ex_map[s["exercise"]] = []
                    ex_order.append(s["exercise"])
                ex_map[s["exercise"]].append(
                    {"weight_lb": s["weight_lb"], "reps": s["reps"], "rir": s["rir"]})
            exercises = []
            for name in ex_order:
                ex_entry = {"exercise": name, "sets": ex_map[name]}
                if is_dual_implement(name):
                    ex_entry["dual_implement"] = True
                exercises.append(ex_entry)
            entry["exercises"] = exercises
        else:
            # Non-strength (cardio) session: no `sets` by design -- render
            # duration + session_metrics instead of an empty exercises list.
            entry["duration_min"] = (
                round(r["duration_s"] / 60.0, 1) if r["duration_s"] else None)
            entry["metrics"] = metrics_by_session.get(r["id"], [])
        workouts.append(entry)

    return {
        "count": len(workouts),
        "workouts": workouts,
        "note": "most recent first; warmups excluded. Non-strength sessions "
                "(walks/runs/hikes/other cardio) show duration + metrics "
                "instead of sets -- they carry none. Dual-implement lifts are "
                "flagged (logged as combined total load; don't compare raw weight "
                "across years). Pair with get_context for their goals + gym.",
    }


def program_diff(conn: sqlite3.Connection) -> dict:
    """Structural diff of the two latest MacroFactor program snapshots:
    workouts and exercises added/removed. Deterministic change detection."""
    rows = conn.execute(
        "SELECT structure_json FROM program_snapshots ORDER BY id DESC LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        return {"status": "need_two_snapshots", "snapshots_available": len(rows)}
    newer = json.loads(rows[0]["structure_json"]).get("workouts", {})
    older = json.loads(rows[1]["structure_json"]).get("workouts", {})

    workouts_added = sorted(set(newer) - set(older))
    workouts_removed = sorted(set(older) - set(newer))
    exercise_changes = {}
    for w in set(newer) & set(older):
        added = sorted(set(newer[w]) - set(older[w]))
        removed = sorted(set(older[w]) - set(newer[w]))
        if added or removed:
            exercise_changes[w] = {"added": added, "removed": removed}
    return {
        "status": "ok",
        "workouts_added": workouts_added,
        "workouts_removed": workouts_removed,
        "exercise_changes": exercise_changes,
    }


def cardio_summary(
    conn: sqlite3.Connection, window_days: int = 90, today: Optional[str] = None
) -> dict:
    """Non-strength activity (walks/runs/hikes/bike/other cardio, from a
    wearable) over a trailing
    window: per-activity-type totals (session count, distance, calories,
    duration) plus a combined total and the most recent activity date. Reads
    `session_metrics` -- cardio sessions carry no `sets` (see docs/ARCHITECTURE.md), so this is the cardio-side counterpart to how strength
    numbers come from `sets`.
    """
    ref = date.fromisoformat(today) if today else date.today()
    start = (ref - timedelta(days=window_days)).isoformat()
    placeholders = ",".join("?" * len(STRENGTH_TYPES))
    rows = conn.execute(
        f"""SELECT id, date, activity_type, duration_s FROM sessions
            WHERE date >= ? AND date <= ? AND activity_type NOT IN ({placeholders})
            ORDER BY date""",
        [start, ref.isoformat(), *STRENGTH_TYPES],
    ).fetchall()
    if not rows:
        return {
            "window_days": window_days, "as_of": ref.isoformat(),
            "status": "no_data",
            "note": "no non-strength activity logged in this window",
        }

    ids = [r["id"] for r in rows]
    m_placeholders = ",".join("?" * len(ids))
    metric_rows = conn.execute(
        f"SELECT session_id, metric, value FROM session_metrics "
        f"WHERE session_id IN ({m_placeholders})", ids,
    ).fetchall()
    metrics_by_session: dict[int, dict] = {}
    for m in metric_rows:
        metrics_by_session.setdefault(m["session_id"], {})[m["metric"]] = m["value"]

    by_type: dict[str, dict] = {}
    total_distance_m = 0.0
    total_calories = 0.0
    total_duration_s = 0.0
    total_elevation_gain_m = 0.0
    hr_values: list[float] = []
    for r in rows:
        t = r["activity_type"]
        entry = by_type.setdefault(t, {
            "activity_type": t, "session_count": 0,
            "distance_m": 0.0, "calories": 0.0, "duration_min": 0.0,
            "elevation_gain_m": 0.0,
        })
        entry["session_count"] += 1
        dur = r["duration_s"] or 0
        entry["duration_min"] += round(dur / 60.0, 1)
        total_duration_s += dur
        m = metrics_by_session.get(r["id"], {})
        dist = m.get("distance_m") or 0.0
        cal = m.get("calories") or 0.0
        elev = m.get("elevation_gain_m") or 0.0
        entry["distance_m"] += dist
        entry["calories"] += cal
        entry["elevation_gain_m"] += elev
        total_distance_m += dist
        total_calories += cal
        total_elevation_gain_m += elev
        if m.get("avg_hr"):
            hr_values.append(m["avg_hr"])

    for entry in by_type.values():
        entry["distance_mi"] = round(entry.pop("distance_m") / _M_PER_MI, 2)
        entry["elevation_gain_ft"] = round(
            entry.pop("elevation_gain_m") * _FT_PER_M)
        entry["calories"] = round(entry["calories"], 1)
        entry["duration_min"] = round(entry["duration_min"], 1)

    return {
        "window_days": window_days,
        "as_of": ref.isoformat(),
        "session_count": len(rows),
        "last_activity_date": rows[-1]["date"],
        "by_activity_type": sorted(
            by_type.values(), key=lambda e: -e["session_count"]),
        "total_distance_mi": round(total_distance_m / _M_PER_MI, 2),
        "total_elevation_gain_ft": round(total_elevation_gain_m * _FT_PER_M),
        "total_calories": round(total_calories, 1),
        "total_duration_min": round(total_duration_s / 60.0, 1),
        "avg_heart_rate_bpm": (
            round(sum(hr_values) / len(hr_values), 1) if hr_values else None),
        "note": "non-strength activity (walks/runs/hikes/bike/other cardio); "
                "does not include strength sessions -- pair with "
                "get_training_consistency/get_training_overview for those.",
    }


# Ranking metric -> (session_metric key or None for duration, extractor that
# maps the raw stored value to the imperial number the record is ranked on).
_RECORD_METRICS = {
    "distance": ("distance_m", lambda v: v / _M_PER_MI),      # -> miles
    "elevation": ("elevation_gain_m", lambda v: v * _FT_PER_M),  # -> feet
    "duration": (None, lambda v: v / 60.0),                   # -> minutes
}


def activity_records(
    conn: sqlite3.Connection,
    metric: str = "distance",
    activity_types: Optional[list] = None,
    window_days: Optional[int] = None,
    limit: int = 5,
    today: Optional[str] = None,
) -> dict:
    """Per-session leaderboard for non-strength activity, ranked by ``metric``
    (``distance`` | ``duration`` | ``elevation``). This is the single-session
    counterpart to cardio_summary (which only aggregates): use it to answer
    "what was my longest run/hike" or "biggest elevation day".

    ``activity_types`` filters to specific buckets (e.g. ["run"]); omitted =
    all non-strength activity. ``window_days`` omitted = all-time. Each record
    carries distance_mi/elevation_gain_ft/duration_min so the top result comes
    with full context regardless of which metric it was ranked on. Sessions
    missing the ranked metric are dropped (can't rank a hike with no distance).
    """
    metric = (metric or "distance").lower()
    if metric not in _RECORD_METRICS:
        return {"error": f"unknown metric {metric!r}; "
                          f"use one of {sorted(_RECORD_METRICS)}"}
    ref = date.fromisoformat(today) if today else date.today()

    if activity_types:
        where = f"activity_type IN ({','.join('?' * len(activity_types))})"
        params: list = [*activity_types]
    else:
        where = f"activity_type NOT IN ({','.join('?' * len(STRENGTH_TYPES))})"
        params = [*STRENGTH_TYPES]
    if window_days is not None:
        where += " AND date >= ?"
        params.append((ref - timedelta(days=window_days)).isoformat())
    where += " AND date <= ?"
    params.append(ref.isoformat())

    rows = conn.execute(
        f"SELECT id, date, activity_type, workout, duration_s "
        f"FROM sessions WHERE {where}", params,
    ).fetchall()
    if not rows:
        return {"metric": metric, "as_of": ref.isoformat(),
                "records": [], "status": "no_data"}

    ids = [r["id"] for r in rows]
    metric_rows = conn.execute(
        f"SELECT session_id, metric, value FROM session_metrics "
        f"WHERE session_id IN ({','.join('?' * len(ids))})", ids,
    ).fetchall()
    by_session: dict[int, dict] = {}
    for m in metric_rows:
        by_session.setdefault(m["session_id"], {})[m["metric"]] = m["value"]

    metric_key, to_imperial = _RECORD_METRICS[metric]
    records = []
    for r in rows:
        m = by_session.get(r["id"], {})
        raw = r["duration_s"] if metric_key is None else m.get(metric_key)
        if not raw:  # missing the ranked metric -> not rankable
            continue
        dur_s = r["duration_s"] or 0
        records.append({
            "date": r["date"],
            "activity_type": r["activity_type"],
            "workout": r["workout"],
            "distance_mi": round((m.get("distance_m") or 0) / _M_PER_MI, 2),
            "elevation_gain_ft": round((m.get("elevation_gain_m") or 0) * _FT_PER_M),
            "duration_min": round(dur_s / 60.0, 1),
            "avg_hr_bpm": m.get("avg_hr"),
            "_rank": to_imperial(raw),
        })

    records.sort(key=lambda e: -e["_rank"])
    for e in records:
        e.pop("_rank")
    return {
        "metric": metric,
        "as_of": ref.isoformat(),
        "window_days": window_days,
        "activity_types": sorted(activity_types) if activity_types else "all_cardio",
        "record_count": len(records),
        "records": records[:limit],
    }


def cardio_bests(conn: sqlite3.Connection) -> dict:
    """All-time single-session bests, one per metric (distance/duration/
    elevation), so the coach can proactively recognize a PR without being
    asked -- e.g. congratulate a hike that's the longest ever. Compact by
    design (top 1 each); use activity_records for a fuller leaderboard."""
    out: dict[str, Optional[dict]] = {}
    for metric in _RECORD_METRICS:
        res = activity_records(conn, metric=metric, limit=1)
        recs = res.get("records") or []
        out[metric] = recs[0] if recs else None
    return {"all_time_bests": out}


def recovery_status(
    conn: sqlite3.Connection, window_days: int = 14, today: Optional[str] = None
) -> dict:
    """Recent recovery signals (sleep, resting HR, HRV) from `daily_metrics` --
    latest value + a trailing-window average per metric, so the coach can
    reason about hard-session/deload calls from real data. Deliberately no
    synthetic composite readiness score (by design --
    see docs/ARCHITECTURE.md). Present the raw signals; let the coach (or
    the user) interpret them in context.
    """
    ref = date.fromisoformat(today) if today else date.today()
    start = (ref - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT date, metric, value, unit FROM daily_metrics "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        (start, ref.isoformat()),
    ).fetchall()
    if not rows:
        return {
            "window_days": window_days, "as_of": ref.isoformat(),
            "status": "no_data",
            "note": "no wearable recovery data logged in this window",
        }

    by_metric: dict[str, list[dict]] = {}
    for r in rows:
        by_metric.setdefault(r["metric"], []).append(
            {"date": r["date"], "value": r["value"], "unit": r["unit"]})

    metrics_out = {}
    for metric, series in by_metric.items():
        values = [p["value"] for p in series if p["value"] is not None]
        latest = series[-1]
        metrics_out[metric] = {
            "latest_value": latest["value"],
            "latest_date": latest["date"],
            "unit": latest["unit"],
            "window_avg": round(sum(values) / len(values), 1) if values else None,
            "n_days": len(series),
        }

    return {
        "window_days": window_days,
        "as_of": ref.isoformat(),
        "metrics": metrics_out,
        "note": "raw wearable recovery signals (sleep, resting HR, HRV) -- no "
                "synthetic readiness score. Compare latest vs. window_avg and "
                "look for a trend, not a single day, before calling a hard "
                "session or a deload.",
    }
