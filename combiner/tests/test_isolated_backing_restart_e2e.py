"""An isolated, sharedserver-backed upstream whose process wedges and is
brought back by the health monitor (not by combiner__restart_server).

Seen live with cribsheet: a wedged daemon, the monitor's forced restart, and
afterwards a server reading "ready" while chats had none of its tools. This
pins the recovery: the monitor's restart resets the chats' per-chat sessions
(as combiner__restart_server does) and both an existing and a fresh chat get
the tools back. The other half of that incident, a prime answered empty after
the connection had listed tools, is covered by the re-probe unit tests in
test_connections and test_session_filter.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import httpx2
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


@requires_sharedserver
async def test_monitor_restart_of_an_isolated_backing_restores_its_tools(
    procs: ProcFactory, tmp_path: Path
) -> None:
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

    # A chat restarts the server: the connection (and its monitor) is
    # re-opened inside that tool call.
    async with _chat(combiner.mcp_url, CHAT_A) as client:
        await client.call_tool("combiner__restart_server", {"server_name": "mockup"})
    await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)
    assert await poll_until(lambda: _greets(combiner.mcp_url, CHAT_A), timeout=30.0)

    async with httpx2.AsyncClient() as http:
        first = (await http.get(f"http://127.0.0.1:{port}/stats", timeout=2.0)).json()

    # The backing process wedges (alive, answering nothing), as a daemon stuck
    # on a slow call does; chats' requests hang. Only the monitor's escalation
    # (a forced stop and restart) brings it back.
    os.kill(int(first["pid"]), signal.SIGSTOP)
    stuck = asyncio.create_task(_greets(combiner.mcp_url, CHAT_A))

    async def _new_process() -> int | None:
        try:
            async with httpx2.AsyncClient() as http:
                stats = (await http.get(f"http://127.0.0.1:{port}/stats", timeout=2.0)).json()
        except Exception:
            return None
        pid = int(stats["pid"])
        return pid if pid != int(first["pid"]) else None

    await poll_until(_new_process, timeout=90.0, desc="monitor restarts the backing process")

    # Both the chat that ran restart_server and a fresh chat get the tools back.
    assert (
        await poll_until(
            lambda: _greets(combiner.mcp_url, CHAT_B), timeout=45.0, desc="fresh chat sees tools"
        )
        == "Hello, b0b0!"
    )
    assert (
        await poll_until(
            lambda: _greets(combiner.mcp_url, CHAT_A), timeout=45.0, desc="old chat sees tools"
        )
        == "Hello, a11c!"
    )
    stuck.cancel()
    # The monitor's restart reset the chats' sessions, as restart_server does.
    log = (tmp_path / "combiner.log").read_text()
    after_escalation = log.split("failed 3 consecutive reconnects", 1)[1]
    assert "isolated per-chat session(s) for 'mockup'" in after_escalation
    # …because it ran the same routine as combiner__restart_server.
    assert "Automatic restart of 'mockup': Server 'mockup' restarted" in after_escalation
