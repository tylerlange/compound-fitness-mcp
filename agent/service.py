"""OpenAI-compatible chat-completions endpoint backed by the coaching agent.

Any OpenAI-compatible chat client can talk to this: point its base URL at
`/v1`. Supports non-streaming and streaming (`"stream": true`, SSE) responses.

Persistent MCP connection: the `lifespan` below enters `async with agent:`
ONCE at service startup, which spawns the mcp_server.py stdio subprocess a
single time and keeps it alive for the life of the process. Every request
reuses that connection; there is no per-request subprocess spawn. If that
ever has to change (for example because concurrent requests contend for one
stdio pipe), it is a contract change and must be called out, not done quietly.

Other behavior:
  - The full request `messages` history is threaded into pydantic-ai via
    `message_history` (see `_split_messages`). The agent owns its own system
    prompt, so request-supplied system messages are dropped rather than
    double-injected.
  - Retry-on-empty / transient-error wrapping (`_run_with_retry`,
    `_stream_chat`) guards against hosted models intermittently returning an
    empty turn under load. A normal first-try response is unaffected.
  - Tool calls are logged at INFO.
"""
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)

from coach import agent, validate_startup  # noqa: E402  (logging must be configured first)
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

logger = logging.getLogger("service")

MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.75


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_startup()
    async with agent:
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "time": int(time.time())}


@app.get("/v1/models")
async def list_models():
    """Minimal OpenAI-compatible model list so any OpenAI client can discover
    the coach as a selectable model."""
    return {
        "object": "list",
        "data": [
            {"id": "coach-agent", "object": "model", "created": 0,
             "owned_by": "coach-agent"}
        ],
    }


def _split_messages(messages: list[dict]) -> tuple[list[ModelMessage], str]:
    """Split OpenAI-style request `messages` into pydantic-ai `message_history`
    (every prior user/assistant turn) plus the latest user turn, which is
    passed separately as `agent.run`'s/`agent.run_stream`'s `user_prompt`.

    Request `system`-role messages are dropped, not translated to a
    SystemPromptPart -- the agent owns its own system prompt (loaded once in
    coach.py from persona-system-prompt.md) and must not have it
    double-injected from the request on top of that.
    """
    last_user_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            last_user_idx = i
    if last_user_idx is None:
        return [], ""

    history: list[ModelMessage] = []
    for m in messages[:last_user_idx]:
        role = m.get("role")
        content = m.get("content", "")
        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=content)]))
        # system-role messages: intentionally dropped, see docstring above.

    latest_prompt = messages[last_user_idx].get("content", "")
    return history, latest_prompt


def _log_tool_calls(result) -> None:
    """Logs every tool call the agent made this run, plus final usage/timing,
    as clean single-line INFO records -- so the container log (or any log
    aggregator) can audit coach behavior without flipping global debug."""
    for m in result.all_messages():
        for part in getattr(m, "parts", []):
            if "ToolCall" in type(part).__name__:
                logger.info(
                    "tool-call name=%s args=%s", part.tool_name, part.args
                )
    usage = getattr(result, "usage", None)
    usage = usage() if callable(usage) else usage
    if usage is not None:
        logger.info(
            "run-usage requests=%s input_tokens=%s output_tokens=%s tool_calls=%s",
            getattr(usage, "requests", None),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "tool_calls", None),
        )


def _is_blank(content) -> bool:
    return not content or not str(content).strip()


async def _run_with_retry(history: list[ModelMessage], user_prompt: str):
    """Runs the agent once; retries up to MAX_ATTEMPTS total on a blank
    output or a transient exception (e.g. Gemini 503/429 under load), with a
    short linear backoff between attempts. A normal, non-empty first attempt
    returns immediately -- no extra model calls, no double charging."""
    result = None
    last_exc: Optional[BaseException] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await agent.run(user_prompt, message_history=history)
        except Exception as exc:  # noqa: BLE001 -- broad on purpose, insurance layer
            last_exc = exc
            logger.warning(
                "agent.run attempt %d/%d raised %s: %s",
                attempt, MAX_ATTEMPTS, type(exc).__name__, exc,
            )
        else:
            last_exc = None
            content = getattr(result, "output", getattr(result, "data", ""))
            if not _is_blank(content):
                return result
            logger.warning(
                "agent.run attempt %d/%d returned a blank response",
                attempt, MAX_ATTEMPTS,
            )
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * attempt)

    if last_exc is not None:
        raise last_exc
    return result  # exhausted retries on blank output with no exception; give up


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "fitness-coach")
    history, user_prompt = _split_messages(body.get("messages", []))

    if body.get("stream", False):
        return StreamingResponse(
            _stream_chat(history, user_prompt, model), media_type="text/event-stream"
        )

    result = await _run_with_retry(history, user_prompt)
    _log_tool_calls(result)
    content = getattr(result, "output", getattr(result, "data", ""))
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


async def _stream_chat(history: list[ModelMessage], user_prompt: str, model: str):
    """SSE contract: each event is `data: {chunk}\\n\\n` where `{chunk}` is an
    OpenAI chat-completion-chunk object (`choices[0].delta.content` carries
    the incremental text), terminated by a final `data: [DONE]\\n\\n`.

    Retry-on-empty/transient-error is best-effort here: once a non-empty
    delta has actually been sent to the client, that text is out the door and
    can't be un-sent, so a mid-stream failure or trailing blank stream is
    logged and the stream ends -- it is NOT silently retried. Retries only
    kick in while an attempt has produced zero visible content so far, which
    keeps a normal streaming run completely unaffected."""
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def _sse(delta: dict, finish_reason: Optional[str] = None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }
        return f"data: {json.dumps(payload)}\n\n"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        sent_content = False
        try:
            async with agent.run_stream(user_prompt, message_history=history) as result:
                async for text in result.stream_text(delta=True):
                    if text:
                        sent_content = True
                        yield _sse({"content": text})
                _log_tool_calls(result)
        except Exception as exc:  # noqa: BLE001 -- broad on purpose, insurance layer
            logger.warning(
                "run_stream attempt %d/%d raised %s: %s",
                attempt, MAX_ATTEMPTS, type(exc).__name__, exc,
            )
            if sent_content or attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * attempt)
            continue
        else:
            if sent_content or attempt == MAX_ATTEMPTS:
                break
            logger.warning(
                "run_stream attempt %d/%d produced no content, retrying",
                attempt, MAX_ATTEMPTS,
            )
            await asyncio.sleep(RETRY_BASE_DELAY_SECONDS * attempt)

    yield _sse({}, finish_reason="stop")
    yield "data: [DONE]\n\n"
