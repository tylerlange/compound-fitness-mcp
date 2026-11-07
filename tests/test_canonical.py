import pytest

from strength_coach import db
from strength_coach.canonical import (
    Canonicalizer,
    merge_exercise,
    normalize,
    seed_aliases,
)
from strength_coach.models import (
    NormalizedSession,
    NormalizedSet,
    ParsedImport,
    SET_TYPE_STANDARD,
)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    yield c
    c.close()


def test_normalize():
    assert normalize("Bench Press (Barbell)") == "bench press barbell"
    assert normalize("  Close-Grip   Bench  ") == "close grip bench"


def test_resolve_creates_and_reuses(conn):
    c = Canonicalizer(conn)
    a = c.resolve("Barbell Squat", "macrofactor")
    b = c.resolve("Barbell Squat", "macrofactor")
    assert a == b
    assert "Barbell Squat" in c.newly_created


def test_normalized_match_same_id_across_sources(conn):
    c = Canonicalizer(conn)
    a = c.resolve("Bench Press (Barbell)", "liftin")
    # Different punctuation/case, different source -> same canonical exercise.
    b = c.resolve("bench press barbell", "stronglifts")
    assert a == b


def test_seed_aliases_maps_variants(conn):
    seed_aliases(conn, {
        "Overhead Press": "Standing Barbell Overhead Press",
        "OHP": "Standing Barbell Overhead Press",
    })
    c = Canonicalizer(conn)
    a = c.resolve("Overhead Press", "stronglifts")
    b = c.resolve("OHP", "liftin")
    assert a == b


def test_merge_exercise_repoints_sets(conn):
    parsed = ParsedImport("macrofactor", "a.xlsx", sessions=[
        NormalizedSession("2026-01-01", "macrofactor", workout="A", sets=[
            NormalizedSet("Bench Press", 0, weight_lb=185, reps=5,
                          set_type=SET_TYPE_STANDARD)]),
        NormalizedSession("2023-01-01", "liftin", workout="A", sets=[
            NormalizedSet("Barbell Bench Press", 0, weight_lb=175, reps=5,
                          set_type=SET_TYPE_STANDARD)]),
    ])
    db.ingest(conn, parsed, Canonicalizer(conn).resolve)
    # Two distinct canonical names got created (names don't normalize equal).
    assert conn.execute("SELECT COUNT(*) c FROM exercises").fetchone()["c"] == 2

    res = merge_exercise(conn, "Barbell Bench Press", "Bench Press")
    assert res["sets_repointed"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM exercises").fetchone()["c"] == 1
    # All sets now point at the surviving exercise.
    ids = {r["exercise_id"] for r in conn.execute("SELECT exercise_id FROM sets")}
    assert len(ids) == 1


def test_suggestion_for_near_match(conn):
    c = Canonicalizer(conn)
    c.resolve("Barbell Back Squat", "macrofactor")
    c.resolve("Barbell Back Squats", "liftin")  # near-dup, should suggest, not fuse
    assert conn.execute("SELECT COUNT(*) c FROM exercises").fetchone()["c"] == 2
    assert "Barbell Back Squats" in c.suggestions
