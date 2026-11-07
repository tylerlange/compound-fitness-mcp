"""AllTrails GPX activity adapter.

Parses the *recorded* hikes ("recordings/*.gpx") out of an AllTrails account
data export into cardio ``NormalizedSession``s. Two input shapes are accepted:

  1. the full AllTrails export zip -- recordings live inside a nested
     "User Online Activity/Activities*.zip"; and
  2. that nested Activities zip on its own -- "recordings/" at the top level.

Saved trail *routes* ("maps/*.gpx") are bookmarks the user never necessarily
walked, so they are ignored -- only "recordings/" (actually-tracked activities)
become sessions. Distances/elevations are derived from the GPS track and stored
SI (meters), the store-wide convention; the coach converts to
imperial at read time.

Two things the raw GPS track needs corrected before it yields sane numbers:

* Elevation gain -- GPS altitude is noisy, so naively summing every positive
  tick inflates gain ~2x. We smooth elevation over a short *time* window
  (sampling-rate independent) and only count sustained rises past a threshold.
  This is still a GPS estimate: AllTrails' own figure comes from a terrain
  model and will read somewhat lower.
* Duration -- summing raw elapsed time counts paused segments (recording left
  running while stopped). We sum inter-point intervals but drop gaps longer
  than a pause threshold, which tracks AllTrails' displayed time closely.

Dedup key: AllTrails appends a stable short id hash to each recording filename
(``...-44cab12.gpx``); that hash is the ``source_session_key`` so re-exporting
and re-importing updates activities in place instead of duplicating them.
"""

import io
import math
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET

from ..models import NormalizedSession, ParsedImport

SOURCE = "alltrails"
ACTIVITY_TYPE = "hike"  # one of analytics.CARDIO_TYPES

_GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}
# AllTrails filenames end with a dash + short hex track id, e.g. "-44cab12.gpx".
_ID_RE = re.compile(r"-([0-9a-f]{6,})\.gpx$", re.IGNORECASE)
_EARTH_R_M = 6371000.0

# Elevation smoothing / gain thresholds and the pause-gap cutoff. Tuned against
# a recorded hike whose AllTrails distance, gain and moving time were known.
_ELE_SMOOTH_HALF_S = 30.0   # moving-average half-window (=> ~60s window)
_ELE_THRESHOLD_M = 3.0      # ignore rises smaller than this (GPS jitter)
_PAUSE_GAP_S = 60.0         # inter-point gap above this = paused, not counted


def _norm(name: str) -> str:
    return name.replace("\\", "/").lower()


def _is_recording(name: str) -> bool:
    n = _norm(name)
    return n.endswith(".gpx") and "/recordings/" in ("/" + n)


def _is_nested_activities_zip(name: str) -> bool:
    n = _norm(name)
    return n.endswith(".zip") and n.split("/")[-1].startswith("activities")


def is_alltrails_export(path: str) -> bool:
    """Content fingerprint: a zip that carries recorded GPX activities, either
    directly ("recordings/*.gpx") or via a nested AllTrails "Activities" zip.
    A real xlsx is also a zip but matches neither, so routing stays unambiguous.
    """
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return any(_is_recording(n) for n in names) or any(
        _is_nested_activities_zip(n) for n in names
    )


