"""Resource-change notifications reach clients (toucan's host refreshes its
resource section on them): lifecycle changes publish resources/list_changed,
and an upstream's resources/updated is forwarded."""

from __future__ import annotations

from pathlib import Path

import mcp.types as mt
import pytest
from conftest import ProcFactory, http_mock_entry, poll_until, write_servers_config
from fastmcp import Client
from fastmcp.client.messages import MessageHandler

from mcp_combiner.runtime import CLIENT_CONNECT_MODE

pytestmark = pytest.mark.e2e

WIDGET = "ui://mock/widget"
WIDGET_NAMESPACED = "ui://mockup/mock/widget"


class _Heard(MessageHandler):
    def __init__(self) -> None:
        super().__init__()
        self.list_changed = 0
        self.updated: list[str] = []

    async def on_resource_list_changed(self, message: mt.ResourceListChangedNotification) -> None:
        self.list_changed += 1

    async def on_resource_updated(self, message: mt.ResourceUpdatedNotification) -> None:
        self.updated.append(str(message.params.uri))


async def test_lifecycle_changes_publish_resources_list_changed(
    procs: ProcFactory, tmp_path: Path
) -> None:
    mock = await procs.start_http_mock("mockup")
    config = write_servers_config(tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)})
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("mockup", ("ready",))
    heard = _Heard()
    url = f"{combiner.mcp_url}/res-life-0001"
    async with Client(url, message_handler=heard, mode=CLIENT_CONNECT_MODE) as client:
        assert WIDGET_NAMESPACED in {str(r.uri) for r in await client.list_resources()}

        before = heard.list_changed
        await client.call_tool("combiner__disable_server", {"server_name": "mockup"})

        async def _told_gone() -> bool | None:
            return True if heard.list_changed > before else None

        await poll_until(_told_gone, timeout=15.0, desc="resources/list_changed on disable")
        assert WIDGET_NAMESPACED not in {str(r.uri) for r in await client.list_resources()}

        before = heard.list_changed
        await client.call_tool("combiner__enable_server", {"server_name": "mockup"})

        async def _told_back() -> bool | None:
            return True if heard.list_changed > before else None

        await poll_until(_told_back, timeout=30.0, desc="resources/list_changed on enable")
        assert WIDGET_NAMESPACED in {str(r.uri) for r in await client.list_resources()}


async def test_an_upstream_resource_update_is_forwarded(procs: ProcFactory, tmp_path: Path) -> None:
    mock = await procs.start_http_mock("mockup")
    config = write_servers_config(tmp_path / "servers.json", {"mockup": http_mock_entry(mock.port)})
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("mockup", ("ready",))
    heard = _Heard()
    url = f"{combiner.mcp_url}/res-upd-0001"
    async with Client(url, message_handler=heard, mode=CLIENT_CONNECT_MODE) as client:
        await client.session.subscribe_resource(WIDGET_NAMESPACED)
        await mock.control("POST", "/control/resource-updated", {"uri": WIDGET})

        async def _updated() -> bool | None:
            return True if WIDGET_NAMESPACED in heard.updated else None

        await poll_until(_updated, timeout=15.0, desc="resources/updated forwarded")
