"""Per-user exercise aliases, merged over the defaults at load time.

`$COACH_DATA_DIR/aliases.yaml` maps the names *your* apps use to the canonical
names you want. It is read with a deliberately small parser instead of a YAML
library: the core package takes no new dependency, and a flat mapping is all
this file ever needs. Accepted, one entry per line:

    # comment
    Source Name: Canonical Name
    "Name: with a colon": Canonical Name

Anything else (nesting, lists, anchors, a missing value) is an error that names
the line, because a silently skipped alias splits one lift into two in every
trend and comparison from then on. Values are data: they are stored as
exercise names and never evaluated.
"""
import os
from typing import Optional

from .seeds import DEFAULT_ALIASES

FILENAME = "aliases.yaml"


class AliasFileError(ValueError):
    """aliases.yaml is present but cannot be used. The message says how to fix it."""


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def parse(text: str, path: str = FILENAME) -> dict:
    out: dict = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path}:{n}"
        if raw[:1] in (" ", "\t"):
            raise AliasFileError(f"{where}: indented line. Use one flat "
                                 "'Source Name: Canonical Name' per line.")
        if line.startswith(("-", "[", "{", "&", "*", "!", "|", ">")):
            raise AliasFileError(f"{where}: only 'Source Name: Canonical Name' "
                                 "lines are supported (no lists, flow maps or anchors).")
        if line[0] in "\"'":
            end = line.find(line[0], 1)
            if end < 0:
                raise AliasFileError(f"{where}: unclosed quote in the source name.")
            key, rest = line[1:end], line[end + 1:].lstrip()
            if not rest.startswith(":"):
                raise AliasFileError(f"{where}: expected ':' after the quoted name.")
            value = rest[1:]
        else:
            key, sep, value = line.partition(": ")
            if not sep:
                key, sep, value = line.partition(":")
                if not sep or value:
                    raise AliasFileError(f"{where}: expected 'Source Name: Canonical "
                                         "Name' (a colon followed by a space). Quote "
                                         "a source name that contains a colon.")
        key = key.strip()
        value = _unquote(value.split(" #", 1)[0].strip())
        if not key or not value:
            raise AliasFileError(f"{where}: both the source name and the canonical "
                                 "name must be non-empty.")
        if key == value:
            raise AliasFileError(f"{where}: '{key}' maps to itself; delete the line.")
        if key in out and out[key] != value:
            raise AliasFileError(f"{where}: '{key}' is listed twice with different "
                                 "targets; keep one.")
        out[key] = value
    return out


def merge(user: dict, defaults: Optional[dict] = None) -> dict:
    """Defaults overlaid by `user`; user entries win. Rejects a merged map in
    which a target is itself an alias, so resolution never depends on order."""
    merged = dict(DEFAULT_ALIASES if defaults is None else defaults)
    merged.update(user)
    for key, value in merged.items():
        if value in merged:
            raise AliasFileError(
                f"aliases.yaml: '{key}' maps to '{value}', which is itself mapped to "
                f"'{merged[value]}'. Map '{key}' to '{merged[value]}' directly (add a "
                f"line for '{key}' if it is one of the defaults in seeds.py).")
    return merged


def load(data_dir: Optional[str]) -> dict:
    """The alias map to seed: defaults only when the file is absent, defaults
    plus the user's file when present. A malformed file raises AliasFileError."""
    if not data_dir:
        return dict(DEFAULT_ALIASES)
    path = os.path.join(data_dir, FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return dict(DEFAULT_ALIASES)
    return merge(parse(text, path))
