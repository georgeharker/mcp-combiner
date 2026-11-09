"""The startup prime of an isolated (per-chat) HTTP upstream.

The prime lists a server's tools outside any chat request. An isolated HTTP
upstream's client could only be made inside one; out of context it raised,
fastmcp's server layer turned the error into an empty list, and every combiner
start logged the server as "answered only empty" — its tools appeared only
once some chat listed them. (Isolated stdio upstreams already had a throwaway
probe client for exactly this.)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    FAST_TIMING_ENV,
    ProcFactory,
    http_mock_entry,
    poll_until,
)
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from mcp_combiner.runtime import CLIENT_CONNECT_MODE

pytestmark = pytest.mark.e2e


async def test_the_startup_prime_confirms_an_isolated_http_upstream(
    procs: ProcFactory, tmp_path: Path
) -> None:
    mock = await procs.start_http_mock("mockup")
    config = tmp_path / "servers.json"
    entry = http_mock_entry(mock.port) | {"isolate": True}
    config.write_text(json.dumps({"servers": {"mockup": entry}}))
    log = tmp_path / "combiner.log"
    combiner = await procs.start_combiner(config, extra_args=["--log-file", str(log)])
    await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)

    async def _primed() -> str | None:
        for line in log.read_text().splitlines():
            if "prime: 'mockup'" in line:
                return line
        return None

    line = await poll_until(_primed, timeout=45.0, desc="the startup prime finishes")
    assert "listed" in line, line


_CHAT = "cafe0000-1111-2222-3333-444444444444"


async def test_a_chat_with_a_dead_session_still_sees_the_catalog(
    procs: ProcFactory, tmp_path: Path
) -> None:
    """The catalog comes from the server's shared connection, not the chat's
    own session: a chat whose upstream session died (the server restarted)
    lists the server's tools from the live server, not through its dead
    session. (Listing through it 404s; the combiner then falls back to its
    last stored list, logged as the server being "absent" — which hid the
    flaw whenever a stored list existed, and lost the tools when none did.)"""
    mock = await procs.start_http_mock("mockup")
    config = tmp_path / "servers.json"
    entry = http_mock_entry(mock.port) | {"isolate": True}
    config.write_text(json.dumps({"servers": {"mockup": entry}}))
    log = tmp_path / "combiner.log"
    combiner = await procs.start_combiner(
        config, env=FAST_TIMING_ENV, extra_args=["--log-file", str(log)]
    )
    await combiner.wait_server_state("mockup", ("ready",), timeout=60.0)

    def primed_counts() -> list[int]:
        return [int(n) for n in re.findall(r"prime: 'mockup' listed (\d+)", log.read_text())]

    def chat() -> Client[Any]:
        return Client(
            StreamableHttpTransport(combiner.mcp_url, headers={"X-MCP-Combiner-Session": _CHAT}),
            mode=CLIENT_CONNECT_MODE,
        )

    # The chat stays connected throughout, as a live chat does: its upstream
    # session stays registered to it rather than being parked.
    async with chat() as client:
        await client.call_tool("mockup_echo", {"message": "hi"})  # the chat's own session

        # The server restarts on the same port: the chat's upstream session is gone.
        mock.proc.kill()
        mock.proc.wait()
        restarted = await procs.start_http_mock("mockup", port=mock.port)

        # A tool appears on the restarted server, so the combiner must list it
        # afresh (not serve its cached list): once the shared connection is
        # back, its list_changed refetches, and the chat sees old and new tools.
        async def _added() -> bool | None:
            try:
                await restarted.control("POST", "/control/tools", {"name": "late_tool"})
            except Exception:
                return None
            return True

        await poll_until(_added, timeout=30.0, desc="add a tool on the restarted server")

        # Wait for the shared connection to be back with the new tool listed.
        before = primed_counts()[0]

        async def _shared_back() -> bool | None:
            return True if any(n > before for n in primed_counts()) else None

        await poll_until(
            _shared_back, timeout=60.0, desc="the shared connection lists the new tool"
        )
        mark = len(log.read_text())

        async def _listed() -> list[str] | None:
            names = [t.name for t in await client.list_tools() if t.name.startswith("mockup_")]
            return names if "mockup_late_tool" in names else None

        names = await poll_until(_listed, timeout=60.0, desc="the chat lists the new tool")
        assert "mockup_echo" in names
        assert "server 'mockup' absent" not in log.read_text()[mark:]
