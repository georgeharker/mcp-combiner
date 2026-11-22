"""Era-neutral asks: what the combiner does when one side asks for input and
the other side speaks the other protocol era.

The handshake era asks with a server-initiated request (``elicitation/create``,
``sampling/createMessage``, ``roots/list``) and blocks until it is answered.
The 2026-07-28 revision asks by *returning* an ``InputRequiredResult``; the
client answers by retrying the request with ``inputResponses`` and the echoed
``requestState``. Same era on both sides needs nothing: fastmcp's proxy relays
the asks unchanged. This module holds the parts that cross eras:

- **A handshake client, a 2026 upstream** (:func:`drive_upstream_asks`): each
  request in the upstream's ``InputRequiredResult`` is put to the client live
  (:func:`ask_live`), and the request is repeated upstream with the answers
  until it returns a real result. A URL-mode ask the user accepted is not asked
  again while the browser step runs: the combiner re-polls the upstream for up
  to :func:`url_ask_wait` seconds, then hands the client ``-32042``.
- **Continuation state for 2026 clients** (:func:`wrap_state`, :func:`classify`):
  an upstream's ``requestState`` goes downstream as ``up:<server>:<state>`` so
  it can't collide with the combiner's own continuations (``held:``,
  ``consent:``, ``restart:``, ``redo:``) or be replayed against another server.
- **The wire params** a retry's answers ride on (:func:`set_continuation`).

The other direction (a 2026 client, a handshake upstream) is
:mod:`mcp_combiner.heldcalls`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.prompts.base import InputRequiredPromptResult
from fastmcp.resources.base import InputRequiredResourceResult
from fastmcp.server.context import Context
from fastmcp.tools.base import InputRequiredToolResult
from mcp.shared.exceptions import UrlElicitationRequiredError

logger = logging.getLogger("mcp-combiner")

#: Bound on ask rounds for one request (an upstream that keeps asking). A URL
#: ask re-polled while its browser step runs doesn't count: time bounds that.
MAX_INPUT_ROUNDS = 8

UP_PREFIX = "up:"
REDO_PREFIX = "redo:"
HELD_PREFIX = "held:"

#: Any of fastmcp's "this leg asks for input" results.
InputRequiredAny = (InputRequiredToolResult, InputRequiredResourceResult, InputRequiredPromptResult)

R = TypeVar("R")


def url_ask_wait() -> float:
    """How long an accepted URL ask is re-polled before falling back to -32042
    (``MCP_COMBINER_URL_ASK_WAIT`` seconds, default 300)."""
    try:
        return max(0.0, float(os.environ["MCP_COMBINER_URL_ASK_WAIT"]))
    except (KeyError, ValueError):
        return 300.0


# ── continuation state (2026 clients) ─────────────────────────────


def wrap_state(server: str, state: str | None) -> str:
    """The ``requestState`` a 2026 client gets for *server*'s own *state*.
    Always a string, so the retry is recognised as a continuation even when
    the upstream sent no state of its own."""
    return f"{UP_PREFIX}{server}" if state is None else f"{UP_PREFIX}{server}:{state}"


def redo_state(server: str) -> str:
    """A continuation that re-runs the request fresh (after a URL step)."""
    return f"{REDO_PREFIX}{server}"


Kind = Literal["first", "held", "up", "redo", "own"]


@dataclass(frozen=True)
class Continuation:
    """What a 2026-era request's ``requestState`` says it continues."""

    kind: Kind
    rest: str = ""  # after the prefix: "<server>[:<state>]" for up/redo

    def owned_by(self, server: str | None) -> bool:
        """Whether this up:/redo: continuation belongs to *server*."""
        return server is not None and (self.rest == server or self.rest.startswith(server + ":"))

    def inner(self, server: str) -> str | None:
        """The upstream's own state inside an ``up:`` continuation."""
        tail = self.rest[len(server) :]
        return tail[1:] if tail.startswith(":") else None

    @property
    def skips_gates(self) -> bool:
        """The first leg already passed consent and input validation."""
        return self.kind in ("held", "up", "redo")


def classify(state: str | None) -> Continuation:
    if state is None:
        return Continuation("first")
    for prefix, kind in ((HELD_PREFIX, "held"), (UP_PREFIX, "up"), (REDO_PREFIX, "redo")):
        if state.startswith(prefix):
            return Continuation(kind, state[len(prefix) :])  # type: ignore[arg-type]
    return Continuation("own")  # consent:, restart:, or nothing we recognise


