"""Unit tests for mcp_combiner.asks: continuation state for 2026 clients.

The combiner seals ``requestState`` on the wire, so the wrapping is invisible
end to end; these pin its shape and the routing decisions made from it.
"""

from __future__ import annotations

import mcp.types as mt
from fastmcp.prompts.base import InputRequiredPromptResult
from fastmcp.resources.base import InputRequiredResourceResult
from fastmcp.tools.base import InputRequiredToolResult
from mcp.shared.exceptions import UrlElicitationRequiredError

from mcp_combiner import asks

ROOTS_ASK: mt.InputRequests = {"roots": mt.ListRootsRequest()}


def test_wrap_and_classify_round_trip() -> None:
    state = asks.wrap_state("svg-mcp", "doc:1")
    assert state == "up:svg-mcp:doc:1"
    cont = asks.classify(state)
    assert cont.kind == "up"
    assert cont.owned_by("svg-mcp")
    assert cont.inner("svg-mcp") == "doc:1"  # the upstream's own colons survive


def test_an_upstream_ask_without_state_still_continues() -> None:
    cont = asks.classify(asks.wrap_state("crib", None))
    assert cont.kind == "up"
    assert cont.owned_by("crib")
    assert cont.inner("crib") is None
    assert cont.skips_gates


def test_ownership_is_exact_not_a_prefix_match() -> None:
    cont = asks.classify(asks.wrap_state("crib", "x"))
    assert not cont.owned_by("cri")
    assert not cont.owned_by("cribsheet")
    assert not cont.owned_by(None)


def test_upstream_states_that_look_like_ours_are_kept_apart() -> None:
    # An upstream that itself uses "held:" must not resume a held call.
    cont = asks.classify(asks.wrap_state("mock", "held:abc"))
    assert cont.kind == "up"
    assert cont.inner("mock") == "held:abc"


def test_the_combiners_own_continuations() -> None:
    assert asks.classify(None).kind == "first"
    assert asks.classify("held:1234").kind == "held"
    assert asks.classify("redo:mock").kind == "redo"
    assert asks.classify("consent:mock/greet").kind == "own"
    assert asks.classify("restart:mock").kind == "own"
    assert not asks.classify("consent:mock/greet").skips_gates


def test_wrap_upstream_keeps_the_result_type() -> None:
    ask = mt.InputRequiredResult(input_requests=ROOTS_ASK, request_state="s")
    for cls in (InputRequiredToolResult, InputRequiredResourceResult, InputRequiredPromptResult):
        wrapped = asks.wrap_upstream(cls(ask), "mock")
        assert type(wrapped) is cls
        assert wrapped.input_required.request_state == "up:mock:s"
        assert wrapped.input_required.input_requests == ROOTS_ASK


def test_wrap_upstream_leaves_real_results_alone() -> None:
    result = object()
    assert asks.wrap_upstream(result, "mock") is result


def test_url_asks_from_a_32042() -> None:
    error = UrlElicitationRequiredError(
        [mt.ElicitRequestURLParams(message="Authorize", url="https://x/y", elicitation_id="e1")]
    )
    ask = asks.url_asks(error, "mock")
    assert ask.request_state == "redo:mock"
    (request,) = (ask.input_requests or {}).values()
    assert isinstance(request, mt.ElicitRequest)
    assert isinstance(request.params, mt.ElicitRequestURLParams)
    assert request.params.url == "https://x/y"
    assert request.params.elicitation_id is None  # 2026 URL asks carry none


def test_set_continuation() -> None:
    params: dict[str, object] = {"name": "t", "requestState": "old", "inputResponses": {}}
    asks.set_continuation(params, {"k": {"action": "accept"}}, "new")
    assert params["requestState"] == "new"
    assert params["inputResponses"] == {"k": {"action": "accept"}}
    asks.set_continuation(params, None, None)
    assert params == {"name": "t"}
