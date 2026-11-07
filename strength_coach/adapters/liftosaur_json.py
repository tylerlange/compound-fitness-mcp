"""Adapter for a Liftosaur app JSON export.

Shape (verified against a real export): a top-level dict with a `history` list,
one record per workout (`vtype: "history_record"`), each with `date` (ISO
timestamp), `dayName`/`programName`, `startTime`/`endTime` (epoch ms), and
`entries` (one per exercise) holding `warmupSets` + `sets`, each set carrying
`completedWeight` (value+unit) and `completedReps` — the actually-performed
numbers, distinct from the programmed `weight`/`reps`. No RPE/RIR in this
export. `isCompleted: false` sets are planned-but-skipped and excluded.
"""

import os

from ..models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_FAILURE,
    SET_TYPE_STANDARD,
    SET_TYPE_WARMUP,
    kg_to_lb,
)

SOURCE = "liftosaur"


def _weight_lb(w):
    if not w or w.get("value") is None:
        return None
    return kg_to_lb(w["value"]) if w.get("unit") == "kg" else w["value"]


def _sets_from(raw_sets, set_type):
    out = []
    for s in raw_sets or []:
        if not s.get("isCompleted", True):
            continue
        stype = SET_TYPE_FAILURE if s.get("isAmrap") else set_type
        out.append(NormalizedSet(
            exercise_raw="",  # filled by caller (shared across a set list)
            seq=0,  # renumbered by caller
            set_type=stype,
            weight_lb=_weight_lb(s.get("completedWeight")),
            reps=s.get("completedReps"),
            rir=None,
        ))
    return out


def parse(path: str) -> ParsedImport:
    import json

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    sessions = []
    for h in data.get("history", []):
        date_iso = str(h.get("date", ""))[:10]
        if not date_iso:
            continue
        workout = h.get("dayName") or h.get("programName") or "Workout"
        start, end = h.get("startTime"), h.get("endTime")
        duration_s = int((end - start) / 1000) if start and end else None

        session = NormalizedSession(
            date=date_iso,
            source=SOURCE,
            workout=workout,
            source_session_key=str(h.get("id", "")),
            duration_s=duration_s,
        )
        for entry in h.get("entries", []):
            name = (entry.get("exercise") or {}).get("name", "")
            sets = (_sets_from(entry.get("warmupSets"), SET_TYPE_WARMUP)
                    + _sets_from(entry.get("sets"), SET_TYPE_STANDARD))
            for s in sets:
                s.exercise_raw = name
                s.seq = len(session.sets)
                session.sets.append(s)
        # skip started-but-abandoned records (no completed sets) — they aren't
        # training days and would inflate consistency
        if session.sets:
            sessions.append(session)

    return ParsedImport(source=SOURCE, filename=os.path.basename(path), sessions=sessions)
