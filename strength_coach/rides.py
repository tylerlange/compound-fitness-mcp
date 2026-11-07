"""Ride-level analysis over per-sample power/heart-rate streams.

Sources that expose a per-second (or similar) performance graph, such as a
connected indoor bike or a power meter export, store it in `session_streams`.

Everything here needs `session_streams`; the ride averages in
`session_metrics` cannot produce any of it. Three quantities matter:

* **Normalized power (NP)** -- a 30s-rolling, 4th-power-weighted mean. It is
  what average power would have to be to cost the same physiologically, and it
  diverges from average power exactly when a ride surges. On steady Zone 2 work
  NP/AP sits near 1.02; on an interval ride it can exceed 1.2.
* **Efficiency factor (EF)** -- NP over average heart rate, watts per beat.
  Scale-free within a person, so it tracks aerobic fitness across a block. It
  is *not* comparable between people or between ride types.
* **Pw:HR decoupling** -- how much EF degrades from the first half of the
  steady-state portion to the second. Under ~5% means the intensity is
  genuinely aerobically supported; above it means the rider is spending
  something they can't sustain. This is the metric that still moves during a
  fixed-FTP base block, when EF on zone-capped rides is flat by construction.

Warmup and cooldown are excluded before any of these are computed. Including
them makes every number a function of ride length rather than of the rider,
which silently inflates comparisons between a 30-minute and a 75-minute class.
"""
import json
import sqlite3
from typing import Optional

# NP's defining window. Fixed by the metric, not a tuning knob.
_NP_WINDOW_S = 30

# Main-set detection. A sample belongs to the main set when the 60s rolling
# mean power around it clears this fraction of the ride's median power.
# Structured-class warmups and cooldowns sit far below half of median output, so
# the split is unambiguous; the 60s smoothing keeps a single recovery valley
# inside an interval ride from truncating the set.
_MAIN_SET_FRACTION = 0.5
_MAIN_SET_SMOOTH_S = 60

# Decoupling above this is conventionally read as "not aerobically supported".
DECOUPLING_THRESHOLD_PCT = 5.0

# A ride whose stream spans less than this fraction of its recorded duration is
# treated as unanalyzable. Connected-bike APIs can return a truncated performance
# graph (a dropped connection mid-class) while still reporting whole-ride
# averages, so the session looks complete everywhere except here. Analyzing the
# surviving fragment produces a confident, meaningless EF rather than a gap.
_MIN_STREAM_COVERAGE = 0.8


def load_streams(conn: sqlite3.Connection, session_id: int) -> dict:
    """`{metric: {"sample_s", "offsets", "values"}}` for one session, or {}."""
    rows = conn.execute(
        """SELECT metric, unit, sample_seconds, offsets_json, values_json
           FROM session_streams WHERE session_id = ?""", (session_id,)).fetchall()
    return {
        r["metric"]: {
            "unit": r["unit"],
            "sample_s": r["sample_seconds"],
            "offsets": json.loads(r["offsets_json"]),
            "values": json.loads(r["values_json"]),
        }
        for r in rows
    }


