"""combiner__server_instructions (design 01M8FG9JMW): an upstream's own
instructions, captured as its session initializes and served on demand —
never concatenated into the combiner's own."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import ProcFactory, stdio_mock_entry
from fakes import content_text
from fastmcp import Client

from mcp_combiner import instructions
from mcp_combiner.runtime import CLIENT_CONNECT_MODE

GUIDE = "Call create_session first, then pass session_id on every call."


def test_record_and_get() -> None:
    instructions.reset()
    assert instructions.get("svg") is None  # never connected
    instructions.record("svg", f"  {GUIDE}\n")
    assert instructions.get("svg") == GUIDE
    instructions.record("plain", None)
    assert instructions.get("plain") == ""  # connected, sent none
    instructions.reset()


@pytest.mark.e2e
async def test_instructions_on_demand(procs: ProcFactory, tmp_path: Path) -> None:
    guided = stdio_mock_entry("guided")
    guided["env"] = {"MOCK_SERVER_INSTRUCTIONS": GUIDE}
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps(
            {
                "collections": {"extra": {"enabled": False}},
                "mcpServers": {
                    "guided": guided,
                    "plain": stdio_mock_entry("plain"),
                    "hidden": stdio_mock_entry("hidden") | {"collections": ["extra"]},
                    "off": stdio_mock_entry("off") | {"disabled": True},
                },
            }
        )
    )
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("guided", ("ready",), timeout=60.0)
    await combiner.wait_server_state("plain", ("ready",), timeout=60.0)

    async def ask(client: Client[Any], server: str) -> str:
        result = await client.call_tool("combiner__server_instructions", {"server_name": server})
        return content_text(result)

    async with Client(f"{combiner.mcp_url}/chat-instr-01", mode=CLIENT_CONNECT_MODE) as c:
        assert c.initialize_result is not None
        own = c.initialize_result.instructions or ""
        assert "combiner__server_instructions" in own
        assert GUIDE not in own  # not concatenated
        assert await ask(c, "guided") == f"Instructions from 'guided':\n\n{GUIDE}"
        assert "sends no instructions" in await ask(c, "plain")
        assert "off for this chat" in await ask(c, "hidden")  # its collection is off
        assert "is disabled" in await ask(c, "off")
        assert "isn't configured" in await ask(c, "nope")
        await c.call_tool("combiner__session_disable_server", {"server_name": "guided"})
        assert "off for this chat" in await ask(c, "guided")


@pytest.mark.e2e
@pytest.mark.parametrize("offer_2026", [False, True], ids=["handshake", "2026"])
async def test_http_upstreams_report_instructions(
    procs: ProcFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offer_2026: bool
) -> None:
    """HTTP upstreams (isolated, so per-chat sessions exist too) on either era:
    the instructions come from initialize or server/discover."""
    from conftest import http_mock_entry

    monkeypatch.setenv("MOCK_SERVER_INSTRUCTIONS", GUIDE)
    mock = await procs.start_http_mock("web", offer_2026=offer_2026)
    monkeypatch.delenv("MOCK_SERVER_INSTRUCTIONS")
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps({"mcpServers": {"web": http_mock_entry(mock.port) | {"isolate": True}}})
    )
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("web", ("ready",), timeout=60.0)
    async with Client(f"{combiner.mcp_url}/chat-instr-02", mode=CLIENT_CONNECT_MODE) as c:
        result = await c.call_tool("combiner__server_instructions", {"server_name": "web"})
    assert content_text(result) == f"Instructions from 'web':\n\n{GUIDE}"
