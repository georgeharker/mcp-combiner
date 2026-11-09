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
from pathlib import Path

import pytest
from conftest import ProcFactory, http_mock_entry, poll_until

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
