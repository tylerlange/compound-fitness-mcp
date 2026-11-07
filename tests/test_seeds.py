from strength_coach import db
from strength_coach.canonical import Canonicalizer, seed_aliases
from strength_coach.seeds import DEFAULT_ALIASES


def test_seed_map_has_no_chains():
    # No alias key may also be a canonical target, or seeding order would
    # matter and mappings could chain unpredictably.
    keys = set(DEFAULT_ALIASES)
    values = set(DEFAULT_ALIASES.values())
    assert keys.isdisjoint(values), keys & values


def test_seed_map_no_self_maps():
    for k, v in DEFAULT_ALIASES.items():
        assert k != v


def test_seeds_apply_and_resolve(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    seed_aliases(conn, DEFAULT_ALIASES)
    c = Canonicalizer(conn)
    # A couple of representative variant names resolve to their canonical id.
    assert c.resolve("Barbell Bench Press", "macrofactor") == c.resolve(
        "Bench Press", "stronglifts")
    assert c.resolve("Pullups", "macrofactor") == c.resolve("Pull-Up", "liftin")
    conn.close()


def test_defaults_merge_no_distinct_variations():
    # Merging genuinely different variations is a per-user decision that
    # belongs in aliases.yaml, not in the engine's defaults.
    targets = set(DEFAULT_ALIASES.values())
    assert "Incline Bench Press" not in targets
    assert not any("(" in k and "Close Grip" not in k for k in DEFAULT_ALIASES)
