"""Server profiles (design decision 01M7E7BH8D): named groups of servers that
load by condition (platform, host, environment) and are switched on or off as
a unit; a server runs if ANY of its loaded profiles is on."""

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
    def test_a_profile_that_doesnt_apply_drops_its_servers(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path,
            {
                "profiles": {
                    "here": {"when": {"platforms": [sys.platform]}},
                    "elsewhere": {"when": {"platforms": [OTHER_PLATFORM]}},
                },
                "mcpServers": {
                    "plain": _srv(),
                    "local": _srv(profiles=["here"]),
                    "foreign": _srv(profiles=["elsewhere"]),
                    "both": _srv(profiles=["here", "elsewhere"]),
                },
            },
        )
        assert set(cfg.servers) == {"plain", "local", "both"}
        assert set(cfg.profiles) == {"here"}
        assert not any(s.disabled for s in cfg.servers.values())

    def test_hosts_and_env_conditions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROFILE_TEST_SITE", "work")
        monkeypatch.delenv("PROFILE_TEST_UNSET", raising=False)
        cfg = _load(
            tmp_path,
            {
                "profiles": {
                    "this-host": {"when": {"hosts": [HOST.upper()]}},
                    "other-host": {"when": {"hosts": ["no-such-host-x"]}},
                    "work": {"when": {"env": {"PROFILE_TEST_SITE": "work"}}},
                    "home": {"when": {"env": {"PROFILE_TEST_SITE": "home"}}},
                    "needs-var": {"when": {"env": ["PROFILE_TEST_UNSET"]}},
                },
                "mcpServers": {},
            },
        )
        assert set(cfg.profiles) == {"this-host", "work"}

    def test_on_wins(self, tmp_path: Path) -> None:
        cfg = _load(
            tmp_path,
            {
                "profiles": {"on": {}, "off": {"enabled": False}},
                "mcpServers": {
                    "only-off": _srv(profiles=["off"]),
                    "on-and-off": _srv(profiles=["on", "off"]),
                    "self-disabled": _srv(profiles=["on"], disabled=True),
                },
            },
        )
        assert cfg.servers["only-off"].disabled
        assert not cfg.servers["on-and-off"].disabled
        assert cfg.servers["self-disabled"].disabled  # its own flag still wins

    def test_the_environment_overrides_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_COMBINER_PROFILES", "+music, -google")
        cfg = _load(
            tmp_path,
            {
                "profiles": {"music": {"enabled": False}, "google": {}},
                "mcpServers": {
                    "ableton": _srv(profiles=["music"]),
                    "gws": _srv(profiles=["google"]),
                },
            },
        )
        assert not cfg.servers["ableton"].disabled
        assert cfg.servers["gws"].disabled

    @pytest.mark.parametrize(
        ("doc", "message"),
        [
            ({"mcpServers": {"a": _srv(profiles=["nope"])}}, "unknown profile"),
            ({"profiles": {"p": {"when": {"platforms": ["macos"]}}}}, "unknown platform"),
            ({"profiles": {"p": {"enabled": "no"}}}, "true or false"),
            ({"profiles": {"p": {"enbled": False}}}, "unknown key"),
            ({"profiles": {"p": {"when": {"os": ["darwin"]}}}}, "unknown when key"),
            ({"profiles": ["mac"]}, "must be an object"),
            ({"mcpServers": {"a": _srv(profiles="mac")}}, "list of profile names"),
        ],
    )
    def test_bad_profiles_fail_the_load(
        self, tmp_path: Path, doc: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            _load(tmp_path, {"mcpServers": {}, **doc})


def test_a_failed_enable_or_disable_is_not_counted_as_done() -> None:
    """The profile toggle reports failures from enable_server / disable_server
    by their messages; "enabled but failed to mount" is a failure."""
    from mcp_combiner.meta_tools import went_wrong

    assert not went_wrong("Server 'a' enabled and mounted")
    assert not went_wrong("Server 'a' disabled and unmounted (1 provider(s) removed)")
    assert went_wrong("Server 'a' enabled but failed to mount: boom")
    assert went_wrong("Server 'a' disabled but failed to unmount: boom")
    assert went_wrong("Error: Server 'a' not found")


@pytest.mark.e2e
async def test_turning_a_profile_on_and_off(procs: ProcFactory, tmp_path: Path) -> None:
    one = await procs.start_http_mock("one")
    two = await procs.start_http_mock("two")
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps(
            {
                "profiles": {"extra": {"enabled": False}, "core": {}},
                "mcpServers": {
                    "one": http_mock_entry(one.port) | {"profiles": ["extra"]},
                    "two": http_mock_entry(two.port) | {"profiles": ["extra", "core"]},
                },
            }
        )
    )
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("two", ("ready",), timeout=60.0)

    async with Client(combiner.mcp_url, mode=CLIENT_CONNECT_MODE) as client:

        async def profiles() -> dict[str, Any]:
            result: dict[str, Any] = json.loads(
                content_text(await client.call_tool("combiner__profiles", {}))
            )
            return result

        assert await profiles() == {
            "core": {"enabled": True, "servers": {"two": True}},
            "extra": {"enabled": False, "servers": {"one": False, "two": True}},
        }

        said = content_text(
            await client.call_tool("combiner__enable_profile", {"profile": "extra"})
        )
        assert "started one" in said
        await combiner.wait_server_state("one", ("ready",), timeout=60.0)

        # Off again: 'one' stops; 'two' stays, because 'core' is still on.
        said = content_text(
            await client.call_tool("combiner__disable_profile", {"profile": "extra"})
        )
        assert "stopped one" in said and "two" not in said
        listed = (await profiles())["extra"]["servers"]
        assert listed == {"one": False, "two": True}

        unknown = content_text(await client.call_tool("combiner__enable_profile", {"profile": "x"}))
        assert unknown.startswith("Error: profile 'x' isn't loaded")