def _rolling_mean(values: list, window: int) -> list:
    """Trailing rolling mean, None-tolerant. Positions whose window holds no
    usable sample come back None rather than 0, so a dropped-sensor stretch
    can't be mistaken for genuine zero output."""
    if window < 1:
        window = 1
    out: list = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def _mean(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def normalized_power(values: list, sample_s: int) -> Optional[float]:
    """NP over a power series. Returns None when the series is too short to
    hold one 30s window, since a partial window would understate the
    4th-power weighting rather than approximate it."""
    if not values or not sample_s or sample_s <= 0:
        return None
    window = max(1, round(_NP_WINDOW_S / sample_s))
    if len(values) < window:
        return None
    rolled = [v for v in _rolling_mean(values, window) if v is not None]
    if not rolled:
        return None
    return (sum(v ** 4 for v in rolled) / len(rolled)) ** 0.25


def main_set_bounds(values: list, sample_s: int) -> Optional[tuple]:
    """`(start, end)` indices of the steady portion, warmup/cooldown trimmed.

    Returns None when no sustained working region is found -- a 1-minute
    "Just Ride" has no main set to speak of, and inventing one would put a
    decoupling number on noise.
    """
    usable = [v for v in values if v is not None]
    if not usable or not sample_s or sample_s <= 0:
        return None
    ordered = sorted(usable)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return None

    threshold = _MAIN_SET_FRACTION * median
    smooth = _rolling_mean(values, max(1, round(_MAIN_SET_SMOOTH_S / sample_s)))
    working = [i for i, v in enumerate(smooth) if v is not None and v >= threshold]
    if len(working) < 2:
        return None
    return working[0], working[-1]


def _slice(stream: Optional[dict], start: int, end: int) -> list:
    if not stream:
        return []
    return stream["values"][start:end + 1]


def ride_analysis(conn: sqlite3.Connection, session_id: int) -> dict:
    """NP, EF, variability index and decoupling for one ride.

    Every field is independently optional: a ride with no HR strap still yields
    NP and VI, and the caller gets None for the HR-derived numbers instead of
    a silently power-only "EF".
    """
    streams = load_streams(conn, session_id)
    power = streams.get("output")
    hr = streams.get("heart_rate")
    out: dict = {
        "session_id": session_id, "np_w": None, "avg_power_w": None,
        "variability_index": None, "avg_hr": None, "efficiency_factor": None,
        "decoupling_pct": None, "main_set_s": None, "sample_s": None,
        "coverage": None, "truncated": False,
    }
    if not power:
        return out

    sample_s = power["sample_s"]
    out["sample_s"] = sample_s

    duration_s = (conn.execute("SELECT duration_s FROM sessions WHERE id = ?",
                               (session_id,)).fetchone() or {})["duration_s"]
    offsets = power["offsets"]
    if duration_s and offsets:
        out["coverage"] = round((offsets[-1] - offsets[0]) / duration_s, 3)
        if out["coverage"] < _MIN_STREAM_COVERAGE:
            out["truncated"] = True
            return out
    bounds = main_set_bounds(power["values"], sample_s)
    if bounds is None:
        return out
    start, end = bounds
    out["main_set_s"] = (end - start + 1) * (sample_s or 0)

    p = _slice(power, start, end)
    np_w = normalized_power(p, sample_s)
    ap = _mean(p)
    out["np_w"] = round(np_w, 1) if np_w is not None else None
    out["avg_power_w"] = round(ap, 1) if ap is not None else None
    if np_w is not None and ap:
        out["variability_index"] = round(np_w / ap, 3)

    h = _slice(hr, start, end)
    mean_hr = _mean(h)
    if mean_hr:
        out["avg_hr"] = round(mean_hr, 1)
        if np_w is not None:
            out["efficiency_factor"] = round(np_w / mean_hr, 4)

    # Decoupling: EF of the first half of the main set vs the second. Split by
    # sample index, which is time here because the series is evenly spaced.
    if h and p and len(p) >= 4:
        mid = len(p) // 2
        halves = []
        for lo, hi in ((0, mid), (mid, len(p))):
            hp, hh = _mean(p[lo:hi]), _mean(h[lo:hi])
            halves.append(hp / hh if (hp is not None and hh) else None)
        if all(halves) and halves[0]:
            out["decoupling_pct"] = round(
                (halves[0] - halves[1]) / halves[0] * 100, 2)
    return out


def ride_series(conn: sqlite3.Connection, workout_like: Optional[str] = None,
                since: Optional[str] = None, limit: int = 50) -> list:
    """`ride_analysis` across matching spin sessions, oldest first.

    `workout_like` exists because EF only means something within one ride type;
    comparing a HIIT ride's EF to an endurance ride's compares the class plan,
    not the rider. Callers are expected to pin it.
    """
    sql = ["SELECT id, date, workout, duration_s FROM sessions",
           "WHERE activity_type = 'spin'"]
    params: list = []
    if workout_like:
        sql.append("AND workout LIKE ?")
        params.append(workout_like)
    if since:
        sql.append("AND date >= ?")
        params.append(since)
    sql.append("ORDER BY date ASC LIMIT ?")
    params.append(limit)

    out = []
    for row in conn.execute(" ".join(sql), params):
        a = ride_analysis(conn, row["id"])
        if a["np_w"] is None or a["truncated"]:
            continue
        a.update({"date": row["date"], "workout": row["workout"],
                  "duration_s": row["duration_s"]})
        out.append(a)
    return out
