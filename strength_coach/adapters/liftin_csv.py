"""Adapter for a Liftin flat CSV export.

Columns: Date, Duration, Routine, Exercise, Set, Warmup, Weight, Reps/Time,
Goal, Perception. Sessions group by the full Date timestamp (shared across a
session's rows) plus Routine. Weight is a string like '110 lbs'. Perception
(RIR/RPE) is empty in the real export, so RIR is unavailable.
"""

import csv
import os

from ..models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_STANDARD,
    SET_TYPE_WARMUP,
)
from .base import parse_hm_duration, parse_weight, to_iso_date

SOURCE = "liftin"


def _parse_reps(text: str):
    text = (text or "").strip()
    if not text or ":" in text:  # ':' would be a timed hold, not reps
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse(path: str) -> ParsedImport:
    sessions: dict[tuple, NormalizedSession] = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            raw_date = (row.get("Date") or "").strip()
            routine = (row.get("Routine") or "").strip()
            # full timestamp keeps two same-day sessions distinct
            key = (raw_date, routine)
            session = sessions.get(key)
            if session is None:
                session = NormalizedSession(
                    date=to_iso_date(raw_date),
                    source=SOURCE,
                    workout=routine or "Workout",
                    source_session_key=raw_date,
                    duration_s=parse_hm_duration(row.get("Duration", "")),
                )
                sessions[key] = session
            is_warmup = (row.get("Warmup") or "").strip().lower() == "x"
            session.sets.append(
                NormalizedSet(
                    exercise_raw=(row.get("Exercise") or "").strip(),
                    seq=len(session.sets),
                    set_type=SET_TYPE_WARMUP if is_warmup else SET_TYPE_STANDARD,
                    weight_lb=parse_weight(row.get("Weight")),
                    reps=_parse_reps(row.get("Reps/Time")),
                    rir=None,
                )
            )
    return ParsedImport(
        source=SOURCE, filename=os.path.basename(path), sessions=list(sessions.values())
    )
