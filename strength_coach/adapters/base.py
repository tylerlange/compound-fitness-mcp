"""Shared normalization helpers for adapters."""

import re
from datetime import date, datetime, timedelta, timezone

_HEADER_LEADING_JUNK = re.compile(r"^[^a-z0-9]+")
_MOJIBAKE_MARKERS = ("Â", "Ã", "â€", "ï»¿")


def fix_mojibake(s):
    """Repair UTF-8 text that was decoded as Latin-1 (e.g. '45Â° Incline' ->
    '45° Incline'). Guarded: only attempts repair when a known mojibake marker
    is present and the round-trip succeeds, so clean text is never touched."""
    if not s or not any(m in s for m in _MOJIBAKE_MARKERS):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def normalize_header(h) -> str:
    """Lowercase a spreadsheet header and strip surrounding quotes plus any
    leading junk. Handles the real BOM (\\ufeff) AND the common mojibake form
    'ï»¿' (a UTF-8 BOM mis-decoded as Latin-1), which the real MacroFactor flat
    export carries on its 'Date' column."""
    s = str(h or "").replace('"', "").strip().lower()
    return _HEADER_LEADING_JUNK.sub("", s).strip()

# Excel's epoch is 1899-12-30 (accounting for the 1900 leap-year bug).
_EXCEL_EPOCH = date(1899, 12, 30)
_DURATION_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", re.IGNORECASE)


def excel_serial_to_iso(serial: float) -> str:
    return (_EXCEL_EPOCH + timedelta(days=int(serial))).isoformat()


def epoch_ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def to_iso_date(value) -> str:
    """Coerce a cell value (datetime, Excel serial number, or ISO-ish string)
    to a ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return excel_serial_to_iso(value)
    s = str(value).strip()
    # take the date portion of an ISO-ish 'YYYY-MM-DD HH:MM' string
    return s.split(" ")[0].split("T")[0]


def parse_hm_duration(text: str):
    """'1h 22m' -> seconds. Returns None if nothing parses."""
    if not text:
        return None
    m = _DURATION_RE.match(text.strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    hours = int(m.group(1)) if m.group(1) else 0
    mins = int(m.group(2)) if m.group(2) else 0
    return hours * 3600 + mins * 60


def parse_weight(text: str):
    """'110 lbs' / '50 kg' / '' -> (weight_lb, ok). Converts kg to lb."""
    if text is None:
        return None
    s = str(text).strip().lower()
    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    if not m:
        return None
    val = float(m.group(1))
    if "kg" in s:
        from ..models import kg_to_lb

        return kg_to_lb(val)
    return val
