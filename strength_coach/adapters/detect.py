"""Deterministic source detection + dispatch.

Routing is by file *content*, never a model guess: misrouting clean data into
the wrong parser would silently corrupt it. Each format has an unambiguous
fingerprint (SQLite table set / xlsx sheet+header / CSV header).
"""

import csv
import json
import sqlite3
import zipfile

from ..models import ParsedImport
from . import (
    alltrails_gpx,
    liftin_csv,
    liftosaur_json,
    macrofactor,
    stronglifts_db,
)
from .base import normalize_header


class UnknownSourceError(ValueError):
    pass


def _is_sqlite(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _sqlite_tables(path: str) -> set:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _xlsx_signature(path: str):
    """(sheetnames, first-sheet-header) or None if not a readable xlsx."""
    if not zipfile.is_zipfile(path):
        return None
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            sheets = list(wb.sheetnames)
            ws = wb[sheets[0]]
            header = [
                normalize_header(h) for h in next(ws.iter_rows(values_only=True))
            ]
            return sheets, header
        finally:
            wb.close()
    except Exception:
        return None


def _csv_header(path: str):
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            return [h.strip().lower() for h in next(csv.reader(f))]
    except (OSError, StopIteration):
        return None


def _is_liftosaur_json(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict) or "history" not in data:
        return False
    history = data["history"]
    return (
        isinstance(history, list)
        and (not history or history[0].get("vtype") == "history_record")
    )


def detect_source(path: str) -> str:
    if _is_liftosaur_json(path):
        return liftosaur_json.SOURCE
    if _is_sqlite(path):
        tables = _sqlite_tables(path)
        if {"history", "history_exercises", "exercises"} <= tables:
            return stronglifts_db.SOURCE
        raise UnknownSourceError(f"SQLite file with unrecognized schema: {path}")

    if alltrails_gpx.is_alltrails_export(path):
        return alltrails_gpx.SOURCE

    sig = _xlsx_signature(path)
    if sig is not None:
        sheets, header = sig
        if macrofactor.WORKOUT_LOG_SHEET in sheets or (
            "set type" in header and "exercise" in header and "reps" in header
        ):
            return macrofactor.SOURCE
        raise UnknownSourceError(f"xlsx with unrecognized layout: {path}")

    header = _csv_header(path)
    if header is not None:
        if {"routine", "exercise", "warmup"} <= set(header):
            return liftin_csv.SOURCE
        raise UnknownSourceError(f"CSV with unrecognized columns: {path}")

    raise UnknownSourceError(f"Could not identify source format: {path}")


_PARSERS = {
    alltrails_gpx.SOURCE: alltrails_gpx.parse,
    macrofactor.SOURCE: macrofactor.parse,
    stronglifts_db.SOURCE: stronglifts_db.parse,
    liftin_csv.SOURCE: liftin_csv.parse,
    liftosaur_json.SOURCE: liftosaur_json.parse,
}


def parse_file(path: str) -> ParsedImport:
    return _PARSERS[detect_source(path)](path)
