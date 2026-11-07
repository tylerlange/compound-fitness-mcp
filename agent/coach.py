"""The Pydantic-AI coaching agent: model config, system prompt, MCP wiring.

The model is swappable via the COACH_MODEL env var, any pydantic-ai model
string ("google:...", "anthropic:...", "openai:..."), with the matching
provider key in the environment. The agent has NO tools of its own: it
discovers and calls everything over MCP against mcp_server.py, spawned as a
stdio subprocess.

service.py owns the connection lifecycle: it enters `async with agent:` once
in the FastAPI lifespan (startup) and exits when the app stops, so the MCP
subprocess is spawned once and reused across requests, not spawned per request.
"""
import logging
import os
import sys
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import FastMCPClient, MCPToolset, StdioTransport

logger = logging.getLogger("coach")

MODEL = os.environ.get("COACH_MODEL", "google:gemini-3.6-flash")

sys.path.insert(
    0, os.environ.get("COACH_CORE_DIR", str(Path(__file__).resolve().parent.parent)))
from strength_coach import config as scconfig  # noqa: E402

def validate_startup() -> None:
    """Fail at service start, with the fix, instead of on the first request."""
    problems = []
    try:
        scconfig.require()
    except scconfig.ConfigError as e:
        problems.append(str(e))
    problems += scconfig.model_problems(MODEL)
    if problems:
        raise RuntimeError("coach agent cannot start:\n  " + "\n  ".join(problems))

# The persona is loaded at import time from persona-system-prompt.md. That
# file's body starts after the first "\n---\n" line; everything above the
# separator is developer notes and is NOT part of the prompt.
PERSONA_PATH = Path(
    os.environ.get(
        "PERSONA_PROMPT_PATH",
        str(Path(__file__).resolve().parent.parent / "persona-system-prompt.md"),
    )
)

_PERSONA_SEPARATOR = "\n---\n"


def _load_system_prompt(path: Path) -> str:
    text = path.read_text()
    _developer_notes, sep, body = text.partition(_PERSONA_SEPARATOR)
    if not sep:
        raise RuntimeError(
            f"{path} has no {_PERSONA_SEPARATOR!r} separator -- can't split "
            "developer notes from the persona body."
        )
    return body.strip()


SYSTEM_PROMPT = _load_system_prompt(PERSONA_PATH)
logger.info("loaded persona system prompt from %s (%d chars)", PERSONA_PATH, len(SYSTEM_PROMPT))

MCP_SERVER_PATH = Path(__file__).parent / "mcp_server.py"

client = FastMCPClient(StdioTransport(sys.executable, args=[str(MCP_SERVER_PATH)]))
toolset = MCPToolset(client)

agent = Agent(MODEL, toolsets=[toolset], system_prompt=SYSTEM_PROMPT)
