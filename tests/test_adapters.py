import pytest

from strength_coach.adapters import (
    alltrails_gpx, detect, liftin_csv, liftosaur_json, macrofactor, stronglifts_db,
)
from strength_coach.adapters.base import (
    epoch_ms_to_iso,
    excel_serial_to_iso,
    normalize_header,
    parse_hm_duration,
    parse_weight,
)
from strength_coach.models import SET_TYPE_FAILURE, SET_TYPE_STANDARD, SET_TYPE_WARMUP
from tests import fixtures


# ---- base helpers ----

def test_date_conversions():
    assert excel_serial_to_iso(46054) == "2026-02-01"
    assert epoch_ms_to_iso(1590969600000) == "2020-06-01"


def test_parse_weight_units():
    assert parse_weight("110 lbs") == 110.0
    assert parse_weight("50 kg") == pytest.approx(110.23, abs=0.1)
    assert parse_weight("") is None


def test_parse_hm_duration():
    assert parse_hm_duration("1h 22m") == 4920
    assert parse_hm_duration("0h 40m") == 2400
    assert parse_hm_duration("") is None


def test_fix_mojibake():
    from strength_coach.adapters.base import fix_mojibake
    assert fix_mojibake("45Â° Incline Dumbbell Press") == "45° Incline Dumbbell Press"
    assert fix_mojibake("Barbell Squat") == "Barbell Squat"  # clean text untouched
    assert fix_mojibake("") == ""


def test_normalize_header_strips_bom_and_mojibake():
    # Real BOM codepoint and the mojibake form the MacroFactor flat export
    # actually carries must both reduce to 'date'.
    assert normalize_header('﻿"Date"') == "date"
    assert normalize_header('ï»¿"Date"') == "date"
    assert normalize_header("Weight (lbs)") == "weight (lbs)"


# ---- StrongLifts adapter ----

def test_stronglifts_adapter(tmp_path):
    p = tmp_path / "sl.db"
    fixtures.write_stronglifts_db(str(p))
    parsed = stronglifts_db.parse(str(p))

    assert parsed.source == "stronglifts"
    # Two workouts on the same day + day_name must remain distinct sessions.
    assert len(parsed.sessions) == 2
    s1 = next(s for s in parsed.sessions if s.source_session_key == "1")
    assert s1.date == "2020-06-01"
    assert s1.duration_s == 45 * 60
    warmup, amrap = s1.sets
    assert warmup.set_type == SET_TYPE_WARMUP
    assert amrap.set_type == SET_TYPE_FAILURE
    assert amrap.rir == 2.0  # RPE 8 -> RIR 2
    assert warmup.rir is None  # RPE 0 -> unavailable, not RIR 10


# ---- Liftin adapter ----

def test_liftin_adapter(tmp_path):
    p = tmp_path / "liftin.csv"
    fixtures.write_liftin_csv(str(p))
    parsed = liftin_csv.parse(str(p))

    assert parsed.source == "liftin"
    # Two sessions same calendar day, distinguished by timestamp.
    assert len(parsed.sessions) == 2
    morning = next(s for s in parsed.sessions if s.source_session_key.endswith("08:00"))
    assert morning.date == "2024-03-01"
    assert morning.workout == "Day 1"
    assert morning.duration_s == 3900
    assert morning.sets[0].set_type == SET_TYPE_WARMUP
    assert morning.sets[0].weight_lb == 95.0
    assert morning.sets[1].set_type == SET_TYPE_STANDARD
    assert all(s.rir is None for s in morning.sets)  # Perception empty


# ---- MacroFactor adapter ----

