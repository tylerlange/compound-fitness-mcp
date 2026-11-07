import pytest

from strength_coach import aliases, db
from strength_coach.canonical import Canonicalizer, seed_aliases
from strength_coach.seeds import DEFAULT_ALIASES


def _write(tmp_path, text):
    (tmp_path / "aliases.yaml").write_text(text, encoding="utf-8")


def test_absent_file_means_defaults_only(tmp_path):
    assert aliases.load(str(tmp_path)) == DEFAULT_ALIASES


def test_no_data_dir_means_defaults_only():
    assert aliases.load(None) == DEFAULT_ALIASES


def test_present_file_adds_entries(tmp_path):
    _write(tmp_path, "# mine\nBack Squat (Barbell): Squat\n\nDB Incline: Incline Dumbbell Press\n")
    got = aliases.load(str(tmp_path))
    assert got["Back Squat (Barbell)"] == "Squat"
    assert got["DB Incline"] == "Incline Dumbbell Press"
    assert DEFAULT_ALIASES.items() <= got.items()


def test_user_entry_overrides_default(tmp_path):
    _write(tmp_path, "OHP: Push Press\n")
    assert aliases.load(str(tmp_path))["OHP"] == "Push Press"


def test_override_repoints_an_already_seeded_store(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    seed_aliases(conn, DEFAULT_ALIASES)
    _write(tmp_path, "OHP: Push Press\n")
    seed_aliases(conn, aliases.load(str(tmp_path)))
    c = Canonicalizer(conn)
    assert c.resolve("OHP", "liftin") == c.resolve("Push Press", "liftin")
    assert c.resolve("OHP", "liftin") != c.resolve("Overhead Press", "liftin")
    conn.close()


def test_quoted_name_with_colon_and_inline_comment(tmp_path):
    _write(tmp_path, '"Squat: Paused": Paused Squat  # tempo work\n')
    assert aliases.load(str(tmp_path))["Squat: Paused"] == "Paused Squat"


def test_values_are_data_not_code(tmp_path):
    _write(tmp_path, "Weird: __import__('os').system('true')\n")
    assert aliases.load(str(tmp_path))["Weird"] == "__import__('os').system('true')"


@pytest.mark.parametrize("text, needle", [
    ("Squat\n", "aliases.yaml:1"),
    ("Squat:\n", "aliases.yaml:1"),
    ("lifts:\n  - Squat\n", "aliases.yaml:1"),
    ("- Squat\n", "no lists"),
    ("ok: Fine\n  nested: Bad\n", "aliases.yaml:2"),
    ('"Unclosed: Squat\n', "unclosed quote"),
    ("Squat: Squat\n", "maps to itself"),
    ("A: B\nA: C\n", "listed twice"),
    ("Back Squat: Barbell Back Squat\n", "Map 'Back Squat' to 'Squat' directly"),
])
def test_malformed_file_raises_with_line_and_fix(tmp_path, text, needle):
    _write(tmp_path, text)
    with pytest.raises(aliases.AliasFileError) as e:
        aliases.load(str(tmp_path))
    assert needle in str(e.value)
