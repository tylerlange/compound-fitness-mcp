"""Two-step preview/apply gate for destructive tools.

A destructive tool called without a valid token performs no writes. It
returns a preview of exactly what would change plus a one-time token. The
apply call must present that token with identical arguments.

Properties enforced here:
- The token is random (`secrets`), created and stored server-side, so a model
  cannot forge or guess one.
- Single use: a presented token is removed from the store whether or not it
  validates. A wrong-argument or stale attempt burns the token and forces a
  fresh preview.
- Short-lived: default 600 seconds, set with COACH_CONFIRM_TTL_S.
- Bound to the tool name and a SHA-256 of the canonical-JSON arguments, and to
  a fingerprint of the previewed row ids, so data that changed between preview
  and apply is rejected instead of silently widening the write.

What this does NOT prove: that a human said yes. The agent relays the user's
answer, so human confirmation stays a prompt-level control. The gate
guarantees that a preview happens and that apply matches it exactly.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Callable, Optional

DEFAULT_TTL_S = 600
MAX_PENDING = 100


def _digest(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ttl_from_env() -> float:
    try:
        v = float(os.environ.get("COACH_CONFIRM_TTL_S", DEFAULT_TTL_S))
        return v if v > 0 else DEFAULT_TTL_S
    except ValueError:
        return DEFAULT_TTL_S


class ConfirmationGate:
    def __init__(self, ttl_s: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_s = ttl_from_env() if ttl_s is None else ttl_s
        self._clock = clock
        self._pending: dict[str, dict] = {}

    def _purge(self) -> None:
        now = self._clock()
        for t in [t for t, r in self._pending.items() if r["expires"] <= now]:
            del self._pending[t]
        while len(self._pending) >= MAX_PENDING:
            del self._pending[next(iter(self._pending))]  # oldest first

    def issue(self, tool: str, args: dict, plan_ids: dict) -> dict:
        """Register a preview. `plan_ids` is the set of row ids the preview
        lists; it is fingerprinted so apply can detect drift."""
        self._purge()
        token = secrets.token_urlsafe(24)
        self._pending[token] = {
            "tool": tool,
            "args": _digest(args),
            "plan": _digest(plan_ids),
            "expires": self._clock() + self.ttl_s,
        }
        return {"confirm_token": token, "expires_in_s": int(self.ttl_s)}

    def consume(self, tool: str, args: dict, plan_ids: dict,
                token: str) -> Optional[str]:
        """Validate and burn a token. Returns None if valid, else a rejection
        reason. Never writes anything."""
        rec = self._pending.pop(token, None) if token else None
        if rec is None:
            return ("confirmation token is missing, unknown or already used; "
                    "call without a token to get a new preview")
        if rec["expires"] <= self._clock():
            return "confirmation token expired; request a new preview"
        if rec["tool"] != tool or not hmac.compare_digest(
                rec["args"], _digest(args)):
            return ("confirmation token does not match these arguments; "
                    "request a new preview")
        if not hmac.compare_digest(rec["plan"], _digest(plan_ids)):
            return ("stored data changed since the preview; "
                    "request a new preview")
        return None
