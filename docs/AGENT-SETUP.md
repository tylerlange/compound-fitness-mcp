# Agent setup guide

For a coding agent setting up a fitness-coach-agent instance for a new user.
Follow the steps in order. Do not read the source code unless a step fails
and its error message does not say how to fix it; if you had to, tell the user
which step so the guide can be fixed.

How to hand this file to your agent: in Claude Code or Codex, run it from the
repo root and say "Follow docs/AGENT-SETUP.md to set me up." For any other
agent, paste this file and give it shell access to the clone.

Ground rules for you, the agent:

- Instance files live outside the repo. Never write the user's data, profile or
  keys inside the checkout, and never commit them.
- Never ask the user to paste a secret (API key, password, token) into chat.
  Tell them which file to put it in and to set it to mode 0600.
- Everything you write into the profile, gym and aliases files comes from the
  user's answers. Do not invent equipment, goals or injuries.

## 1. Check prerequisites

```bash
python3 --version          # needs 3.10+
python3 -c "import pytest, openpyxl, mcp" || pip install -r requirements-dev.txt
make check                 # must pass before you change anything
```

If `pip` is unavailable, tell the user; do not install system packages yourself.

## 2. Run init

Ask where the data should live; suggest `~/.local/share/fitness-coach`. Ask
whether one of the examples is close to them (`examples/README.md` lists
three); if so add `--example NAME`.

```bash
bin/coach init --dir ~/.local/share/fitness-coach [--example NAME]
export COACH_DATA_DIR=~/.local/share/fitness-coach
```

`init` never overwrites an existing file and refuses a path inside the repo.

## 3. Interview the user and fill each file

Ask one section at a time. Replace every `REPLACE:` line with their answer or
delete it. Keep their words; do not pad.

**`gym.md`**

1. Where do you train (home, commercial gym, work gym, hotel gyms when travelling)?
2. Racks, benches and machines you can use?
3. Bars, plates, and the heaviest dumbbell per hand?
4. Cable stacks: how many, and the max on each?
5. Anything you have to avoid or can't load past (low ceiling, no spotter)?

**`coaching-profile.md`**

1. How do you want to be coached: blunt or gentle, short or detailed, who decides?
2. Training history: how long, which apps, any long breaks?
3. Injuries, pain, or movements to avoid? (Record them; the coach steers
   around them and does not diagnose.)
4. Training days per week, session length, time of day?
5. Cardio: what kind (walks, runs, rides, hikes, classes) and how often?
6. What usually breaks your routine?
7. Goals, in priority order, and any dated event. Is "consistency first" your
   north star, or is something else (strength, a race, body composition)?
8. Should the coach bring up nutrition?

**`aliases.yaml`**

1. Which apps have you logged lifts in, and over what years?
2. Import a copy of each export into a throwaway store and list the names:
   `python scripts/backfill.py --db /tmp/probe.db EXPORT [EXPORT ...]`, then
   `bin/coach names --db /tmp/probe.db`. It shows each exercise with the raw
   name every app used, and pairs that may be the same lift.
3. For each pair, ask: "Are these the same lift for you?" Only add a line when
   they say yes. Map to the canonical name directly. Delete `/tmp/probe.db`.

## 4. Validate the config

```bash
bin/coach check
```

Fix every `ERROR`. `WARN` lines are acceptable only if the user chose to leave
that section empty, and a missing `training.db` before step 6.

## 5. Run the demo

```bash
make demo
```

This uses fictional data in `./demo-data` and needs no key. Show the user the
`bin/coach context` output it prints and explain that this is what the coach
receives about them once their own data is in.

## 6. First real import, as a dry run first

```bash
python scripts/backfill.py --db /tmp/dryrun.db EXPORT [EXPORT ...]   # dry run: throwaway store
python scripts/backfill.py --db "$COACH_DATA_DIR/training.db" EXPORT [EXPORT ...]
bin/coach check && bin/coach context | less
```

Show the user the per-source counts and the near-match suggestions. If a
suggestion is a lift they consider the same, go back to the aliases step,
then re-run the real import (re-importing is idempotent). Delete `/tmp/dryrun.db`.

For ongoing imports, the watched-folder importer is `python importer/watcher.py`
with `INBOX_DIR` (default `$COACH_DATA_DIR/inbox`); see importer/README.md.

## 7. Report what is left for the human

Tell the user, and stop:

- **Model key** (only to chat with the coach): copy `.env.example` to `.env`
  in the repo root (git-ignored), set `COACH_DATA_DIR`, `COACH_MODEL` and that
  provider's key in it, then `chmod 600 .env`. You must not see the key.
- **Agent runtime**: `pip install -r agent/requirements.txt`, then
  `set -a; . ./.env; set +a; cd agent && uvicorn service:app --port 8000`.
- **Identifier denylist** (if they will publish a fork): create
  `.identifiers.local` with their name, city, gym and app accounts, one per
  line, and run `make hooks`.
- Anything you could not complete, with the exact error.
