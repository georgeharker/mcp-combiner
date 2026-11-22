"""Held calls: a 2026-era client in front of a handshake-era upstream.

A handshake-era upstream asks for input mid-call (``elicitation/create``) and
blocks until it is answered. A 2026-07-28 client cannot receive a
server-initiated request: it is asked by returning an ``InputRequiredResult``
and answers by retrying the call with ``inputResponses``. The combiner bridges
the two by *holding* the upstream call open across the client's requests:

1. ``ToolProcessingMiddleware`` runs the upstream call as a task, under a
   :class:`HeldCall` registered for the chat token and the request id.
2. The upstream's elicitation reaches :func:`bridged_elicitation_handler` (the
   proxy client's handler, with the original request's context restored). For
   a 2026-era request it parks the question on the held call and waits.
3. The middleware sees the question before the task finishes and returns it to
   the client as an ``InputRequiredResult`` whose ``request_state`` names the
   held call (fastmcp seals ``request_state`` on the wire).
4. The client's retry carries the answer; the middleware hands it to the
   waiting handler and waits again — for the result, or another question.

A held call belongs to the chat token that started it: a retry from another
token cannot resume it. It is cancelled after :data:`HOLD_TIMEOUT_S` (60 min by
default, ``MCP_COMBINER_HOLD_TIMEOUT``) or when its client never comes back. Held
calls do not survive a combiner restart; a retry then gets a clear "call the
tool again". Handshake-era requests never touch this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastmcp.client.elicitation import create_elicitation_callback
from fastmcp.client.roots import RootsList, create_roots_callback
from fastmcp.client.sampling import create_sampling_callback
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers.proxy import (
    ProxyClient,
    _make_restoring_handler,
    default_proxy_elicitation_handler,
    default_proxy_roots_handler,
    default_proxy_sampling_handler,
)
from fastmcp.tools.base import InputRequiredToolResult
from mcp.types import (
    CreateMessageRequest,
    CreateMessageRequestParams,
    CreateMessageResult,
    CreateMessageResultWithTools,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitRequestURLParams,
    ElicitResult,
    InputRequest,
    InputRequiredResult,
    InputResponse,
    InputResponses,
    ListRootsRequest,
    ListRootsResult,
    SamplingMessage,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

logger = logging.getLogger("mcp-combiner")


def _hold_timeout() -> float:
    try:
        return float(os.environ["MCP_COMBINER_HOLD_TIMEOUT"])
    except (KeyError, ValueError):
        return 3600.0


#: How long a held call waits for its client to come back with an answer.
HOLD_TIMEOUT_S = _hold_timeout()

STATE_PREFIX = "held:"
_TOKEN_HEADER = "x-mcp-combiner-session"


@dataclass
class _Ask:
    """One question the upstream is blocked on (elicitation, sampling or roots)."""

    key: str
    request: InputRequest
    answer: asyncio.Future[InputResponse | None]


#: Wraps a held call's question in the fastmcp result type of its request
#: (tools/call, resources/read or prompts/get).
Wrap = Callable[[InputRequiredResult], Any]


@dataclass
class HeldCall:
    """An upstream call kept running across a 2026-era client's requests."""

    token: str | None
    origin_request_id: str
    wrap: Wrap = InputRequiredToolResult
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task: asyncio.Task[Any] | None = None
    asks: asyncio.Queue[_Ask] = field(default_factory=asyncio.Queue)
    pending: dict[str, _Ask] = field(default_factory=dict)
    expiry: asyncio.TimerHandle | None = None


_by_id: dict[str, HeldCall] = {}
_by_origin: dict[tuple[str | None, str], HeldCall] = {}


def _register(held: HeldCall) -> None:
    _by_id[held.id] = held
    _by_origin[(held.token, held.origin_request_id)] = held
    held.expiry = asyncio.get_running_loop().call_later(HOLD_TIMEOUT_S, _expire, held.id)


def _forget(held: HeldCall) -> None:
    _by_id.pop(held.id, None)
    if _by_origin.get((held.token, held.origin_request_id)) is held:
        del _by_origin[(held.token, held.origin_request_id)]
    if held.expiry is not None:
        held.expiry.cancel()
    for ask in held.pending.values():
        if not ask.answer.done():
            ask.answer.cancel()


def _expire(held_id: str) -> None:
    held = _by_id.get(held_id)
    if held is None:
        return
    logger.info("held call %s expired waiting for its client; cancelling", held_id[:8])
    _forget(held)
    if held.task is not None and not held.task.done():
        held.task.cancel()


def reset() -> None:
    """Cancel every held call (tests; combiner shutdown)."""
    for held in list(_by_id.values()):
        _forget(held)
        if held.task is not None and not held.task.done():
            held.task.cancel()


def resumes(request_state: str | None) -> bool:
    """Whether a request is the retry of a held call (skip the first-leg gates)."""
    return request_state is not None and request_state.startswith(STATE_PREFIX)


async def start(
    run: Callable[[], Awaitable[Any]],
    token: str | None,
    request_id: str,
    wrap: Wrap = InputRequiredToolResult,
) -> Any:
    """Run an upstream request for a 2026-era client, holding it if it asks.
    *wrap* puts a question in the request's own fastmcp result type."""
    held = HeldCall(token=token, origin_request_id=request_id, wrap=wrap)
    _register(held)

    async def _run() -> Any:
        return await run()

    held.task = asyncio.create_task(_run(), name=f"held-call-{held.id[:8]}")
    return await _await_outcome(held)


