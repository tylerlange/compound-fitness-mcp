"""Adapter for MacroFactor Workout exports.

Handles both shapes: the 4-sheet "quick export" (a ``Workout Log`` sheet plus a
jagged ``Active Program`` sheet) and the single-sheet full-history flat export.
Both carry the same Workout Log schema. The ``Date`` header contains a BOM in
the real file; headers are normalized before mapping. The Active Program sheet
is parsed best-effort into a structure snapshot for later change-detection; a
failure there never fails the log import.
"""

import os
import re

import openpyxl

from ..models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_FAILURE,
    SET_TYPE_STANDARD,
    SET_TYPE_WARMUP,
)
from .base import fix_mojibake, normalize_header, to_iso_date

SOURCE = "macrofactor"
WORKOUT_LOG_SHEET = "Workout Log"
ACTIVE_PROGRAM_SHEET = "Active Program"

_SET_TYPE_MAP = {
    "standard set": SET_TYPE_STANDARD,
    "failure set": SET_TYPE_FAILURE,
    "warm-up set": SET_TYPE_WARMUP,
    "warmup set": SET_TYPE_WARMUP,
}


def _map_set_type(raw) -> str:
    s = str(raw or "").strip().lower()
    # collapse unilateral "(l)"/"(r)" variants onto their base type
    s = s.replace("(l)", "").replace("(r)", "").strip()
    return _SET_TYPE_MAP.get(s, SET_TYPE_STANDARD)


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


_KG_TO_LB = 2.2046226218
_UNIT_TO_LB = {"": 1.0, "lb": 1.0, "lbs": 1.0, "kg": _KG_TO_LB, "kgs": _KG_TO_LB}
_WEIGHT_HEADER_RE = re.compile(r"^weight\s*(?:\(\s*([a-z]*)\s*\))?$")
_WEIGHT_CELL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(lbs?|kgs?)?\s*$", re.IGNORECASE)


def _weight_column(header: list) -> tuple:
    """(column index, factor to lb) of the per-set weight column. Matches
    "weight (lbs)" and its kg/unitless variants, never "exercise base weight".

    A missing column raises: Sep 2026 exports imported "ok" with every weight
    NULL for weeks, and the file was deleted on success, so the cause was
    unrecoverable. Raising routes the file to failed/ where it is kept."""
    for i, name in enumerate(header):
        m = _WEIGHT_HEADER_RE.match(name)
        if m and (m.group(1) or "") in _UNIT_TO_LB:
            return i, _UNIT_TO_LB[m.group(1) or ""]
    raise ValueError(f"MacroFactor Workout Log has no recognizable weight column: {header}")


def _weight_lb(v, factor: float):
    """A weight cell -> pounds. Accepts numbers and unit-suffixed strings
    ("80 kg"); a unit on the cell overrides the header's."""
    n = _num(v)
    if n is not None:
        return round(n * factor, 2)
    m = _WEIGHT_CELL_RE.match(str(v or ""))
    if not m:
        return None
    unit = (m.group(2) or "").lower()
    return round(float(m.group(1)) * (_UNIT_TO_LB[unit] if unit else factor), 2)


def _find_log_sheet(wb):
    if WORKOUT_LOG_SHEET in wb.sheetnames:
        return wb[WORKOUT_LOG_SHEET]
    return wb[wb.sheetnames[0]]  # flat export: the only sheet is the log


def _parse_workout_log(ws) -> list:
    rows = ws.iter_rows(values_only=True)
    header = [normalize_header(h) for h in next(rows)]
    idx = {name: i for i, name in enumerate(header)}
    weight_i, weight_factor = _weight_column(header)

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    sessions: dict[tuple, NormalizedSession] = {}
    for row in rows:
        if row is None or all(c is None for c in row):
            continue
        date_iso = to_iso_date(cell(row, "date"))
        workout = str(cell(row, "workout") or "").strip() or "Workout"
        key = (date_iso, workout)
        session = sessions.get(key)
        if session is None:
            session = NormalizedSession(
                date=date_iso,
                source=SOURCE,
                workout=workout,
                duration_s=_int(cell(row, "workout duration")),
            )
            sessions[key] = session
        session.sets.append(
            NormalizedSet(
                exercise_raw=fix_mojibake(str(cell(row, "exercise") or "").strip()),
                seq=len(session.sets),
                set_type=_map_set_type(cell(row, "set type")),
                weight_lb=_weight_lb(
                    row[weight_i] if weight_i < len(row) else None, weight_factor),
                reps=_int(cell(row, "reps")),
                rir=_num(cell(row, "rir")),
            )
        )
    out = list(sessions.values())
    working = [s for sess in out for s in sess.sets if s.set_type != SET_TYPE_WARMUP]
    if working and all(s.weight_lb is None for s in working):
        # A whole export with no loaded working set is a parse failure, not a
        # bodyweight-only fortnight. Fail loudly so the file is kept.
        raise ValueError(
            f"MacroFactor export parsed {len(working)} working sets but no weights")
    return out


def _parse_active_program(ws) -> dict:
    """Best-effort structure snapshot: program metadata + workout -> exercise
    list. Cycle-level target detail is intentionally coarse; the point is
    detecting exercises/days added or removed between imports."""
    rows = list(ws.iter_rows(values_only=True))
    meta = {}
    for cell in rows[0] if rows else []:
        if isinstance(cell, str) and ":" in cell:
            k, _, v = cell.partition(":")
            meta[k.strip().lower()] = v.strip()

    workouts: dict[str, list] = {}
    current_workout = None
    for row in rows[1:]:
        if not row:
            continue
        marker = str(row[0] or "").strip() if len(row) > 0 else ""
        exercise = str(row[1] or "").strip() if len(row) > 1 else ""
        if marker.lower().startswith("workout"):
            current_workout = marker
            workouts.setdefault(current_workout, [])
        if exercise and exercise.lower() != "exercise" and current_workout:
            if exercise not in workouts[current_workout]:
                workouts[current_workout].append(exercise)

    return {
        "program_name": meta.get("program"),
        "cycles": meta.get("cycles"),
        "deload": meta.get("deload"),
        "workouts": workouts,
    }


def parse(path: str) -> ParsedImport:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sessions = _parse_workout_log(_find_log_sheet(wb))
        snapshot = None
        if ACTIVE_PROGRAM_SHEET in wb.sheetnames:
            try:
                snapshot = _parse_active_program(wb[ACTIVE_PROGRAM_SHEET])
            except Exception:
                snapshot = None  # auxiliary; never fail the log import
    finally:
        wb.close()
    return ParsedImport(
        source=SOURCE,
        filename=os.path.basename(path),
        sessions=sessions,
        program_snapshot=snapshot,
    )