def input_required_of(result: object) -> mt.InputRequiredResult | None:
    """The ``InputRequiredResult`` a leg's result carries, if it asks."""
    if isinstance(result, InputRequiredAny):
        return result.input_required
    return None


def rewrap(result: object, ask: mt.InputRequiredResult) -> Any:
    """*ask* in the same fastmcp wrapper as *result*."""
    if isinstance(result, InputRequiredResourceResult):
        return InputRequiredResourceResult(ask)
    if isinstance(result, InputRequiredPromptResult):
        return InputRequiredPromptResult(ask)
    return InputRequiredToolResult(ask)


def wrap_upstream(result: R, server: str) -> R:
    """Namespace an upstream ask's ``requestState`` for a 2026 client."""
    ask = input_required_of(result)
    if ask is None:
        return result
    wrapped = mt.InputRequiredResult(
        input_requests=ask.input_requests, request_state=wrap_state(server, ask.request_state)
    )
    return rewrap(result, wrapped)  # type: ignore[no-any-return]


def url_asks(error: UrlElicitationRequiredError, server: str) -> mt.InputRequiredResult:
    """A ``-32042`` as a 2026-era ask: the URL elicitations as input requests;
    the retry (``redo:``) runs the request again once the user has been."""
    requests: dict[str, mt.InputRequest] = {}
    for i, el in enumerate(error.elicitations):
        requests[f"url-{i}"] = mt.ElicitRequest(
            params=mt.ElicitRequestURLParams(message=el.message, url=el.url)
        )
    return mt.InputRequiredResult(input_requests=requests, request_state=redo_state(server))


# ── the wire request a retry's answers ride on ─────────────────────


def wire_params(fctx: Context | None) -> dict[str, Any] | None:
    """The current wire request's raw params dict, shared by every fastmcp
    ``Context`` of this request (the mounted proxy's included): it is where
    ``input_responses`` / ``request_state`` are parsed from."""
    if fctx is None:
        return None
    rc = fctx.request_context
    params = rc._srctx.params if rc is not None else None
    return params if isinstance(params, dict) else None


def set_continuation(
    params: dict[str, Any] | None, responses: dict[str, Any] | None, state: str | None
) -> None:
    """Make the request carry *responses* / *state* (``None`` removes them)."""
    if params is None:
        return
    if responses is None:
        params.pop("inputResponses", None)
    else:
        params["inputResponses"] = responses
    if state is None:
        params.pop("requestState", None)
    else:
        params["requestState"] = state


def _dump(response: mt.InputResponse) -> dict[str, Any]:
    return response.model_dump(by_alias=True, mode="json", exclude_none=True)


# ── a handshake client: ask live ──────────────────────────────────


def _unsupported(what: str) -> ToolError:
    return ToolError(f"The upstream asked for {what}, which this client doesn't support")


async def ask_live(
    fctx: Context, request: mt.InputRequest, *, elicitation_id: str | None = None
) -> mt.InputResponse:
    """Put one upstream input request to the handshake-era client."""
    session = fctx.session
    caps = session.client_capabilities
    if isinstance(request, mt.ElicitRequest):
        params = request.params
        if isinstance(params, mt.ElicitRequestURLParams):
            if caps is None or caps.elicitation is None or caps.elicitation.url is None:
                raise _unsupported("a URL to be opened (URL-mode elicitation)")
            answer = await session.elicit_url(
                message=params.message,
                url=params.url,
                elicitation_id=elicitation_id or params.elicitation_id or uuid.uuid4().hex,
                related_request_id=fctx.request_id,
            )
        else:
            answer = await session.elicit(
                message=params.message,
                requested_schema=params.requested_schema,
                related_request_id=fctx.request_id,
            )
        return mt.ElicitResult(action=answer.action, content=answer.content)
    if isinstance(request, mt.CreateMessageRequest):
        if caps is None or caps.sampling is None:
            raise _unsupported("sampling")
        p = request.params
        common: dict[str, Any] = {
            "messages": list(p.messages),
            "max_tokens": p.max_tokens,
            "system_prompt": p.system_prompt,
            "include_context": p.include_context,
            "temperature": p.temperature,
            "stop_sequences": p.stop_sequences,
            "metadata": p.metadata,
            "model_preferences": p.model_preferences,
            "related_request_id": fctx.request_id,
        }
        if p.tools is not None:
            common["tools"] = p.tools
            if p.tool_choice is not None:
                common["tool_choice"] = p.tool_choice
        sampled: (
            mt.CreateMessageResult | mt.CreateMessageResultWithTools
        ) = await session.create_message(**common)
        return sampled
    if isinstance(request, mt.ListRootsRequest):
        if caps is None or caps.roots is None:
            raise _unsupported("its roots")
        return await session.list_roots()
    raise _unsupported(type(request).__name__)