async def resume(request_state: str, token: str | None, responses: InputResponses | None) -> Any:
    """Continue a held call from its client's retry."""
    held = _by_id.get(request_state.removeprefix(STATE_PREFIX))
    if held is None or held.token != token:
        raise ToolError(
            "This call was paused waiting for input and is no longer available "
            "(it expired, or the combiner restarted). Call the tool again."
        )
    answered = responses or {}
    logger.debug(
        "held call %s resumed: pending=%s answered=%s",
        held.id[:8],
        sorted(held.pending),
        {k: (type(v).__name__, getattr(v, "action", None)) for k, v in answered.items()},
    )
    for key, ask in list(held.pending.items()):
        if not ask.answer.done():
            ask.answer.set_result(answered.get(key))
        del held.pending[key]
    return await _await_outcome(held)


async def _await_outcome(held: HeldCall) -> Any:
    """The call's result, or an InputRequiredResult for its next question."""
    task = held.task
    if task is None:
        raise RuntimeError("held call has no task (start() sets it before waiting)")
    next_ask = asyncio.ensure_future(held.asks.get())
    try:
        done, _ = await asyncio.wait({task, next_ask}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not next_ask.done():
            next_ask.cancel()
    if next_ask in done and not task.done():
        ask = next_ask.result()
        held.pending[ask.key] = ask
        return held.wrap(
            InputRequiredResult(
                input_requests={ask.key: ask.request},
                request_state=STATE_PREFIX + held.id,
            )
        )
    if next_ask in done:  # both finished: the question is moot
        ask = next_ask.result()
        if not ask.answer.done():
            ask.answer.cancel()
    _forget(held)
    return task.result()  # re-raises the upstream call's own exception


async def _park(held: HeldCall, kind: str, request: InputRequest) -> InputResponse | None:
    """Hand *request* to the held call's client; its answer, or ``None`` when
    it never came (expired, abandoned, or the retry didn't answer it)."""
    ask = _Ask(
        key=f"{kind}-{uuid.uuid4().hex[:8]}",
        request=request,
        answer=asyncio.get_running_loop().create_future(),
    )
    await held.asks.put(ask)
    try:
        return await ask.answer
    except asyncio.CancelledError:
        return None


async def bridged_elicitation_handler(
    message: str,
    response_type: type,
    params: ElicitRequestParams,
    context: Any,
) -> ElicitResult:
    """The proxy client's elicitation handler (with the downstream request's
    context restored): 2026-era requests park the question on their held call;
    handshake-era requests forward it live, as fastmcp does by default."""
    held = _held_for_current_request()
    if held is None:
        return await default_proxy_elicitation_handler(message, response_type, params, context)
    if isinstance(params, ElicitRequestURLParams):
        # 2026 URL asks carry no elicitationId: the upstream's stays here.
        request = ElicitRequest(params=ElicitRequestURLParams(message=message, url=params.url))
    else:
        request = ElicitRequest(
            params=ElicitRequestFormParams(
                message=message, requested_schema=params.requested_schema
            )
        )
    answer = await _park(held, "elicit", request)
    # Unanswered: tell the upstream the user didn't answer.
    return answer if isinstance(answer, ElicitResult) else ElicitResult(action="cancel")


async def bridged_sampling_handler(
    messages: list[SamplingMessage],
    params: CreateMessageRequestParams,
    context: Any,
) -> CreateMessageResult | CreateMessageResultWithTools:
    """The proxy client's sampling handler: parked on a 2026-era request's
    held call, forwarded live otherwise."""
    held = _held_for_current_request()
    if held is None:
        return await default_proxy_sampling_handler(messages, params, context)
    answer = await _park(held, "sample", CreateMessageRequest(params=params))
    if isinstance(answer, (CreateMessageResult, CreateMessageResultWithTools)):
        return answer
    # create_sampling_callback turns this into an error for the upstream.
    raise RuntimeError("The client did not answer the sampling request")


async def bridged_roots_handler(context: Any) -> RootsList:
    """The proxy client's roots handler: parked on a 2026-era request's held
    call, forwarded live otherwise."""
    held = _held_for_current_request()
    if held is None:
        return await default_proxy_roots_handler(context)
    answer = await _park(held, "roots", ListRootsRequest())
    if isinstance(answer, ListRootsResult):
        roots: RootsList = list(answer.roots)
        return roots
    raise RuntimeError("The client did not answer the roots request")


def _held_for_current_request() -> HeldCall | None:
    try:
        rc = get_context().request_context
    except Exception:
        return None
    if rc is None or rc.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return None
    if rc.request_id is None:
        return None
    token = rc.request.headers.get(_TOKEN_HEADER) if rc.request is not None else None
    return _by_origin.get((token or None, str(rc.request_id)))


def bind_bridged_asks(client: ProxyClient[Any]) -> None:
    """Route *client*'s upstream elicitation, sampling and roots requests
    through the bridged handlers above.

    Only for the ones fastmcp installed its own default forwarding handler for
    (a caller that passed an explicit handler keeps it). Call after
    construction AND from ``_bind_restoring_handlers`` overrides, since a
    per-chat ``Client.new()`` clone rebinds the defaults.
    """
    keys = client._proxy_restoring_handler_keys
    rc_ref = client._proxy_rc_ref
    if "elicitation_handler" in keys:
        client._session_kwargs["elicitation_callback"] = create_elicitation_callback(
            _make_restoring_handler(bridged_elicitation_handler, rc_ref)
        )
    if "sampling_handler" in keys:
        client._session_kwargs["sampling_callback"] = create_sampling_callback(
            _make_restoring_handler(bridged_sampling_handler, rc_ref)
        )
    if "roots" in keys:
        client._session_kwargs["list_roots_callback"] = create_roots_callback(
            _make_restoring_handler(bridged_roots_handler, rc_ref)
        )


#: The pre-0.25 name (elicitation was the only ask held calls carried).
bind_bridged_elicitation = bind_bridged_asks
