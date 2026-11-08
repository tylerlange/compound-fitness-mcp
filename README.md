# Compound

## What it is

Your lifts live in one app and your runs in another. Compound merges your saved workouts from each into one standardized history, then serves it to your AI over MCP, so it has a single source of truth for feedback and analysis.

Ask things like "How has my squat moved this year?" or "Am I training more consistently than last quarter?" and get answers from your own numbers.

It works from the exports you download, not live connections to your apps. Your history lives in one local file.

**Features.**
- Imports MacroFactor Workouts, StrongLifts, Liftin, Liftosaur and AllTrails (GPX).
- Matches the same exercise across apps.
- Works with any OpenAI-compatible or MCP client.

## Architecture

```
app exports -> adapters -> SQLite -> MCP tool server -> coach agent
```

- **Adapters.** One parser per app. No AI in the import path.
- **Store.** One SQLite file. Trends computed on read.
- **Tools.** 22 MCP tools. Reads are read-only. Deletes and merges need a preview token.
- **Coach.** Pydantic-AI agent on an OpenAI-compatible endpoint. Gemini by default.

Details: [ARCHITECTURE](docs/ARCHITECTURE.md), [REFERENCE](docs/REFERENCE.md), [TOOLS](docs/TOOLS.md).

## Install

Python 3.10+.

```bash
# "Use this template" on GitHub, then:
git clone https://github.com/<you>/<your-copy>.git && cd <your-copy>
pip install -r requirements-dev.txt
make demo                                   # synthetic data, no key

bin/coach init --dir ~/.local/share/fitness-coach
export COACH_DATA_DIR=~/.local/share/fitness-coach
$EDITOR "$COACH_DATA_DIR"/{coaching-profile.md,gym.md,aliases.yaml}
bin/coach check

python scripts/backfill.py --db "$COACH_DATA_DIR/training.db" path/to/export
bin/coach context | less                    # what the coach sees
```

To chat: `pip install -r agent/requirements.txt`, set `GOOGLE_API_KEY` in `.env`, run `make demo-agent`, and point any OpenAI-compatible client at `http://localhost:8000/v1`.

Agent-assisted setup: [AGENT-SETUP](docs/AGENT-SETUP.md). New app: [ADAPTERS](docs/ADAPTERS.md).

## About

Built by Tyler Lange directing Claude Code, gated by tests and evals. MIT license.