def _url_of(request: mt.InputRequest) -> str | None:
    if isinstance(request, mt.ElicitRequest) and isinstance(
        request.params, mt.ElicitRequestURLParams
    ):
        return request.params.url
    return None


async def _note_waiting(fctx: Context, waited: float, url: str) -> None:
    """Progress while a URL step runs (keeps progress-aware clients patient)."""
    try:
        await fctx.report_progress(waited, None, f"Waiting for the browser step at {url}")
    except Exception:  # noqa: BLE001 — progress is best effort
        pass


async def drive_upstream_asks(
    fctx: Context | None,
    result: R,
    rerun: Callable[[], Awaitable[R]],
) -> R:
    """The reverse bridge: a handshake-era client, a 2026-era upstream.

    While *result* asks for input, answer it live and *rerun* the request with
    the answers (riding the wire params, restored afterwards). URL asks the
    user accepted are re-polled with backoff instead of asked again, for up to
    :func:`url_ask_wait`; then ``UrlElicitationRequiredError`` (-32042) goes to
    the client, which calls again later.
    """
    params = wire_params(fctx)
    if fctx is None or params is None:
        return result  # no wire request to carry answers on: the ask stands
    loop = asyncio.get_running_loop()
    rounds = 0
    accepted: dict[str, str] = {}  # url -> the elicitation id the client saw
    messages: dict[str, str] = {}
    deadline: float | None = None
    delay = 0.5
    state_only_delay = 0.05
    try:
        while (ask := input_required_of(result)) is not None:
            requests = ask.input_requests or {}
            urls = [_url_of(r) for r in requests.values()]
            answers: dict[str, Any] | None
            if requests and all(u is not None and u in accepted for u in urls):
                # Every ask is a URL the user already agreed to open: the
                # browser step is still running. Poll, don't ask again.
                now = loop.time()
                if deadline is None:
                    deadline = now + url_ask_wait()
                if now >= deadline:
                    raise UrlElicitationRequiredError(
                        [
                            mt.ElicitRequestURLParams(
                                message=messages[u], url=u, elicitation_id=accepted[u]
                            )
                            for u in dict.fromkeys(u for u in urls if u is not None)
                        ],
                        message="Complete the step in the browser, then call again",
                    )
                await _note_waiting(fctx, url_ask_wait() - (deadline - now), urls[0] or "")
                await asyncio.sleep(min(delay, max(0.0, deadline - now)))
                delay = min(delay * 2, 10.0)
                answers = {k: _dump(mt.ElicitResult(action="accept")) for k in requests}
            elif not requests:
                # State only: the upstream is working; retry with backoff.
                rounds += 1
                if rounds > MAX_INPUT_ROUNDS:
                    raise ToolError(
                        f"The upstream asked for input more than {MAX_INPUT_ROUNDS} times"
                    )
                await asyncio.sleep(state_only_delay)
                state_only_delay = min(state_only_delay * 2, 0.25)
                answers = None
            else:
                rounds += 1
                if rounds > MAX_INPUT_ROUNDS:
                    raise ToolError(
                        f"The upstream asked for input more than {MAX_INPUT_ROUNDS} times"
                    )
                deadline, delay = None, 0.5
                answers = {}
                for key, request in requests.items():
                    url = _url_of(request)
                    eid = accepted.get(url) if url else None
                    if url and eid is None:
                        eid = uuid.uuid4().hex
                    response = await ask_live(fctx, request, elicitation_id=eid)
                    if url and eid and isinstance(response, mt.ElicitResult):
                        if response.action == "accept":
                            accepted[url] = eid
                            assert isinstance(request, mt.ElicitRequest)
                            messages[url] = request.params.message
                    answers[key] = _dump(response)
            set_continuation(params, answers, ask.request_state)
            result = await rerun()
    finally:
        set_continuation(params, None, None)
    return result
