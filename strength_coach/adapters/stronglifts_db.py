"""Adapter for a StrongLifts-family app SQLite backup.

Shape (verified against a real multi-year backup): history (one row per workout)
joined to history_exercises (one row per set) joined to exercises (names).
Weight is stored in both kg and lb; we take lb directly. ``type==1`` marks
warmups, ``reptype==1`` marks AMRAP/top sets. ``RPE`` is populated but usually
0 (unused in this era); only nonzero RPE becomes RIR.
"""

import os
import sqlite3

from ..models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_FAILURE,
    SET_TYPE_STANDARD,
    SET_TYPE_WARMUP,
)
from .base import epoch_ms_to_iso

SOURCE = "stronglifts"

_QUERY = """
SELECT h.id AS history_id, h.date AS date_ms, h.day_name, h.duration AS dur_min,
       e.exercise_name, he.weightlb, he.reps, he.RPE, he.type, he.reptype,
       he.exercise_number, he.set_number
FROM history h
JOIN history_exercises he ON he.history_id = h.id
JOIN exercises e ON e.id = he.exercise_id
ORDER BY h.date, h.id, he.exercise_number, he.set_number
"""


def _set_type(type_flag, reptype_flag) -> str:
    if type_flag == 1:
        return SET_TYPE_WARMUP
    if reptype_flag == 1:
        return SET_TYPE_FAILURE
    return SET_TYPE_STANDARD


def _rir_from_rpe(rpe):
    # RPE and RIR are inverse on a 10-point scale; RPE 8 -> ~2 RIR. RPE 0 here
    # means "not recorded", not "maximal effort", so treat it as unavailable.
    if rpe is None or rpe <= 0:
        return None
    return max(0.0, 10.0 - float(rpe))


def parse(path: str) -> ParsedImport:
    # Open the attached backup read-only (it's an untrusted upload we only read),
    # matching detect.py — never write to a source file.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sessions: dict[int, NormalizedSession] = {}
    try:
        for row in conn.execute(_QUERY):
            hid = row["history_id"]
            session = sessions.get(hid)
            if session is None:
                session = NormalizedSession(
                    date=epoch_ms_to_iso(row["date_ms"]),
                    source=SOURCE,
                    workout=(row["day_name"] or "").strip() or "Workout",
                    source_session_key=str(hid),
                    duration_s=(row["dur_min"] * 60) if row["dur_min"] else None,
                )
                sessions[hid] = session
            session.sets.append(
                NormalizedSet(
                    exercise_raw=row["exercise_name"],
                    seq=len(session.sets),
                    set_type=_set_type(row["type"], row["reptype"]),
                    weight_lb=row["weightlb"],
                    reps=row["reps"],
                    rir=_rir_from_rpe(row["RPE"]),
                )
            )
    finally:
        conn.close()
    return ParsedImport(
        source=SOURCE, filename=os.path.basename(path), sessions=list(sessions.values())
    )
