import os

import pytest

from strength_coach import cli, config, context, synthetic

from .helpers import make_server


def test_init_scaffolds_templates_outside_repo(tmp_path, capsys):
    target = tmp_path / "inst"
    assert cli.main(["init", "--dir", str(target)]) == 0
    assert sorted(os.listdir(target)) == ["aliases.yaml", "coaching-profile.md", "gym.md"]
    assert oct(os.stat(target).st_mode & 0o777) == "0o700"
    rep = config.check(str(target), need_db=False)
    assert rep.ok
    assert any("REPLACE:" in w for w in rep.warnings)  # template not filled in yet


def test_init_never_overwrites(tmp_path, capsys):
    target = tmp_path / "inst"
    target.mkdir()
    (target / "gym.md").write_text("mine\n")
    assert cli.main(["init", "--dir", str(target)]) == 0
    assert (target / "gym.md").read_text() == "mine\n"
    assert "kept" in capsys.readouterr().out


def test_init_from_example(tmp_path):
    target = tmp_path / "inst"
    assert cli.main(["init", "--dir", str(target), "--example", "runner-2day"]) == 0
    assert "runner-2day" in (target / "coaching-profile.md").read_text()
    assert not config.check(str(target), need_db=False).warnings


def test_init_unknown_example_lists_choices(tmp_path, capsys):
    assert cli.main(["init", "--dir", str(tmp_path / "x"), "--example", "nope"]) == 2
    assert "home-3day" in capsys.readouterr().err


def test_init_refuses_a_target_inside_the_checkout(capsys):
    if not os.path.exists(os.path.join(cli.REPO, ".git")):
        pytest.skip("not a git checkout")
    assert cli.main(["init", "--dir", os.path.join(cli.REPO, "mydata")]) == 2
    assert not os.path.exists(os.path.join(cli.REPO, "mydata"))
    assert "outside" in capsys.readouterr().err


def test_every_example_has_both_files_and_a_dumbbell_cap():
    names = cli._examples()
    assert len(names) >= 2
    for n in names:
        gym = open(os.path.join(cli.EXAMPLES, n, "gym.md")).read().lower()
        assert "per hand" in gym and "ceiling" in gym, n


def test_unset_data_dir_is_an_error_with_a_fix(monkeypatch):
    monkeypatch.delenv("COACH_DATA_DIR", raising=False)
    with pytest.raises(config.ConfigError) as e:
        config.data_dir()
    assert "bin/coach init" in str(e.value)


def test_check_fails_on_malformed_aliases(tmp_path, capsys):
    (tmp_path / "aliases.yaml").write_text("- nope\n")
    assert cli.main(["check", "--dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "ERROR" in out and "aliases.yaml:1" in out


def test_context_prints_exactly_what_get_context_returns(tmp_path, monkeypatch, capsys):
    m, _, db_path = make_server(tmp_path, monkeypatch, inject=False)
    for n in ("gym.md", "coaching-profile.md"):
        src = os.path.join(cli.EXAMPLES, "home-3day", n)
        (tmp_path / n).write_text(open(src).read())
    assert cli.main(["context", "--dir", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert printed == m.get_context() + "\n"
    assert printed == context.as_sent(str(tmp_path)) + "\n"
    assert printed.startswith(context.DATA_NOTICE)


def test_synthetic_store_is_reproducible_for_the_demo(tmp_path):
    a = synthetic.build(str(tmp_path / "a.db"))
    b = synthetic.build(str(tmp_path / "b.db"))
    assert a == b


def test_template_comments_are_not_sent_to_the_model(tmp_path):
    assert cli.main(["init", "--dir", str(tmp_path / "i")]) == 0
    text = context.read_data_file(str(tmp_path / "i"), "gym.md", "gym")
    assert "<!--" not in text and "REPLACE:" in text


def test_names_shows_raw_variants_and_likely_duplicates(tmp_path, capsys):
    from strength_coach import db as scdb
    from strength_coach.canonical import Canonicalizer, seed_aliases
    from strength_coach.models import NormalizedSession, NormalizedSet, ParsedImport
    from strength_coach.seeds import DEFAULT_ALIASES
    path = str(tmp_path / "probe.db")
    conn = scdb.connect(path)
    scdb.init_schema(conn)
    seed_aliases(conn, DEFAULT_ALIASES)
    sets = [NormalizedSet(n, i, weight_lb=100, reps=5) for i, n in enumerate(
        ["Barbell Back Squat", "Incline Bench Press", "Incline Bench Press (Barbell)"])]
    scdb.ingest(conn, ParsedImport("liftin", "x.csv", sessions=[
        NormalizedSession("2026-03-02", "liftin", workout="A", sets=sets)]),
        Canonicalizer(conn).resolve)
    conn.close()
    assert cli.main(["names", "--db", path]) == 0
    out = capsys.readouterr().out
    assert "liftin: Barbell Back Squat" in out          # folded by a default, still visible
    assert "Incline Bench Press  ~  Incline Bench Press (Barbell)" in out
