"""Asking the user whether to restart a server that's running but not
answering (restartprompt; design decision 01M70HS1Q6)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from mcp_types import ElicitResult, InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from mcp_combiner import restartprompt
from mcp_combiner.runtime import RUNTIME


@dataclass
class _RequestContext:
    protocol_version: str


@dataclass
class _Ctx:
    """A request context with a scripted ``elicit`` (as in test_permissions)."""

    result: Any = None
    exc: Exception | None = None
    era_2026: bool = False
    request_state: str | None = None
    input_responses: dict[str, Any] | None = None
    asked: list[str] = field(default_factory=list)

    @property
    def request_context(self) -> _RequestContext:
        version = sorted(MODERN_PROTOCOL_VERSIONS)[-1] if self.era_2026 else "2025-11-25"
        return _RequestContext(protocol_version=version)

    async def elicit(self, message: str, response_type: Any = None) -> Any:
        self.asked.append(message)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    restartprompt.reset()
    restarted: list[str] = []

    async def routine(name: str) -> str:
        restarted.append(name)
        return f"Server '{name}' restarted."

    monkeypatch.setattr(RUNTIME, "restart_server", routine)
    yield restarted
    restartprompt.reset()


async def test_restart_now_runs_the_routine_and_asks_for_a_retry(_clean: list[str]) -> None:
    ctx = _Ctx(result=AcceptedElicitation(data=restartprompt.RESTART))
    with pytest.raises(ToolError, match="Restarted 'crib' at your request.*Retry the call"):
        await restartprompt.gate(ctx, "crib")
    assert _clean == ["crib"]
    assert "stopped responding" in ctx.asked[0]


async def test_not_now_fails_the_call_and_stops_asking_for_a_while(_clean: list[str]) -> None:
    ctx = _Ctx(result=AcceptedElicitation(data=restartprompt.NOT_NOW))
    with pytest.raises(ToolError, match="wasn't restarted"):
        await restartprompt.gate(ctx, "crib")
    again = _Ctx(result=AcceptedElicitation(data=restartprompt.RESTART))
    with pytest.raises(ToolError, match="declined a few minutes ago"):
        await restartprompt.gate(again, "crib")
    assert again.asked == [] and _clean == []
    # Another server is asked independently.
    with pytest.raises(ToolError, match="Restarted 'svg'"):
        await restartprompt.gate(again, "svg")
    # Recovery clears the silence.
    restartprompt.recovered("crib")
    with pytest.raises(ToolError, match="Restarted 'crib'"):
        await restartprompt.gate(_Ctx(result=AcceptedElicitation(data="Restart now")), "crib")


async def test_dismissing_the_question_counts_as_not_now(_clean: list[str]) -> None:
    with pytest.raises(ToolError, match="wasn't restarted"):
        await restartprompt.gate(_Ctx(result=DeclinedElicitation()), "crib")
    assert _clean == []


async def test_a_client_that_cannot_elicit_is_pointed_at_restart_server(
    _clean: list[str],
) -> None:
    ctx = _Ctx(exc=RuntimeError("no elicitation capability"))
    with pytest.raises(ToolError, match=r"combiner__restart_server\('crib'\)"):
        await restartprompt.gate(ctx, "crib")
    # Not a decline: the next capable chat is asked.
    with pytest.raises(ToolError, match="Restarted"):
        await restartprompt.gate(_Ctx(result=AcceptedElicitation(data="Restart now")), "crib")


async def test_one_question_per_server_at_a_time(_clean: list[str]) -> None:
    restartprompt._asking.add("crib")
    with pytest.raises(ToolError, match="being asked in another chat"):
        await restartprompt.gate(_Ctx(result=AcceptedElicitation(data="Restart now")), "crib")
    assert _clean == []


async def test_2026_clients_are_asked_with_input_required(_clean: list[str]) -> None:
    question = await restartprompt.gate(_Ctx(era_2026=True), "crib")
    assert isinstance(question, InputRequiredResult)
    assert question.request_state == "restart:crib"
    assert _clean == []
    answer = ElicitResult(action="accept", content={"value": restartprompt.RESTART})
    retry = _Ctx(era_2026=True, request_state="restart:crib", input_responses={"restart": answer})
    with pytest.raises(ToolError, match="Restarted 'crib'"):
        await restartprompt.gate(retry, "crib")
    assert _clean == ["crib"]


def test_only_a_transport_level_unknown_session_counts_as_gone() -> None:
    """A tool's own error that happens to mention a session doesn't make the
    combiner drop the chat's session; only the upstream rejecting it does."""
    from fastmcp.exceptions import ToolError as _ToolError
    from mcp.shared.exceptions import MCPError

    from mcp_combiner.middleware import _session_gone

    rejected = MCPError(-32600, "Session not found")
    assert _session_gone(rejected)
    try:
        try:
            raise rejected
        except MCPError as inner:
            raise _ToolError("Error calling tool 'echo': Session not found") from inner
    except _ToolError as wrapped:
        assert _session_gone(wrapped)
    assert not _session_gone(_ToolError("Session not found"))  # the tool's own words
    assert not _session_gone(MCPError(-32600, "Invalid request"))
