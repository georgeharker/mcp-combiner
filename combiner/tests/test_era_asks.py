"""Every kind of input ask, bridged across protocol eras (mcp_combiner.asks,
mcp_combiner.heldcalls).

Kinds: form, URL, sampling and roots. Directions:

- a handshake client in front of a 2026 upstream (the upstream asks by
  returning ``InputRequiredResult``; the combiner asks the client live);
- a 2026 client in front of a handshake upstream (the upstream asks with a
  server-initiated request; the combiner holds the call);
- both on 2026 (the ask passes through, its state wrapped).

Requests: tools/call, resources/read, prompts/get. The mock's ``--era-asks``
fixtures echo what their answer carried, and refuse any ``requestState`` that
isn't their own, so a leaked combiner state fails the test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp.types as mt
import pytest
from conftest import CombinerHandle, ProcFactory, http_mock_entry, write_servers_config
from fakes import content_text
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from mcp_combiner.era import OFFER_2026_ENV
from mcp_combiner.runtime import CLIENT_CONNECT_MODE

pytestmark = pytest.mark.e2e

ROOTS = ["file:///tmp/era-a", "file:///tmp/era-b"]
GUARD_URI = "data://mockup/mock/guard"
ELICIT_URI = "data://mockup/mock/elicit"


async def _combiner(
    procs: ProcFactory,
    tmp_path: Path,
    *,
    upstream_2026: bool,
    offer_downstream: bool,
    env: dict[str, str] | None = None,
) -> CombinerHandle:
    mock = await procs.start_http_mock("mockup", offer_2026=upstream_2026, era_asks=True)
    entry: dict[str, Any] = {**http_mock_entry(mock.port), "isolate": True}
    config = write_servers_config(tmp_path / "servers.json", {"mockup": entry})
    combiner = await procs.start_combiner(
        config,
        env={**({OFFER_2026_ENV: "1"} if offer_downstream else {}), **(env or {})},
        extra_args=["--log-file", str(tmp_path / "combiner.log")],
    )
    await combiner.wait_server_state("mockup", ("ready",))
    era = "2026" if upstream_2026 else "legacy"
    assert f"Upstream 'mockup': {era} protocol" in (tmp_path / "combiner.log").read_text()
    return combiner


class _User:
    """A client's answers to every kind of ask, counting what it was asked."""

    def __init__(self, url_action: str = "accept") -> None:
        self.url_action = url_action
        self.forms: list[str] = []
        self.urls: list[str] = []
        self.samples = 0
        self.roots = ROOTS

    async def elicit(self, message: str, response_type: Any, params: Any, context: object) -> Any:
        if isinstance(params, mt.ElicitRequestURLParams):
            self.urls.append(params.url)
            return ElicitResult(action=self.url_action)  # type: ignore[arg-type]
        self.forms.append(message)
        return {"name": "Ada"}

    async def sample(self, messages: Any, params: Any, context: object) -> str:
        self.samples += 1
        return "hi there"

    def client(self, url: str, *, handshake: bool, **extra: Any) -> Client[Any]:
        kwargs: dict[str, Any] = {
            "elicitation_handler": self.elicit,
            "sampling_handler": self.sample,
            "roots": self.roots,
            **extra,
        }
        if handshake:
            kwargs["mode"] = CLIENT_CONNECT_MODE
        return Client(url, **kwargs)


def _resource_text(contents: Any) -> str:
    (first,) = contents
    return str(first.text)


def _prompt_text(result: Any) -> str:
    content = result.messages[0].content
    return str(content.text)


def _echo(text: str, tag: str) -> dict[str, Any]:
    assert text.startswith(f"{tag}: "), text[:200]
    return dict(json.loads(text.removeprefix(f"{tag}: ")))


FORM = {"action": "accept", "content": {"name": "Ada"}}


# ── a handshake client, a 2026 upstream (the reverse bridge) ────────


