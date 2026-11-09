"""Per-chat hidden tools: glob patterns over the server-level catalog, on
top of the per-chat server filter (design decision 01M70BRB5N). Hidden for
one chat means not listed to it and not callable by it; other chats are
unaffected."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import ProcFactory, http_mock_entry
from fakes import content_text
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from mcp_combiner.runtime import CLIENT_CONNECT_MODE, tool_hidden

pytestmark = pytest.mark.e2e

CHAT_A = "a11ce000-1111-2222-3333-444444444444"
CHAT_B = "b0b00000-1111-2222-3333-444444444444"


def test_patterns_match_namespaced_names() -> None:
    assert tool_hidden("svg-mcp_add_rect", {"svg-mcp_add_*"})
    assert tool_hidden("github_search_code", {"github_search_code"})
    assert not tool_hidden("svg-mcp_render", {"svg-mcp_add_*"})
    assert not tool_hidden("anything", set())


def _chat(combiner: Any, token: str) -> Client[Any]:
    return Client(
        StreamableHttpTransport(combiner.mcp_url, headers={"X-MCP-Combiner-Session": token}),
        mode=CLIENT_CONNECT_MODE,
    )


async def _names(client: Client[Any]) -> set[str]:
    return {t.name for t in await client.list_tools() if t.name.startswith("mockup_")}


async def test_a_chat_hides_and_shows_tools(procs: ProcFactory, tmp_path: Path) -> None:
    mock = await procs.start_http_mock("mockup")
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"servers": {"mockup": http_mock_entry(mock.port)}}))
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)

    async with _chat(combiner, CHAT_A) as a, _chat(combiner, CHAT_B) as b:
        everything = await _names(a)
        assert "mockup_echo" in everything

        reply = await a.call_tool("combiner__session_hide_tools", {"patterns": ["mockup_ec*"]})
        assert json.loads(content_text(reply))["hidden_tools"] == ["mockup_ec*"]
        assert "mockup_echo" not in await _names(a)
        assert await _names(b) == everything  # only chat A
        with pytest.raises((ToolError, MCPError), match="hidden for this chat"):
            await a.call_tool("mockup_echo", {"message": "hi"})
        assert (await b.call_tool("mockup_echo", {"message": "hi"})).content

        status = await a.call_tool("combiner__session_status", {})
        assert json.loads(content_text(status))["hidden_tools"] == ["mockup_ec*"]

        await a.call_tool("combiner__session_show_tools", {})
        assert await _names(a) == everything


async def test_the_token_route_sets_hidden_tools(procs: ProcFactory, tmp_path: Path) -> None:
    mock = await procs.start_http_mock("mockup")
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"servers": {"mockup": http_mock_entry(mock.port)}}))
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)
    route = f"{combiner.base_url}/sessions/token/{CHAT_A}/filter"

    async with httpx2.AsyncClient(timeout=10.0) as http:
        posted = (await http.post(route, json={"hide": "mockup_echo"})).json()
        assert posted["hidden_tools"] == ["mockup_echo"]
        assert posted["disabled_servers"] == []  # the server filter is untouched
        async with _chat(combiner, CHAT_A) as a:
            assert "mockup_echo" not in await _names(a)
        assert (await http.get(route)).json()["hidden_tools"] == ["mockup_echo"]
        await http.delete(route)
        assert (await http.get(route)).json()["hidden_tools"] == []
    async with _chat(combiner, CHAT_A) as a:
        assert "mockup_echo" in await _names(a)