def _iter_recording_gpx(path: str):
    """Yield (arcname, gpx_bytes) for every recordings/*.gpx, descending into
    the nested Activities zip when handed the full export."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        direct = [n for n in names if _is_recording(n)]
        if direct:
            for n in direct:
                yield n, zf.read(n)
            return
        for n in names:
            if _is_nested_activities_zip(n):
                with zipfile.ZipFile(io.BytesIO(zf.read(n))) as inner:
                    for m in inner.namelist():
                        if _is_recording(m):
                            yield m, inner.read(m)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _moving_seconds(times: list, gap_cap: float = _PAUSE_GAP_S) -> int:
    """Sum inter-point intervals, dropping gaps longer than ``gap_cap`` (the
    recording was paused/stopped). Approximates AllTrails' displayed time far
    better than raw first->last elapsed."""
    total = 0.0
    for a, b in zip(times, times[1:]):
        dt = (b - a).total_seconds()
        if 0 < dt <= gap_cap:
            total += dt
    return int(total)


def _smoothed_gain_m(
    samples: list,
    half_s: float = _ELE_SMOOTH_HALF_S,
    threshold_m: float = _ELE_THRESHOLD_M,
) -> float:
    """Total elevation gain from ``[(t_seconds, ele_m), ...]`` (time-ordered).
    Smooths elevation over a +/-``half_s`` time window to kill GPS jitter, then
    accumulates only sustained rises past ``threshold_m``."""
    if len(samples) < 2:
        return 0.0
    ts = [s[0] for s in samples]
    el = [s[1] for s in samples]
    n = len(samples)
    sm = []
    j0 = j1 = 0
    for i in range(n):
        while ts[i] - ts[j0] > half_s:
            j0 += 1
        if j1 < i:
            j1 = i
        while j1 + 1 < n and ts[j1 + 1] - ts[i] <= half_s:
            j1 += 1
        window = el[j0:j1 + 1]
        sm.append(sum(window) / len(window))
    gain = 0.0
    ref = sm[0]
    for e in sm[1:]:
        if e - ref >= threshold_m:
            gain += e - ref
            ref = e
        elif e < ref:
            ref = e
    return gain


def _parse_time(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _text(el) -> str:
    return el.text.strip() if el is not None and el.text else ""


def _parse_recording(arcname: str, data: bytes) -> Optional[NormalizedSession]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    name = _text(root.find("gpx:metadata/gpx:name", _GPX_NS)) or _text(
        root.find("gpx:trk/gpx:name", _GPX_NS)
    )

    pts = []  # (lat, lon, ele_or_None, time_or_None)
    for tp in root.iterfind(".//gpx:trkpt", _GPX_NS):
        try:
            lat, lon = float(tp.get("lat")), float(tp.get("lon"))
        except (TypeError, ValueError):
            continue
        ele_el = tp.find("gpx:ele", _GPX_NS)
        try:
            ele = float(ele_el.text) if ele_el is not None and ele_el.text else None
        except ValueError:
            ele = None
        pts.append((lat, lon, ele, _parse_time(_text(tp.find("gpx:time", _GPX_NS)))))

    if not pts:
        return None

    times = [t for *_x, t in pts if t is not None]
    if not times:
        return None  # no timestamps -> can't place the activity on a day; skip
    date = min(times).astimezone(timezone.utc).date().isoformat()
    duration_s = _moving_seconds(times) or None

    dist_m = 0.0
    prev_ll = None
    for lat, lon, _ele, _t in pts:
        if prev_ll is not None:
            dist_m += _haversine_m(prev_ll[0], prev_ll[1], lat, lon)
        prev_ll = (lat, lon)

    start = min(times)
    ele_samples = [
        ((t - start).total_seconds(), ele)
        for _lat, _lon, ele, t in pts
        if ele is not None and t is not None
    ]
    gain_m = _smoothed_gain_m(ele_samples)

    m = _ID_RE.search(arcname)
    key = m.group(1) if m else os.path.splitext(os.path.basename(arcname))[0]

    metrics = [("distance_m", round(dist_m, 1), "m")]
    if gain_m > 0:
        metrics.append(("elevation_gain_m", round(gain_m, 1), "m"))

    return NormalizedSession(
        date=date,
        source=SOURCE,
        activity_type=ACTIVITY_TYPE,
        workout=name or "Hike",
        source_session_key=key,
        duration_s=duration_s,
        notes=None,
        sets=[],
        metrics=metrics,
    )


def parse(path: str) -> ParsedImport:
    sessions = []
    seen = set()
    for arcname, data in _iter_recording_gpx(path):
        sess = _parse_recording(arcname, data)
        if sess is None:
            continue
        dedup = (sess.date, sess.workout, sess.source_session_key)
        if dedup in seen:  # same activity twice in one export -> keep first
            continue
        seen.add(dedup)
        sessions.append(sess)
    sessions.sort(key=lambda s: (s.date, s.source_session_key))
    return ParsedImport(
        source=SOURCE, filename=os.path.basename(str(path)), sessions=sessions
    )
