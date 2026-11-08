# fitness-coach-agent

## What it is

**Problem.** Workout data lives in separate apps: one for lifting, one for cardio, another for recovery. None of them can answer a question that spans all of it, and general AI chatbots guess at your numbers.

**How this solves it.** It imports each app's exports into one training history on your machine, then gives an AI coach tools to query that history. Answers come from your actual data.

**What it does.**
- Imports exports from MacroFactor, StrongLifts, Liftin, Liftosaur and AllTrails (GPX), and matches the same exercise across apps.
- Answers questions about progress, trends, consistency, cardio and recovery, through any OpenAI-compatible chat client or MCP client.
- Keeps your data in a folder on your machine, outside the repo.

## Architecture

```
app exports -> adapters -> SQLite store -> MCP tool server -> coach agent
```

- **Adapters.** One plain parser per app. No AI model touches the import path, so the history can always be rebuilt from the exports.
- **Store.** One SQLite file. Trends and estimated one-rep maxes are computed when asked, not stored.
- **Tool server.** 22 MCP tools ([docs/TOOLS.md](docs/TOOLS.md)). Read tools open the database read-only. The two tools that remove or merge data require a preview and a one-time token first, enforced in code.
- **Coach.** A Pydantic-AI agent behind `/v1/chat/completions`. `COACH_MODEL` picks the model (Gemini by default).

More: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design decisions, [docs/REFERENCE.md](docs/REFERENCE.md) for guardrails, evals and configuration.

## Install

Python 3.10+.

```bash
# Use GitHub's "Use this template" button (not Fork), then:
git clone https://github.com/<you>/<your-copy>.git && cd <your-copy>
pip install -r requirements-dev.txt

# Demo with synthetic data. No key, no network.
make demo

# Your own instance, outside the repo.
bin/coach init --dir ~/.local/share/fitness-coach
export COACH_DATA_DIR=~/.local/share/fitness-coach
$EDITOR "$COACH_DATA_DIR"/{coaching-profile.md,gym.md,aliases.yaml}
bin/coach check

# Import your exports, then see what the coach will see.
python scripts/backfill.py --db "$COACH_DATA_DIR/training.db" path/to/your-export
bin/coach context | less
```

To talk to the coach, install `agent/requirements.txt`, put the model's API key in `.env` (`GOOGLE_API_KEY` for the default), and run `make demo-agent`. Point any OpenAI-compatible client at `http://localhost:8000/v1`, or any MCP client at `agent/mcp_server.py`. [docs/AGENT-SETUP.md](docs/AGENT-SETUP.md) lets a coding agent do the setup for you.

To add an app, see [docs/ADAPTERS.md](docs/ADAPTERS.md).

## About

Built by Tyler Lange directing Claude Code, with the unit tests and eval suite as the acceptance gate. MIT license.