def test_macrofactor_adapter(tmp_path):
    p = tmp_path / "mf.xlsx"
    fixtures.write_macrofactor_xlsx(str(p), with_program=True)
    parsed = macrofactor.parse(str(p))

    assert parsed.source == "macrofactor"
    assert len(parsed.sessions) == 1
    session = parsed.sessions[0]
    assert session.date == "2026-02-01"  # BOM header + Excel serial handled
    assert session.workout == "Workout A"
    types = [s.set_type for s in session.sets]
    assert types == [SET_TYPE_WARMUP, SET_TYPE_STANDARD, SET_TYPE_FAILURE]
    # Per-set weight, not the "Exercise Base Weight (lbs)" column beside it.
    assert [s.weight_lb for s in session.sets] == [95, 135, 225]

    # Active Program snapshot captured for change-detection.
    snap = parsed.program_snapshot
    assert snap["program_name"] == "Advanced Full Body"
    assert snap["cycles"] == "5"
    assert "Close Grip Bench Press" in snap["workouts"]["Workout A @ Home Gym"]
    assert "Barbell Back Squat" in snap["workouts"]["Workout A @ Home Gym"]


@pytest.mark.parametrize("header,weights,expected", [
    ("Weight (kg)", (40, 60, 100), [88.18, 132.28, 220.46]),
    ("Weight (kgs)", (40, 60, 100), [88.18, 132.28, 220.46]),
    ("Weight", (95, 135, 225), [95, 135, 225]),
    ("Weight (lbs)", ("95 lbs", "135", "100 kg"), [95, 135, 220.46]),
])
def test_macrofactor_weight_column_variants(tmp_path, header, weights, expected):
    p = tmp_path / "mf.xlsx"
    fixtures.write_macrofactor_xlsx(str(p), weight_header=header, weights=weights)
    sets = macrofactor.parse(str(p)).sessions[0].sets
    assert [s.weight_lb for s in sets] == expected


def test_macrofactor_missing_weight_column_raises(tmp_path):
    # A silent all-NULL import is what lost Sep 2026's weights; raising routes
    # the file to the watcher's failed/ folder, where it is kept.
    p = tmp_path / "mf.xlsx"
    fixtures.write_macrofactor_xlsx(str(p), weight_header="Load (stones)")
    with pytest.raises(ValueError, match="weight column"):
        macrofactor.parse(str(p))


def test_macrofactor_all_weights_unparseable_raises(tmp_path):
    p = tmp_path / "mf.xlsx"
    fixtures.write_macrofactor_xlsx(str(p), weights=(95, "heavy", "heavy"))
    with pytest.raises(ValueError, match="no weights"):
        macrofactor.parse(str(p))


def test_macrofactor_flat_export_no_program(tmp_path):
    p = tmp_path / "mf_flat.xlsx"
    fixtures.write_macrofactor_xlsx(str(p), with_program=False)
    parsed = macrofactor.parse(str(p))
    assert len(parsed.sessions) == 1
    assert parsed.program_snapshot is None


# ---- Liftosaur adapter ----

def test_liftosaur_adapter(tmp_path):
    p = tmp_path / "lo.json"
    fixtures.write_liftosaur_json(str(p))
    parsed = liftosaur_json.parse(str(p))

    assert parsed.source == "liftosaur"
    assert len(parsed.sessions) == 1
    session = parsed.sessions[0]
    assert session.date == "2025-10-05"
    assert session.workout == "Day 1"
    assert session.duration_s == 3600
    assert session.source_session_key == "555"

    bench_sets = [s for s in session.sets if s.exercise_raw == "Bench Press"]
    # warmup + working + amrap = 3; the isCompleted:False set is excluded
    assert len(bench_sets) == 3
    assert bench_sets[0].set_type == SET_TYPE_WARMUP
    assert bench_sets[1].set_type == SET_TYPE_STANDARD
    assert bench_sets[2].set_type == SET_TYPE_FAILURE  # isAmrap -> failure
    assert bench_sets[2].reps == 8

    squat_set = next(s for s in session.sets if s.exercise_raw == "Squat")
    assert squat_set.weight_lb == pytest.approx(220.46, abs=0.1)  # kg -> lb


# ---- detection ----

