import importlib.util
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "scan_identifiers", os.path.join(ROOT, "scripts", "scan_identifiers.py"))
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def test_terms_match_whole_terms_case_insensitively():
    rx = scan.term_pattern("kg")
    assert rx.search("a 100kg bar") and rx.search("KG")
    assert not rx.search("background") and not rx.search("backgrounds")
    num = scan.term_pattern("1984")
    assert num.search("since 1984.") and num.search("1984-1990")
    assert not num.search("19845")
    multi = scan.term_pattern("acme gym")
    assert multi.search("the ACME   Gym floor") and not multi.search("acme gymnasium")


def _repo(tmp_path, files, denylist):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (tmp_path / scan.DENYLIST).write_text(denylist)
    return str(tmp_path)


def test_denylist_hits_allow_lines_and_ignored_files(tmp_path):
    root = _repo(tmp_path, {
        "a.md": "trains at Acme Gym\nno match here\n",
        "README.md": "Built by Pat Example of Acme Gym.\n",
        ".gitignore": "private/\n.identifiers.local\n",
        "private/notes.md": "Acme Gym\n",
        "acme gym.txt": "x\n",
    }, "# mine\nAcme Gym\nallow: Built by Pat Example\n")
    hits = scan.scan(root)
    where = sorted((os.path.relpath(p, root), line) for p, line, _, _ in hits)
    assert where == [("a.md", 1), ("acme gym.txt", 0)]


def test_absent_denylist_runs_builtin_patterns_only(tmp_path):
    root = _repo(tmp_path, {"a.md": "Acme Gym\n"}, "")
    os.remove(os.path.join(root, scan.DENYLIST))
    assert scan.scan(root) == []
