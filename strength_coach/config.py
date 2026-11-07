"""Where the instance lives, and whether it is usable.

The repo is the engine. Everything that makes it *yours* (the training store,
coaching-profile.md, gym.md, aliases.yaml) lives in one data directory outside
the repo, named by COACH_DATA_DIR. `check()` is what `bin/coach check` prints
and what every entry point runs at startup, so a misconfiguration fails with a
fix instead of a stack trace or, worse, a coach that quietly has no profile.
"""
import os
from dataclasses import dataclass, field
from typing import Optional

from . import aliases

DB_FILENAME = "training.db"
PROFILE = "coaching-profile.md"
GYM = "gym.md"
INSTANCE_FILES = (PROFILE, GYM, aliases.FILENAME)
# Templates mark every line the user still has to fill in with this token.
PLACEHOLDER = "REPLACE:"


# COACH_MODEL provider prefix -> the key pydantic-ai reads for it. Unknown
# prefixes are not checked; pydantic-ai reports them itself.
PROVIDER_KEYS = {
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "google-gla": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}
DEFAULT_MODEL = "google:gemini-3.6-flash"


def model_problems(model: str) -> list:
    """Why the agent runtime could not use COACH_MODEL, or [] if it can."""
    provider, sep, _ = model.partition(":")
    if not sep:
        return [f"COACH_MODEL={model!r} has no provider prefix. Use "
                "'provider:model', e.g. 'google:gemini-3.6-flash'."]
    keys = PROVIDER_KEYS.get(provider)
    if keys and not any(os.environ.get(k) for k in keys):
        return [f"COACH_MODEL uses provider '{provider}' but {' or '.join(keys)} is "
                "not set. Put it in .env (see .env.example), never in code or chat."]
    return []


class ConfigError(RuntimeError):
    """The instance cannot be used. The message says how to fix it."""


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def lines(self) -> list:
        return ([f"ERROR  {e}" for e in self.errors]
                + [f"WARN   {w}" for w in self.warnings])


def data_dir(explicit: Optional[str] = None) -> str:
    """The data directory: an explicit path, else COACH_DATA_DIR. No silent
    default: a default inside the repo is how personal data gets committed."""
    d = explicit or os.environ.get("COACH_DATA_DIR", "")
    if not d:
        raise ConfigError(
            "COACH_DATA_DIR is not set. Point it at your data directory, e.g. "
            "`export COACH_DATA_DIR=~/.local/share/fitness-coach`, and create it "
            "with `bin/coach init`. For a throwaway demo run `make demo`.")
    return os.path.abspath(os.path.expanduser(d))


def check(directory: str, *, need_db: bool = True) -> Report:
    r = Report()
    if not os.path.isdir(directory):
        r.errors.append(f"{directory} does not exist. Run `bin/coach init --dir {directory}`.")
        return r
    db_path = os.path.join(directory, DB_FILENAME)
    if need_db and not os.path.exists(db_path):
        r.warnings.append(
            f"no {DB_FILENAME} yet in {directory}. Import an export with "
            f"`python scripts/backfill.py --db {db_path} FILE`, or start the "
            "watched-folder importer. The coach has no training data until then.")
    for name in (PROFILE, GYM):
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            r.warnings.append(f"{path} is missing. The coach will ask for it. "
                              "Create it with `bin/coach init`.")
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        todo = sum(1 for line in text.splitlines()
                   if line.strip().lstrip("-* ").startswith(PLACEHOLDER))
        if todo:
            r.warnings.append(f"{path} still has {todo} '{PLACEHOLDER}' line(s) "
                              "from the template. Fill them in or delete them.")
    try:
        aliases.load(directory)
    except aliases.AliasFileError as e:
        r.errors.append(str(e))
    return r


def require(directory: Optional[str] = None, *, need_db: bool = True) -> str:
    """Resolve and validate the data directory, raising ConfigError with every
    error at once. Warnings are returned to the caller via check(), not raised."""
    d = data_dir(directory)
    rep = check(d, need_db=need_db)
    if not rep.ok:
        raise ConfigError("configuration problems:\n  " + "\n  ".join(rep.errors))
    return d