def test_detect_routes_each_source(tmp_path):
    sl = tmp_path / "a.db"
    fixtures.write_stronglifts_db(str(sl))
    lf = tmp_path / "b.csv"
    fixtures.write_liftin_csv(str(lf))
    mf = tmp_path / "c.xlsx"
    fixtures.write_macrofactor_xlsx(str(mf))
    lo = tmp_path / "d.json"
    fixtures.write_liftosaur_json(str(lo))
    at = tmp_path / "e.zip"
    fixtures.write_alltrails_export(str(at))
    assert detect.detect_source(str(at)) == "alltrails"
    assert detect.detect_source(str(sl)) == "stronglifts"
    assert detect.detect_source(str(lf)) == "liftin"
    assert detect.detect_source(str(mf)) == "macrofactor"
    assert detect.detect_source(str(lo)) == "liftosaur"
    # parse_file dispatches correctly end to end
    assert detect.parse_file(str(mf)).source == "macrofactor"
    assert detect.parse_file(str(lo)).source == "liftosaur"


def test_detect_rejects_unknown(tmp_path):
    junk = tmp_path / "junk.csv"
    junk.write_text("foo,bar,baz\n1,2,3\n")
    with pytest.raises(detect.UnknownSourceError):
        detect.detect_source(str(junk))


# ---- AllTrails adapter ----

def test_alltrails_adapter(tmp_path):
    p = tmp_path / "alltrails.zip"
    fixtures.write_alltrails_export(str(p))
    parsed = alltrails_gpx.parse(str(p))

    assert parsed.source == "alltrails"
    # only the recorded hike becomes a session; the saved route (maps/) is ignored
    assert len(parsed.sessions) == 1
    s = parsed.sessions[0]
    assert s.activity_type == "hike"
    assert s.date == "2021-05-01"
    assert s.workout == "Test Loop"
    assert s.source_session_key == "abc1234"  # stable id hash from filename
    assert s.sets == []
    assert s.duration_s == 180  # 19 points @ 10s apart, no pause gaps
    metrics = {m: (v, u) for m, v, u in s.metrics}
    gain, gunit = metrics["elevation_gain_m"]
    assert gunit == "m"
    assert gain == pytest.approx(43.5, abs=4)  # +54m raw, smoothing trims edges
    dist_val, dist_unit = metrics["distance_m"]
    assert dist_unit == "m"
    assert dist_val == pytest.approx(200, abs=25)


def test_alltrails_accepts_nested_activities_zip(tmp_path):
    # Handing just the inner "Activities" zip (recordings/ at top level) works too.
    import zipfile
    full = tmp_path / "full.zip"
    fixtures.write_alltrails_export(str(full))
    inner = tmp_path / "activities.zip"
    with zipfile.ZipFile(str(full)) as zf:
        inner.write_bytes(zf.read("User Online Activity/Activities 0__test.zip"))
    parsed = alltrails_gpx.parse(str(inner))
    assert len(parsed.sessions) == 1
    assert parsed.sessions[0].source_session_key == "abc1234"


def test_alltrails_detection(tmp_path):
    p = tmp_path / "alltrails.zip"
    fixtures.write_alltrails_export(str(p))
    assert detect.detect_source(str(p)) == "alltrails"
    assert detect.parse_file(str(p)).source == "alltrails"


def test_alltrails_moving_seconds_excludes_pauses():
    from datetime import datetime, timedelta, timezone
    base = datetime(2021, 5, 1, tzinfo=timezone.utc)
    times = [
        base,
        base + timedelta(seconds=10),
        base + timedelta(seconds=20),
        base + timedelta(seconds=620),  # 10-min pause gap -> not counted
        base + timedelta(seconds=630),
    ]
    assert alltrails_gpx._moving_seconds(times) == 30  # 10 + 10 + 10


def test_alltrails_smoothed_gain_ignores_gps_noise():
    # flat elevation with +/-5m GPS jitter every 5s -> real gain ~0
    noisy = [(i * 5, 100 + (5 if i % 2 else -5)) for i in range(60)]
    assert alltrails_gpx._smoothed_gain_m(noisy) < 10
    # a genuine +90m climb over 5 min survives smoothing
    climb = [(i * 10, 100 + 3 * i) for i in range(31)]
    assert alltrails_gpx._smoothed_gain_m(climb) == pytest.approx(80, abs=6)
