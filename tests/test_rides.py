"""Ride analysis: NP, EF, decoupling and main-set detection."""

import pytest

from strength_coach import db, rides
from strength_coach.models import NormalizedSession, ParsedImport


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    return c


def _ingest(conn, streams, workout="45 min Power Zone Endurance Ride",
            duration_s=None):
    """Ingest one spin session. `duration_s` defaults to the stream's own span
    so fixtures are self-consistent; pass it explicitly to simulate a ride whose
    graph came back truncated relative to the class length."""
    if duration_s is None:
        offsets = streams[0][3] if streams else []
        duration_s = (offsets[-1] - offsets[0]) if len(offsets) > 1 else 0
    s = NormalizedSession(
        date="2026-09-23", source="indoor_bike", activity_type="spin",
        workout=workout, source_session_key="w1", duration_s=duration_s,
        streams=streams)
    db.ingest(conn, ParsedImport(source="indoor_bike", filename="api", sessions=[s]),
              lambda raw, src: 1)
    return conn.execute("SELECT id FROM sessions").fetchone()["id"]


def _ride(power, hr=None, sample_s=5):
    offsets = [i * sample_s for i in range(len(power))]
    out = [("output", "watts", sample_s, offsets, power)]
    if hr is not None:
        out.append(("heart_rate", "bpm", sample_s, offsets, hr))
    return out


# --- storage round-trip -------------------------------------------------

def test_streams_round_trip(conn):
    sid = _ingest(conn, _ride([100.0] * 10, [120.0] * 10))
    got = rides.load_streams(conn, sid)
    assert set(got) == {"output", "heart_rate"}
    assert got["output"]["sample_s"] == 5
    assert got["output"]["values"] == [100.0] * 10
    assert got["output"]["offsets"][:3] == [0, 5, 10]


def test_reingest_replaces_streams_rather_than_duplicating(conn):
    sid = _ingest(conn, _ride([100.0] * 10, [120.0] * 10))
    _ingest(conn, _ride([200.0] * 10, [130.0] * 10))
    rows = conn.execute("SELECT COUNT(*) c FROM session_streams").fetchone()["c"]
    assert rows == 2  # output + heart_rate, not 4
    assert rides.load_streams(conn, sid)["output"]["values"] == [200.0] * 10


def test_schema_version_advances_on_existing_store(tmp_path):
    path = str(tmp_path / "old.db")
    c = db.connect(path)
    c.executescript("CREATE TABLE schema_meta (version INTEGER NOT NULL);"
                    "INSERT INTO schema_meta (version) VALUES (1);")
    c.commit()
    db.init_schema(c)
    assert c.execute("SELECT version FROM schema_meta").fetchone()["version"] == 2
    c.execute("SELECT 1 FROM session_streams LIMIT 1")  # table now exists


# --- normalized power ---------------------------------------------------

def test_np_equals_ap_on_perfectly_steady_power():
    steady = [100.0] * 200
    assert rides.normalized_power(steady, 5) == pytest.approx(100.0, abs=0.01)


def test_np_exceeds_ap_when_power_surges():
    # Same arithmetic mean (100W), wildly different cost.
    surging = ([200.0] * 12 + [0.0] * 12) * 10
    ap = sum(surging) / len(surging)
    np_w = rides.normalized_power(surging, 5)
    assert ap == pytest.approx(100.0)
    assert np_w > ap * 1.2


def test_np_none_when_series_shorter_than_its_window():
    # 30s window at 5s spacing needs 6 samples; 3 can't fill one.
    assert rides.normalized_power([100.0, 110.0, 120.0], 5) is None


def test_np_tolerates_dropped_samples():
    vals = [100.0, None, 100.0, None, 100.0, 100.0, 100.0, 100.0]
    assert rides.normalized_power(vals, 5) == pytest.approx(100.0, abs=0.01)


# --- main-set detection -------------------------------------------------

def test_main_set_trims_warmup_and_cooldown():
    # 60 samples (5 min) easy, 240 hard, 60 easy.
    power = [20.0] * 60 + [120.0] * 240 + [20.0] * 60
    start, end = rides.main_set_bounds(power, 5)
    assert start >= 55 and end <= 310
    assert (end - start + 1) * 5 > 900  # recovers most of the 20-min main set


def test_main_set_none_for_a_one_minute_spin():
    assert rides.main_set_bounds([0.0] * 12, 5) is None


def test_warmup_exclusion_changes_the_reported_power(conn):
    power = [20.0] * 60 + [120.0] * 240 + [20.0] * 60
    sid = _ingest(conn, _ride(power, [100.0] * 360))
    a = rides.ride_analysis(conn, sid)
    # Whole-ride average is ~86W; the main set is 120W.
    assert a["avg_power_w"] > 110


# --- efficiency factor and decoupling -----------------------------------

def test_ef_is_np_over_mean_hr(conn):
    sid = _ingest(conn, _ride([100.0] * 200, [125.0] * 200))
    a = rides.ride_analysis(conn, sid)
    assert a["efficiency_factor"] == pytest.approx(100.0 / 125.0, abs=0.005)
    assert a["variability_index"] == pytest.approx(1.0, abs=0.01)


def test_decoupling_zero_when_hr_holds_at_constant_power(conn):
    sid = _ingest(conn, _ride([100.0] * 200, [120.0] * 200))
    assert rides.ride_analysis(conn, sid)["decoupling_pct"] == pytest.approx(0.0, abs=0.5)


def test_decoupling_positive_when_hr_drifts_up_at_constant_power(conn):
    hr = [120.0] * 100 + [132.0] * 100
    sid = _ingest(conn, _ride([100.0] * 200, hr))
    d = rides.ride_analysis(conn, sid)["decoupling_pct"]
    assert d > rides.DECOUPLING_THRESHOLD_PCT  # ~9%


def test_ride_without_hr_still_reports_power_metrics(conn):
    sid = _ingest(conn, _ride([100.0] * 200))
    a = rides.ride_analysis(conn, sid)
    assert a["np_w"] is not None
    assert a["efficiency_factor"] is None and a["decoupling_pct"] is None


def test_series_filters_by_ride_type(conn):
    _ingest(conn, _ride([100.0] * 200, [120.0] * 200))
    assert len(rides.ride_series(conn, workout_like="%Power Zone Endurance%")) == 1
    assert rides.ride_series(conn, workout_like="%HIIT%") == []


# --- truncated-stream guard ---------------------------------------------

def test_truncated_stream_is_rejected_not_analyzed(conn):
    # 45-minute session, but the source returned only ~2 minutes of graph.
    sid = _ingest(conn, _ride([100.0] * 24, [120.0] * 24), duration_s=2700)
    a = rides.ride_analysis(conn, sid)
    assert a["truncated"] is True
    assert a["coverage"] < 0.1
    assert a["efficiency_factor"] is None  # no confident number off a fragment


def test_truncated_rides_are_dropped_from_the_series(conn):
    _ingest(conn, _ride([100.0] * 24, [120.0] * 24), duration_s=2700)
    assert rides.ride_series(conn, workout_like="%Power Zone Endurance%") == []


def test_complete_stream_reports_full_coverage(conn):
    sid = _ingest(conn, _ride([100.0] * 540, [120.0] * 540), duration_s=2700)
    a = rides.ride_analysis(conn, sid)
    assert a["truncated"] is False
    assert a["coverage"] > 0.95