class TestHandshakeClient2026Upstream:
    async def _start(
        self, procs: ProcFactory, tmp_path: Path, env: dict[str, str] | None = None
    ) -> CombinerHandle:
        return await _combiner(procs, tmp_path, upstream_2026=True, offer_downstream=False, env=env)

    async def test_url_accept(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-ha-url1", handshake=True) as c:
            assert c.protocol_version not in MODERN_PROTOCOL_VERSIONS
            text = content_text(await c.call_tool("mockup_mock__ask_url_guard", {}))
        assert text == "url: accept after 0 polls"
        assert len(user.urls) == 1

    async def test_an_accepted_url_is_polled_not_asked_again(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-ha-url2", handshake=True) as c:
            text = content_text(await c.call_tool("mockup_mock__ask_url_guard", {"polls": 2}))
        assert text == "url: accept after 2 polls"
        assert len(user.urls) == 1  # the user opened it once; the combiner re-polled

    async def test_a_url_step_that_never_finishes_falls_back_to_32042(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path, {"MCP_COMBINER_URL_ASK_WAIT": "1"})
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-ha-url3", handshake=True) as c:
            with pytest.raises(MCPError) as caught:
                await c.call_tool("mockup_mock__ask_url_guard", {"polls": 1000})
        assert caught.value.error.code == -32042
        assert isinstance(caught.value.error.data, dict)
        (elicitation,) = caught.value.error.data["elicitations"]
        assert elicitation["url"] == "https://example.invalid/step"
        assert len(user.urls) == 1

    async def test_url_decline_reaches_the_upstream(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User(url_action="decline")
        async with user.client(f"{combiner.mcp_url}/chat-ha-url4", handshake=True) as c:
            text = content_text(await c.call_tool("mockup_mock__ask_url_guard", {}))
        assert text == "url: decline"

    async def test_sampling(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-ha-smp1", handshake=True) as c:
            text = content_text(await c.call_tool("mockup_mock__ask_sample_guard", {}))
        assert text == "sample: hi there"
        assert user.samples == 1

    async def test_sampling_without_the_capability_fails_clearly(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path)
        url = f"{combiner.mcp_url}/chat-ha-smp2"
        async with Client(url, mode=CLIENT_CONNECT_MODE) as c:
            with pytest.raises(ToolError, match="sampling, which this client doesn't support"):
                await c.call_tool("mockup_mock__ask_sample_guard", {})

    async def test_roots(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-ha-rts1", handshake=True) as c:
            text = content_text(await c.call_tool("mockup_mock__ask_roots_guard", {}))
        assert text == "roots: " + json.dumps(ROOTS)

    async def test_resource_read(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-ha-res1", handshake=True) as c:
            text = _resource_text(await c.read_resource(GUARD_URI))
        assert _echo(text, "resource-guard") == FORM
        assert user.forms == ["resource-guard confirmation"]

    async def test_prompt(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-ha-prm1", handshake=True) as c:
            text = _prompt_text(await c.get_prompt("mockup_guard_prompt"))
        assert _echo(text, "prompt-guard") == FORM


# ── a 2026 client, a handshake upstream (held calls) ───────────────


class Test2026ClientHandshakeUpstream:
    async def _start(self, procs: ProcFactory, tmp_path: Path) -> CombinerHandle:
        return await _combiner(procs, tmp_path, upstream_2026=False, offer_downstream=True)

    async def test_url(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nh-url1", handshake=False) as c:
            assert c.protocol_version in MODERN_PROTOCOL_VERSIONS
            text = content_text(await c.call_tool("mockup_mock__elicit_url", {}))
        assert text == "elicit-url: accept"
        assert user.urls == ["https://example.invalid/step"]

    async def test_sampling(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nh-smp1", handshake=False) as c:
            text = content_text(await c.call_tool("mockup_mock__sample", {}))
        assert text == "sample: hi there"
        assert user.samples == 1

    async def test_roots(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nh-rts1", handshake=False) as c:
            text = content_text(await c.call_tool("mockup_mock__list_roots", {}))
        assert text == "roots: " + json.dumps(ROOTS)

    async def test_an_upstream_32042_becomes_a_url_ask(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nh-url2", handshake=False) as c:
            text = content_text(await c.call_tool("mockup_mock__url_required", {"times": 1}))
        assert text == "url-required: done after 2 calls"
        assert user.urls == ["https://example.invalid/step"]

    async def test_resource_read(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nh-res1", handshake=False) as c:
            text = _resource_text(await c.read_resource(ELICIT_URI))
        assert _echo(text, "resource-elicit") == FORM

    async def test_prompt(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nh-prm1", handshake=False) as c:
            text = _prompt_text(await c.get_prompt("mockup_elicit_prompt"))
        assert _echo(text, "prompt-elicit") == FORM


# ── both on 2026: the ask passes through, its state wrapped ────────


class Test2026Both:
    async def _start(self, procs: ProcFactory, tmp_path: Path) -> CombinerHandle:
        return await _combiner(procs, tmp_path, upstream_2026=True, offer_downstream=True)

    async def test_every_kind_on_tools(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nn-all1", handshake=False) as c:
            assert c.protocol_version in MODERN_PROTOCOL_VERSIONS
            url = content_text(await c.call_tool("mockup_mock__ask_url_guard", {}))
            sample = content_text(await c.call_tool("mockup_mock__ask_sample_guard", {}))
            roots = content_text(await c.call_tool("mockup_mock__ask_roots_guard", {}))
        assert url == "url: accept after 0 polls"
        assert sample == "sample: hi there"
        assert roots == "roots: " + json.dumps(ROOTS)

    async def test_resource_and_prompt(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await self._start(procs, tmp_path)
        user = _User()
        async with user.client(f"{combiner.mcp_url}/chat-nn-rp1", handshake=False) as c:
            resource = _resource_text(await c.read_resource(GUARD_URI))
            prompt = _prompt_text(await c.get_prompt("mockup_guard_prompt"))
        assert _echo(resource, "resource-guard") == FORM
        assert _echo(prompt, "prompt-guard") == FORM
