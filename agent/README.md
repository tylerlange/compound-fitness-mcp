# agent/

The coaching agent runtime.

| File | Role |
|---|---|
| `mcp_server.py` | MCP server (stdio) exposing the coaching tools. See its header for the pattern to add a tool. |
| `coach.py` | The Pydantic-AI agent: model from `COACH_MODEL`, system prompt from `persona-system-prompt.md`, tools discovered over MCP. |
| `service.py` | FastAPI app: OpenAI-compatible `/v1/chat/completions` (streaming and non-streaming), `/v1/models`, `/health`. One persistent MCP connection for the life of the process. |
| `Dockerfile` | Build from the repo root: `docker build -f agent/Dockerfile -t coach-agent .` |
| `requirements.txt` | Runtime dependencies, pinned. |

## Run

```bash
python scripts/make_synthetic_data.py --out ./demo-data   # or use your own data dir
export COACH_DATA_DIR=./demo-data
pip install -r agent/requirements.txt
cp .env.example .env                                    # fill in a model API key
set -a; . ./.env; set +a
cd agent && uvicorn service:app --port 8000
```

Settings come from the environment (see `.env.example`):

- `COACH_DATA_DIR`: directory with `training.db`, `gym.md`, `coaching-profile.md`.
- `COACH_MODEL`: any pydantic-ai model string. Swap models without a rebuild.
- The provider key for that model (for example `GOOGLE_API_KEY`). Never commit it.
- `PERSONA_PROMPT_PATH`, `COACH_CORE_DIR`: optional path overrides.

## Verify

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"fitness-coach","messages":[{"role":"user","content":"How is my training consistency? One line."}]}'
```

The service logs `tool-call name=... args=...` for every tool call, which is
the proof a number came from a tool and not from memory.

## Data directory permissions

Read tools open the database with `mode=ro` regardless of file permissions.
Write tools (`set_away_period`, `remove_away_period`, `add_coaching_note`,
`remove_coaching_note`, `merge_exercises`, `remove_import`) need the file and
its directory to be writable. The two destructive tools preview on a read-only
connection first, so a preview works even on a read-only mount; only the apply
step needs write access. If you mount the data directory read-only in a
container, every write tool returns an error string and the read tools keep
working. That is a useful way to run a read-only coach.

## Adding a tool

1. Add the logic to `strength_coach/` with a unit test.
2. Wrap it in `mcp_server.py` following the pattern in its header.
3. Add the name to `EXPECTED_TOOLS` in `tests/test_mcp_server.py`.
4. Update `docs/TOOLS.md`.
5. Add an eval case if the tool encodes a behavior the model could get wrong.
