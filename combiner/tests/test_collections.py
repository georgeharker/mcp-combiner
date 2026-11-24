"""Server collections (design decision 01M7E7BH8D): named groups of servers that
load by condition (platform, host, environment) and are switched on or off as
a unit; a server runs if ANY of its loaded collections is on."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import ProcFactory, http_mock_entry
from fakes import content_text
from fastmcp import Client

from mcp_combiner.config import CombinerConfig
from mcp_combiner.runtime import CLIENT_CONNECT_MODE

OTHER_PLATFORM = "win32" if sys.platform != "win32" else "linux"
HOST = socket.gethostname().split(".", 1)[0]


def _load(tmp_path: Path, doc: dict[str, Any]) -> CombinerConfig:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps(doc))
    return CombinerConfig.load(str(path))


def _srv(**extra: Any) -> dict[str, Any]:
    return {"command": "true", **extra}


class TestLoading:
    def test_a_collection_that_doesnt_apply_drops_its_servers(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path,
            {
                "collections": {
                    "here": {"when": {"platforms": [sys.platform]}},
                    "elsewhere": {"when": {"platforms": [OTHER_PLATFORM]}},
                },
                "mcpServers": {
                    "plain": _srv(),
                    "local": _srv(collections=["here"]),
                    "foreign": _srv(collections=["elsewhere"]),
                    "both": _srv(collections=["here", "elsewhere"]),
                },
            },
        )
        assert set(cfg.servers) == {"plain", "local", "both"}
        assert set(cfg.collections) == {"here"}
        assert not any(s.disabled for s in cfg.servers.values())

    def test_hosts_and_env_conditions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COLLECTION_TEST_SITE", "work")
        monkeypatch.delenv("COLLECTION_TEST_UNSET", raising=False)
        cfg = _load(
            tmp_path,
            {
                "collections": {
                    "this-host": {"when": {"hosts": [HOST.upper()]}},
                    "other-host": {"when": {"hosts": ["no-such-host-x"]}},
                    "work": {"when": {"env": {"COLLECTION_TEST_SITE": "work"}}},
                    "home": {"when": {"env": {"COLLECTION_TEST_SITE": "home"}}},
                    "needs-var": {"when": {"env": ["COLLECTION_TEST_UNSET"]}},
                },
                "mcpServers": {},
            },
        )
        assert set(cfg.collections) == {"this-host", "work"}

    def test_on_wins(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path,
            {
                "collections": {"on": {}, "off": {"enabled": False}},
                "mcpServers": {
                    "only-off": _srv(collections=["off"]),
                    "on-and-off": _srv(collections=["on", "off"]),
                    "self-disabled": _srv(collections=["on"], disabled=True),
                },
            },
        )
        assert cfg.servers["only-off"].disabled
        assert not cfg.servers["on-and-off"].disabled
        assert cfg.servers["self-disabled"].disabled  # its own flag still wins

    def test_the_environment_overrides_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_COMBINER_COLLECTIONS", "+music, -google")
        cfg = _load(
            tmp_path,
            {
                "collections": {"music": {"enabled": False}, "google": {}},
                "mcpServers": {
                    "ableton": _srv(collections=["music"]),
                    "gws": _srv(collections=["google"]),
                },
            },
        )
        assert not cfg.servers["ableton"].disabled
        assert cfg.servers["gws"].disabled

    def test_the_old_profiles_key_names_its_replacement(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match='"profiles" was renamed to "collections"'):
            _load(tmp_path, {"profiles": {"mac": {}}, "mcpServers": {}})
        with pytest.raises(ValueError, match='"profiles" was renamed to "collections"'):
            _load(tmp_path, {"mcpServers": {"a": _srv(profiles=["mac"])}})

    @pytest.mark.parametrize(
        ("doc", "message"),
        [
            ({"mcpServers": {"a": _srv(collections=["nope"])}}, "unknown collection"),
            ({"collections": {"p": {"when": {"platforms": ["macos"]}}}}, "unknown platform"),
            ({"collections": {"p": {"enabled": "no"}}}, "true or false"),
            ({"collections": {"p": {"enbled": False}}}, "unknown key"),
            ({"collections": {"p": {"when": {"os": ["darwin"]}}}}, "unknown when key"),
            ({"collections": ["mac"]}, "must be an object"),
            ({"mcpServers": {"a": _srv(collections="mac")}}, "list of collection names"),
        ],
    )
    def test_bad_collections_fail_the_load(
        self, tmp_path: Path, doc: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            _load(tmp_path, {"mcpServers": {}, **doc})


def test_a_failed_enable_or_disable_is_not_counted_as_done() -> None:
    """The collection toggle reports failures from enable_server / disable_server
    by their messages; "enabled but failed to mount" is a failure."""
    from mcp_combiner.meta_tools import went_wrong

    assert not went_wrong("Server 'a' enabled and mounted")
    assert not went_wrong("Server 'a' disabled and unmounted (1 provider(s) removed)")
    assert went_wrong("Server 'a' enabled but failed to mount: boom")
    assert went_wrong("Server 'a' disabled but failed to unmount: boom")
    assert went_wrong("Error: Server 'a' not found")


@pytest.mark.e2e
async def test_turning_a_collection_on_and_off(procs: ProcFactory, tmp_path: Path) -> None:
    one = await procs.start_http_mock("one")
    two = await procs.start_http_mock("two")
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps(
            {
                "collections": {"extra": {"enabled": False}, "core": {}},
                "mcpServers": {
                    "one": http_mock_entry(one.port) | {"collections": ["extra"]},
                    "two": http_mock_entry(two.port) | {"collections": ["extra", "core"]},
                },
            }
        )
    )
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("two", ("ready",), timeout=60.0)

    async with Client(combiner.mcp_url, mode=CLIENT_CONNECT_MODE) as client:

        async def collections() -> dict[str, Any]:
            result: dict[str, Any] = json.loads(
                content_text(await client.call_tool("combiner__collections", {}))
            )
            return result

        assert await collections() == {
            "core": {
                "enabled": True,
                "this_chat": True,
                "this_chat_set": False,
                "servers": {"two": {"running": True, "this_chat": True}},
            },
            "extra": {
                "enabled": False,
                "this_chat": False,
                "this_chat_set": False,
                "servers": {
                    "one": {"running": False, "this_chat": False},
                    "two": {"running": True, "this_chat": True},
                },
            },
        }

        said = content_text(
            await client.call_tool("combiner__enable_collection", {"collection": "extra"})
        )
        assert "started one" in said
        await combiner.wait_server_state("one", ("ready",), timeout=60.0)

        # Off again: 'one' stops; 'two' stays, because 'core' is still on.
        said = content_text(
            await client.call_tool("combiner__disable_collection", {"collection": "extra"})
        )
        assert "stopped one" in said and "two" not in said
        listed = (await collections())["extra"]["servers"]
        assert {k: v["running"] for k, v in listed.items()} == {"one": False, "two": True}

        unknown = content_text(
            await client.call_tool("combiner__enable_collection", {"collection": "x"})
        )
        assert unknown.startswith("Error: collection 'x' isn't loaded")


# ── per-chat collections (design 01M84GRGJ6) ─────────────────────────


async def _chat_combiner(procs: ProcFactory, tmp_path: Path) -> Any:
    """'one' is only in 'extra' (off); 'two' is in 'core' (on)."""
    one = await procs.start_http_mock("one")
    two = await procs.start_http_mock("two")
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps(
            {
                "collections": {"extra": {"enabled": False}, "core": {}},
                "mcpServers": {
                    "one": http_mock_entry(one.port) | {"collections": ["extra"]},
                    "two": http_mock_entry(two.port) | {"collections": ["core"]},
                },
            }
        )
    )
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("two", ("ready",), timeout=60.0)
    return combiner


async def _servers_seen(client: Client[Any]) -> set[str]:
    names = {str(t.name) for t in await client.list_tools()}
    return {srv for srv in ("one", "two") if any(n.startswith(srv + "_") for n in names)}


async def _call(client: Client[Any], tool: str, **args: Any) -> Any:
    return json.loads(content_text(await client.call_tool(tool, args)))


@pytest.mark.e2e
class TestPerChatCollections:
    async def test_on_in_one_chat_starts_it_for_that_chat_only(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        from fastmcp.exceptions import ToolError
        from mcp.shared.exceptions import MCPError

        combiner = await _chat_combiner(procs, tmp_path)
        a = Client(f"{combiner.mcp_url}/chat-col-aaaa", mode=CLIENT_CONNECT_MODE)
        b = Client(f"{combiner.mcp_url}/chat-col-bbbb", mode=CLIENT_CONNECT_MODE)
        async with a, b:
            assert await _servers_seen(a) == {"two"}
            said = await _call(a, "combiner__session_enable_collection", collection="extra")
            assert said["started"] == ["one"] and said["this_chat"] is True
            await combiner.wait_server_state("one", ("ready",), timeout=60.0)
            assert await _servers_seen(a) == {"one", "two"}
            assert await _servers_seen(b) == {"two"}
            with pytest.raises((ToolError, MCPError), match="off for this chat"):
                await b.call_tool("one_echo", {"message": "hi"})
            assert content_text(await a.call_tool("one_echo", {"message": "hi"})) == "Echo: hi"

            # Back to following the combiner: nothing wants 'one' any more.
            said = await _call(a, "combiner__session_reset_collection", collection="extra")
            assert said["stopped"] == ["one"] and said["this_chat_set"] is False
            assert await _servers_seen(a) == {"two"}

    async def test_off_in_one_chat_hides_it_there_and_a_server_toggle_wins(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _chat_combiner(procs, tmp_path)
        a = Client(f"{combiner.mcp_url}/chat-col-cccc", mode=CLIENT_CONNECT_MODE)
        b = Client(f"{combiner.mcp_url}/chat-col-dddd", mode=CLIENT_CONNECT_MODE)
        async with a, b:
            said = await _call(a, "combiner__session_disable_collection", collection="core")
            assert said["stopped"] == [] and said["servers"] == {"two": False}
            assert await _servers_seen(a) == set()
            assert await _servers_seen(b) == {"two"}  # still on for everyone else

            # Turning the server itself on wins over the chat's collections.
            await a.call_tool("combiner__session_enable_server", {"server_name": "two"})
            assert await _servers_seen(a) == {"two"}
            status = await _call(a, "combiner__session_status")
            assert status["collections"] == {"core": False}
            assert status["enabled_servers"] == ["two"]
            assert status["hidden_by_collections"] == ["one"]  # 'extra' is off

            # And off again for this chat.
            await a.call_tool("combiner__session_disable_server", {"server_name": "two"})
            assert await _servers_seen(a) == set()
            assert (await _call(a, "combiner__session_status"))["enabled_servers"] == []

    async def test_a_global_disable_beats_a_chats_collection(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _chat_combiner(procs, tmp_path)
        async with Client(f"{combiner.mcp_url}/chat-col-eeee", mode=CLIENT_CONNECT_MODE) as a:
            await _call(a, "combiner__session_enable_collection", collection="extra")
            await combiner.wait_server_state("one", ("ready",), timeout=60.0)
            said = content_text(
                await a.call_tool("combiner__disable_server", {"server_name": "one"})
            )
            assert "disabled" in said
            # Another per-chat change reconciles again; 'one' stays off.
            said_json = await _call(a, "combiner__session_disable_collection", collection="core")
            assert "one" not in said_json["started"]
            listed = await _call(a, "combiner__collections")
            assert listed["extra"]["servers"]["one"] == {"running": False, "this_chat": False}

    async def test_the_token_rest_route(self, procs: ProcFactory, tmp_path: Path) -> None:
        import httpx2

        combiner = await _chat_combiner(procs, tmp_path)
        token = "chat-col-rest"
        url = f"{combiner.base_url}/sessions/token/{token}/filter"
        async with httpx2.AsyncClient(timeout=60.0) as http:
            got = (await http.post(url, json={"collection_on": "extra"})).json()
            assert got["collections"] == {"extra": True}
            await combiner.wait_server_state("one", ("ready",), timeout=60.0)
            # A supervisor re-asserting only disabled_servers keeps collections.
            got = (await http.post(url, json={"disabled_servers": []})).json()
            assert got["collections"] == {"extra": True}
            bad = await http.post(url, json={"collections": {"nope": True}})
            assert bad.status_code == 400 and "unknown collection" in bad.json()["error"]
            async with Client(f"{combiner.mcp_url}/{token}", mode=CLIENT_CONNECT_MODE) as c:
                assert await _servers_seen(c) == {"one", "two"}
            # DELETE clears the chat: nothing wants 'one' now.
            await http.delete(url)
            got = (await http.get(url)).json()
            assert got["collections"] == {}
        listed: dict[str, Any] = {}
        async with Client(combiner.mcp_url, mode=CLIENT_CONNECT_MODE) as c:
            listed = await _call(c, "combiner__collections")
        assert listed["extra"]["servers"]["one"]["running"] is False

    async def test_an_abandoned_chats_collection_expires(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Two chats turn 'extra' on and go away without a DELETE: when the
        first one's state expires, 'one' keeps running for the second; when
        the second's does, 'one' stops."""
        import asyncio

        import httpx2

        one = await procs.start_http_mock("one")
        two = await procs.start_http_mock("two")
        config = tmp_path / "servers.json"
        config.write_text(
            json.dumps(
                {
                    "isolation": {"grace_seconds": 0.5, "park_ttl_seconds": 1.5},
                    "collections": {"extra": {"enabled": False}, "core": {}},
                    "mcpServers": {
                        "one": http_mock_entry(one.port) | {"collections": ["extra"]},
                        "two": http_mock_entry(two.port) | {"collections": ["core"]},
                    },
                }
            )
        )
        combiner = await procs.start_combiner(config)
        await combiner.wait_server_state("two", ("ready",), timeout=60.0)

        async def running() -> bool:
            async with Client(combiner.mcp_url, mode=CLIENT_CONNECT_MODE) as c:
                listed = await _call(c, "combiner__collections")
            return bool(listed["extra"]["servers"]["one"]["running"])

        base = f"{combiner.base_url}/sessions/token"
        async with httpx2.AsyncClient(timeout=60.0) as http:
            await http.post(f"{base}/chat-exp-aaaa/filter", json={"collection_on": "extra"})
            await combiner.wait_server_state("one", ("ready",), timeout=60.0)
            await asyncio.sleep(1.2)
            await http.post(f"{base}/chat-exp-bbbb/filter", json={"collection_on": "extra"})
            await asyncio.sleep(1.5)  # a's 2 s are up, b's aren't
            assert (await http.get(f"{base}/chat-exp-aaaa/filter")).json()["collections"] == {}
            assert (await http.get(f"{base}/chat-exp-bbbb/filter")).json()["collections"] == {
                "extra": True
            }
            assert await running()
            await asyncio.sleep(2.0)  # b's are up too
            assert (await http.get(f"{base}/chat-exp-bbbb/filter")).json()["collections"] == {}
        assert not await running()
