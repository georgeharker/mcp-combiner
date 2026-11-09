"""An isolated, sharedserver-backed upstream that dies or stops answering
(design decision 01M70HS1Q6).

- GONE (its process killed, the port refuses): the health monitor restarts it
  through the combiner__restart_server routine, which resets the chats'
  per-chat sessions; existing and fresh chats get the tools back.
- STILL RUNNING but not answering (frozen, as cribsheet was when blocked on an
  LLM call): never restarted automatically. The next call routed to it asks
  the user; "Restart now" restarts it and the retried call works.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import httpx2
import mcp.types as mt
import pytest
from conftest import (
    FAST_TIMING_ENV,
    ProcFactory,
    free_port,
    poll_until,
    requires_sharedserver,
    sharedserver_def,
    sharedserver_mock_entry,
    unique_shared_name,
    write_servers_config,
    write_tools_spec,
)
from fakes import content_text
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from mcp_combiner.runtime import CLIENT_CONNECT_MODE

pytestmark = pytest.mark.e2e

_SPEC = [
    {"name": "greet", "params": {"who": "string"}, "response_template": "Hello, {who}!"},
]
CHAT_A = "a11ce000-1111-2222-3333-444444444444"
CHAT_B = "b0b00000-1111-2222-3333-444444444444"


def _chat(mcp_url: str, token: str) -> Client[Any]:
    return Client(
        StreamableHttpTransport(mcp_url, headers={"X-MCP-Combiner-Session": token}),
        mode=CLIENT_CONNECT_MODE,
    )


async def _greets(mcp_url: str, token: str) -> str | None:
    """The chat's greet answer, or None while its tools are missing."""
    try:
        async with _chat(mcp_url, token) as client:
            names = {t.name for t in await client.list_tools()}
            if "mockup_greet" not in names:
                return None
            return content_text(await client.call_tool("mockup_greet", {"who": token[:4]}))
    except Exception:
        return None


async def _start(procs: ProcFactory, tmp_path: Path) -> tuple[Any, int]:
    tools_path = write_tools_spec(tmp_path / "tools.json", _SPEC)
    port = free_port()
    shared = procs.track_shared(unique_shared_name())
    entry = sharedserver_mock_entry("mockup", shared, port) | {"isolate": True}
    cfg = write_servers_config(
        tmp_path / "servers.json",
        {"mockup": entry},
        shared_servers={
            shared: sharedserver_def(shared, port, tools_path=tools_path, state_dir=tmp_path)
        },
    )
    env = FAST_TIMING_ENV | {
        "MCP_COMBINER_HEALTH_PING_TIMEOUT": "1",
        "MCP_COMBINER_CONNECT_TIMEOUT": "3",
    }
    combiner = await procs.start_combiner(
        cfg, env=env, extra_args=["--log-file", str(tmp_path / "combiner.log")]
    )
    await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)
    assert await poll_until(lambda: _greets(combiner.mcp_url, CHAT_A), timeout=30.0)
    return combiner, port


async def _pid(port: int) -> int | None:
    try:
        async with httpx2.AsyncClient() as http:
            stats = (await http.get(f"http://127.0.0.1:{port}/stats", timeout=2.0)).json()
    except Exception:
        return None
    return int(stats["pid"])


@requires_sharedserver
async def test_a_dead_backing_process_is_restarted_automatically(
    procs: ProcFactory, tmp_path: Path
) -> None:
    combiner, port = await _start(procs, tmp_path)
    first = await _pid(port)
    assert first is not None
    os.kill(first, signal.SIGKILL)  # gone: nothing listens on its port

    async def _new_process() -> int | None:
        pid = await _pid(port)
        return pid if pid is not None and pid != first else None

    await poll_until(_new_process, timeout=90.0, desc="the monitor restarts the dead process")
    for chat, expected in ((CHAT_B, "Hello, b0b0!"), (CHAT_A, "Hello, a11c!")):

        async def _greeted(token: str = chat) -> str | None:
            return await _greets(combiner.mcp_url, token)

        assert await poll_until(_greeted, timeout=45.0, desc="chat sees tools") == expected
    log = (tmp_path / "combiner.log").read_text()
    after = log.split("failed 3 consecutive reconnects", 1)[1]
    assert "isolated per-chat session(s) for 'mockup'" in after  # sessions reset
    assert "Automatic restart of 'mockup': Server 'mockup' restarted" in after  # the routine


@requires_sharedserver
async def test_a_frozen_backing_process_is_not_killed_the_user_is_asked(
    procs: ProcFactory, tmp_path: Path
) -> None:
    combiner, port = await _start(procs, tmp_path)
    first = await _pid(port)
    assert first is not None
    os.kill(first, signal.SIGSTOP)  # alive (its port accepts) but answering nothing

    async def _unresponsive() -> bool | None:
        async with httpx2.AsyncClient() as http:
            health = (await http.get(f"{combiner.base_url}/health", timeout=5.0)).json()
        return True if health["servers"]["mockup"]["state"] == "unresponsive" else None

    await poll_until(_unresponsive, timeout=90.0, desc="the server reads unresponsive")
    assert await _pid(port) is None  # still the frozen process: nobody restarted it
    assert (
        "restarting it (the combiner__restart_server routine)"
        not in (tmp_path / "combiner.log").read_text()
    )

    asked: list[str] = []

    async def answer(
        message: str, response_type: Any, params: mt.ElicitRequestParams, context: object
    ) -> dict[str, Any]:
        asked.append(message)
        return {"value": "Restart now"}

    async with Client(
        StreamableHttpTransport(combiner.mcp_url, headers={"X-MCP-Combiner-Session": CHAT_B}),
        mode=CLIENT_CONNECT_MODE,
        elicitation_handler=answer,
    ) as client:
        with pytest.raises(ToolError, match="Restarted 'mockup' at your request"):
            await client.call_tool("mockup_greet", {"who": "b0b0"})
    assert asked and "stopped responding" in asked[0]

    greeted = await poll_until(
        lambda: _greets(combiner.mcp_url, CHAT_B), timeout=45.0, desc="the retried call works"
    )
    assert greeted == "Hello, b0b0!"
    assert await _pid(port) not in (None, first)
