"""Asking the user whether to restart a server that's alive but not answering.

Design decision 01M70HS1Q6: the health monitor restarts a server on its own
only when it is GONE (its port refuses connections). One that is still
running but has stopped answering is never killed automatically: a restart
ends its in-flight work and every chat's state on it, and only the user can
judge whether that's worth it. The next tool call routed to it asks instead
(the same two-era shape as the permission prompt in ``permissions``):

- handshake-era clients get an ``elicit``;
- 2026-era clients get an ``InputRequiredResult`` and retry with the answer.

"Restart now" runs the restart routine and fails the call (it never ran) with
a note to retry. "Not now" fails the call and silences the question for that
server for :data:`COOL_OFF_S`. One question per server at a time; a client
that can't elicit is told to use ``combiner__restart_server``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation, parse_elicit_response_type
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")

RESTART = "Restart now"
NOT_NOW = "Not now"
_OPTIONS = [RESTART, NOT_NOW]
_INPUT = "restart"
_SCHEMA: dict[str, Any] = parse_elicit_response_type(_OPTIONS).schema

#: After "Not now", calls to that server fail without asking for this long.
COOL_OFF_S = 600.0

_declined_until: dict[str, float] = {}
_asking: set[str] = set()


def reset() -> None:
    """Forget every decline and in-flight question (tests; a server recovered)."""
    _declined_until.clear()
    _asking.clear()


def recovered(server: str) -> None:
    """*server* answered again: a later stall asks afresh."""
    _declined_until.pop(server, None)


def message(server: str) -> str:
    return (
        f"'{server}' has stopped responding: its process is running, but it hasn't "
        "answered for several minutes. Restart it? Restarting ends what it's doing "
        "and resets every chat's state on it (open projects, documents)."
    )


def _state(server: str) -> str:
    return f"restart:{server}"


def _is_2026_request(ctx: Any) -> bool:
    rc = getattr(ctx, "request_context", None)
    return rc is not None and rc.protocol_version in MODERN_PROTOCOL_VERSIONS


async def gate(ctx: Any, server: str) -> InputRequiredResult:
    """Handle a call routed to unresponsive *server*. Returns the 2026-era
    question (the call's result); otherwise always raises ``ToolError``: the
    call can't run against a server that isn't answering."""
    now = time.monotonic()
    if _declined_until.get(server, 0.0) > now:
        raise ToolError(
            f"'{server}' isn't responding (still running, but not answering), and a "
            f"restart was declined a few minutes ago. Run combiner__restart_server"
            f"('{server}') to restart it now."
        )
    if _is_2026_request(ctx):
        answer = _answer(ctx, server)
        if answer is None:
            return InputRequiredResult(
                input_requests={
                    _INPUT: ElicitRequest(
                        params=ElicitRequestFormParams(
                            message=message(server), requested_schema=_SCHEMA
                        )
                    )
                },
                request_state=_state(server),
            )
        return await _decide(server, answer)
    if server in _asking:
        raise ToolError(
            f"'{server}' isn't responding; whether to restart it is being asked in "
            "another chat. Try again shortly."
        )
    _asking.add(server)
    try:
        result = await ctx.elicit(message(server), response_type=_OPTIONS)
    except Exception as exc:  # noqa: BLE001 — a client that can't elicit
        logger.info("restart prompt for '%s' unavailable (%s)", server, exc)
        raise ToolError(
            f"'{server}' isn't responding (still running, but not answering for "
            f"several minutes). Run combiner__restart_server('{server}') to restart "
            "it; that resets every chat's state on it."
        ) from None
    finally:
        _asking.discard(server)
    choice = result.data if isinstance(result, AcceptedElicitation) else NOT_NOW
    return await _decide(server, choice)


def _answer(ctx: Any, server: str) -> str | None:
    """The choice a 2026-era retry carries for *server*, if this is that retry."""
    if getattr(ctx, "request_state", None) != _state(server):
        return None
    answer = (getattr(ctx, "input_responses", None) or {}).get(_INPUT)
    if not isinstance(answer, ElicitResult):
        return None
    if answer.action != "accept":
        return NOT_NOW
    value = (answer.content or {}).get("value")
    return value if isinstance(value, str) else NOT_NOW


async def _decide(server: str, choice: object) -> InputRequiredResult:
    if choice != RESTART:
        _declined_until[server] = time.monotonic() + COOL_OFF_S
        logger.info("restart of unresponsive '%s' declined by the user", server)
        raise ToolError(f"'{server}' isn't responding, and wasn't restarted.")
    routine = RUNTIME.restart_server
    if routine is None:
        raise ToolError(f"'{server}' isn't responding, and no restart routine is available.")
    logger.warning("restarting unresponsive '%s' at the user's request", server)
    summary = await routine(server)
    raise ToolError(f"Restarted '{server}' at your request: {summary} Retry the call.")
