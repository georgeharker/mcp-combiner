"""Out-of-band OAuth authorization (authprompt): the authorization URL reaches
the user through the MCP client (URL-mode elicitation, ``-32042``) instead of
— or as well as — a browser on the combiner host.

Unit tests cover config resolution, the provider's redirect URI and detached
mode, and the pending-authorization lifecycle. The e2e tests run the real flow
against the mock OAuth server: the loopback callback at the provider level, and
the full chain through a combiner subprocess with ``oauth.public_url`` — a
downstream client gets ``-32042``, "the user" follows the link, the combiner's
``/oauth/callback/<server>`` completes it, and the retried call succeeds.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import mcp.types as mt
import pytest
from conftest import (
    OAuthMockFactory,
    ProcFactory,
    ServedOAuthMock,
    free_port_pair,
    poll_until,
    stdio_mock_entry,
    write_servers_config,
)
from fakes import content_text, recording_connection
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError, UrlElicitationRequiredError

from mcp_combiner import authpages, authprompt, webauth
from mcp_combiner.auth import (
    _RefreshTokenOAuth,
    authorizes_out_of_band,
    build_auth,
    reload_stored_tokens,
)
from mcp_combiner.config import CombinerConfig, OAuthConfig, resolve_auth_prompt
from mcp_combiner.runtime import CLIENT_CONNECT_MODE


@pytest.fixture(autouse=True)
def _clean_authprompt() -> Iterator[None]:
    authprompt.reset()
    authprompt.set_on_authorized(None)
    yield
    authprompt.reset()
    authprompt.set_on_authorized(None)


def _pending(
    server: str = "srv",
    *,
    exchange: authprompt.CodeExchange | None = None,
    elicit: bool = True,
    ttl: float = 60.0,
) -> authprompt.PendingAuthorization:
    async def _no_exchange(code: str, iss: str | None) -> None:
        return None

    return authprompt.PendingAuthorization(
        server=server,
        url="https://auth.example/authorize?state=s1",
        state="s1",
        exchange=exchange or _no_exchange,
        expires_at=time.monotonic() + ttl,
        elicit=elicit,
    )


# ── config ─────────────────────────────────────────────────────────


class TestPromptConfig:
    def test_default_keeps_browser_without_public_url(self) -> None:
        assert resolve_auth_prompt("s", None, None, None) == "browser"

    def test_default_elicits_with_public_url(self) -> None:
        assert resolve_auth_prompt("s", None, None, "https://c.ts.net") == "both"

    def test_server_prompt_overrides_global(self) -> None:
        assert resolve_auth_prompt("s", "elicit", "browser", None) == "elicit"
        assert resolve_auth_prompt("s", None, "elicit", None) == "elicit"

    def test_invalid_prompt_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="oauth prompt must be one of"):
            resolve_auth_prompt("s", "popup", None, None)

    def test_global_block_parses_public_url_and_prompt(self) -> None:
        cfg = OAuthConfig.from_dict({"public_url": "https://c.ts.net/", "prompt": "elicit"})
        assert cfg.public_url == "https://c.ts.net"
        assert cfg.prompt == "elicit"
        with pytest.raises(ValueError):
            OAuthConfig.from_dict({"prompt": "sometimes"})

    def _load(self, tmp_path: Path, doc: dict[str, Any]) -> CombinerConfig:
        path = tmp_path / "servers.json"
        path.write_text(json.dumps(doc))
        return CombinerConfig.load(str(path))

    def test_elicit_without_public_url_warns_but_loads(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        server = {"url": "http://x/mcp", "auth": {"oauth": {"prompt": "elicit"}}}
        with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
            cfg = self._load(tmp_path, {"servers": {"g": server}})
        assert "g" in cfg.servers
        assert "without oauth.public_url" in caplog.text

    def test_public_url_silences_the_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        doc = {
            "servers": {"g": {"url": "http://x/mcp", "auth": "oauth"}},
            "oauth": {"public_url": "https://c.ts.net"},
        }
        with caplog.at_level(logging.WARNING, logger="mcp-combiner"):
            self._load(tmp_path, doc)
        assert "public_url" not in caplog.text

    def test_invalid_server_prompt_fails_the_load(self, tmp_path: Path) -> None:
        server = {"url": "http://x/mcp", "auth": {"oauth": {"prompt": "nope"}}}
        with pytest.raises(ValueError, match="Server 'g'"):
            self._load(tmp_path, {"servers": {"g": server}})


# ── the provider ───────────────────────────────────────────────────


def _oauth(tmp_path: Path, **kw: Any) -> _RefreshTokenOAuth:
    auth = build_auth(
        "g", auth_config=kw.pop("auth_config", "oauth"), server_url="http://up/mcp", **kw
    )
    assert isinstance(auth, _RefreshTokenOAuth)
    return auth


class TestProvider:
    def test_browser_on_loopback_keeps_the_inline_flow(self, tmp_path: Path) -> None:
        oauth = _oauth(tmp_path, token_dir=tmp_path)
        uris = oauth.context.client_metadata.redirect_uris
        assert uris is not None
        assert str(uris[0]) == f"http://localhost:{oauth.redirect_port}/callback"
        assert not oauth.detached
        assert not authorizes_out_of_band(oauth)

    def test_public_url_redirects_to_the_combiner(self, tmp_path: Path) -> None:
        oauth = _oauth(tmp_path, token_dir=tmp_path, public_url="https://c.ts.net")
        uris = oauth.context.client_metadata.redirect_uris
        assert uris is not None
        assert str(uris[0]) == "https://c.ts.net/oauth/callback/g"
        assert oauth.prompt == "both"
        assert authorizes_out_of_band(oauth)

    def test_public_url_patches_static_client_registration(self, tmp_path: Path) -> None:
        oauth = _oauth(
            tmp_path,
            token_dir=tmp_path,
            public_url="https://c.ts.net",
            auth_config={"oauth": {"client_id": "web-client", "client_secret": "s"}},
        )
        static = oauth._static_client_info
        assert static is not None and static.redirect_uris is not None
        assert [str(u) for u in static.redirect_uris] == ["https://c.ts.net/oauth/callback/g"]

    def test_elicit_on_loopback_is_detached(self, tmp_path: Path) -> None:
        oauth = _oauth(tmp_path, token_dir=tmp_path, auth_config={"oauth": {"prompt": "elicit"}})
        assert oauth.detached
        assert oauth.public_url is None


# ── presenting a pending authorization ─────────────────────────────


class TestPromptError:
    def test_capable_client_gets_url_elicitation(self) -> None:
        pending = _pending()
        connection, _ = recording_connection(url_elicitation=True)
        err = authprompt.prompt_error(pending, connection)
        assert isinstance(err, UrlElicitationRequiredError)
        assert err.error.code == mt.URL_ELICITATION_REQUIRED
        assert [e.url for e in err.elicitations] == [pending.url]
        assert err.elicitations[0].elicitation_id == pending.elicitation_id
        assert connection in pending.notified

    def test_other_clients_get_the_url_in_the_tool_error(self) -> None:
        pending = _pending()
        err = authprompt.prompt_error(pending, recording_connection()[0])
        assert isinstance(err, ToolError)
        assert pending.url in str(err)
        assert not pending.notified

    def test_browser_only_flow_never_hands_out_the_url(self) -> None:
        pending = _pending(elicit=False)
        err = authprompt.prompt_error(pending, recording_connection(url_elicitation=True)[0])
        assert isinstance(err, ToolError)
        assert pending.url not in str(err)


class TestComplete:
    async def test_success_exchanges_rearms_and_notifies(self) -> None:
        exchanged: list[str] = []
        authorized: list[str] = []

        async def exchange(code: str, iss: str | None) -> None:
            exchanged.append(code)

        async def on_authorized(server: str) -> None:
            authorized.append(server)

        authprompt.set_on_authorized(on_authorized)
        pending = _pending(exchange=exchange)
        connection, outbound = recording_connection(url_elicitation=True)
        pending.notified.add(connection)
        authprompt.publish(pending)

        await authprompt.complete("srv", "the-code", "s1", None)

        assert exchanged == ["the-code"]
        assert authorized == ["srv"]
        assert outbound.sent("notifications/elicitation/complete") == [
            {"elicitationId": pending.elicitation_id}
        ]
        assert authprompt.pending_for("srv") is None

    @pytest.mark.parametrize(
        ("server", "code", "state", "error", "reason"),
        [
            ("other", "c", "s1", None, "no authorization is pending"),
            ("srv", "c", "wrong", None, "state mismatch"),
            ("srv", "c", None, None, "state mismatch"),
            ("srv", None, "s1", "access_denied", "not granted"),
            ("srv", None, "s1", None, "no authorization code"),
        ],
    )
    async def test_rejected_callbacks(
        self, server: str, code: str | None, state: str | None, error: str | None, reason: str
    ) -> None:
        exchanged: list[str] = []

        async def exchange(code: str, iss: str | None) -> None:
            exchanged.append(code)

        authprompt.publish(_pending(exchange=exchange))
        with pytest.raises(authprompt.CallbackRejected, match=reason):
            await authprompt.complete(server, code, state, error)
        assert exchanged == []

    def test_a_junk_link_id_is_unknown_not_an_error(self) -> None:
        pending = _pending()
        authprompt.publish(pending)
        assert authprompt.pending_by_elicitation("é" * 4) is None
        assert authprompt.pending_by_elicitation(pending.elicitation_id) is pending

    def test_expired_pending_is_dropped(self) -> None:
        authprompt.publish(_pending(ttl=-1.0))
        assert authprompt.pending_for("srv") is None

    def test_publish_replaces_an_older_pending(self) -> None:
        first, second = _pending(), _pending()
        authprompt.publish(first)
        authprompt.publish(second)
        assert authprompt.pending_for("srv") is second


# ── e2e: the real flow against the mock OAuth server ───────────────


_AUTHORIZE_BUTTON = re.compile(r'<a class="button" id="go" href="([^"]+)"')


async def _follow_as_user(url: str) -> httpx2.Response:
    """What the user's browser does: open the link, press Authorize on the
    combiner's page if it shows one, and follow the redirects (the mock
    authorization server auto-consents)."""
    async with httpx2.AsyncClient(follow_redirects=True, timeout=30.0) as http:
        page = await http.get(url)
        button = _AUTHORIZE_BUTTON.search(page.text)
        if button is None:
            return page
        return await http.get(html.unescape(button.group(1)))


@pytest.mark.e2e
class TestLoopbackDetachedFlow:
    async def test_elicit_without_public_url_completes_on_the_loopback(
        self, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock: ServedOAuthMock = await oauth_mock()
        oauth = build_auth(
            "mock",
            auth_config={"oauth": {"prompt": "elicit"}},
            server_url=mock.mcp_url,
            token_dir=tmp_path,
        )
        assert isinstance(oauth, _RefreshTokenOAuth)

        # The connect fails fast — no waiting on the user — with the link published.
        with pytest.raises(Exception):
            async with Client(
                StreamableHttpTransport(url=mock.mcp_url), auth=oauth, mode=CLIENT_CONNECT_MODE
            ):
                pass
        pending = authprompt.pending_for("mock")
        assert pending is not None
        assert f"localhost%3A{oauth.redirect_port}" in pending.url

        page = await _follow_as_user(pending.url)
        assert page.status_code == 200

        async def _tokens() -> bool:
            return oauth.context.current_tokens is not None

        await poll_until(_tokens, timeout=15.0, desc="tokens stored after callback")
        assert authprompt.pending_for("mock") is None

        async with Client(
            StreamableHttpTransport(url=mock.mcp_url), auth=oauth, mode=CLIENT_CONNECT_MODE
        ) as client:
            result = await client.call_tool("echo", {"message": "hi"})
        assert content_text(result) == "Echo: hi"
        assert mock.provider.audit.pkce_failures == 0

    async def test_second_provider_picks_up_tokens_stored_by_the_first(
        self, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        """Two providers for one server share its token store. The link comes
        from the first; after it completes, the second (already loaded, with no
        tokens) must reconnect from the stored tokens rather than start another
        authorization — the live failure this pins."""
        mock: ServedOAuthMock = await oauth_mock()

        def provider() -> _RefreshTokenOAuth:
            auth = build_auth(
                "mock",
                auth_config={"oauth": {"prompt": "elicit"}},
                server_url=mock.mcp_url,
                token_dir=tmp_path,
            )
            assert isinstance(auth, _RefreshTokenOAuth)
            return auth

        first, second = provider(), provider()
        for auth in (first, second):  # both load the (empty) store and hit the link
            with pytest.raises(Exception):
                async with Client(
                    StreamableHttpTransport(url=mock.mcp_url), auth=auth, mode=CLIENT_CONNECT_MODE
                ):
                    pass
        pending = authprompt.pending_for("mock")
        assert pending is not None
        assert (await _follow_as_user(pending.url)).status_code == 200

        async def _stored() -> bool:
            return first.context.current_tokens is not None

        await poll_until(_stored, timeout=15.0, desc="tokens stored by the first provider")

        reload_stored_tokens(second)
        async with Client(
            StreamableHttpTransport(url=mock.mcp_url), auth=second, mode=CLIENT_CONNECT_MODE
        ) as client:
            result = await client.call_tool("echo", {"message": "hi"})
        assert content_text(result) == "Echo: hi"
        assert authprompt.pending_for("mock") is None  # no second authorization


class _Notifications(MessageHandler):
    def __init__(self) -> None:
        self.seen: list[mt.ServerNotification] = []

    async def on_notification(self, message: mt.ServerNotification) -> None:
        self.seen.append(message)

    def elicitation_completed(self) -> list[str]:
        return [
            n.params.elicitation_id
            for n in self.seen
            if isinstance(n, mt.ElicitCompleteNotification)
        ]


async def _never_elicit(
    message: str,
    response_type: type[Any] | None,
    params: mt.ElicitRequestParams,
    context: object,  # never read; accepts fastmcp's RequestContext
) -> dict[str, Any]:
    raise AssertionError(f"unexpected elicitation/create: {message}")


@pytest.mark.e2e
class TestCombinerPublicUrlFlow:
    """The full chain through a combiner subprocess: startup auth goes
    out-of-band, the client is handed the link, the combiner's own
    /oauth/callback/<server> completes it, and the call then succeeds."""

    async def _start(
        self,
        procs: ProcFactory,
        mock: ServedOAuthMock,
        tmp_path: Path,
        env: dict[str, str] | None = None,
        protocol: str | None = None,
        web_auth: dict[str, Any] | None = None,
    ) -> str:
        port = free_port_pair()
        entry: dict[str, Any] = {"url": mock.mcp_url, "transport": "http", "auth": "oauth"}
        if protocol is not None:
            entry["protocol"] = protocol
        config = write_servers_config(
            tmp_path / "servers.json",
            {"mock": entry},
            oauth={
                "public_url": f"http://127.0.0.1:{port}",
                "prompt": "elicit",
                "token_dir": str(tmp_path / "tokens"),
            },
            web_auth=web_auth,
        )
        combiner = await procs.start_combiner(
            config,
            port=port,
            # Kept for diagnosis: the subprocess has no other output.
            extra_args=["--log-file", str(tmp_path / "combiner.log")],
            env=env,
        )
        await combiner.wait_server_state("mock", ("auth_failed",))
        return combiner.mcp_url

    async def test_elicitation_capable_client(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        notes = _Notifications()

        async with Client(
            mcp_url,
            elicitation_handler=_never_elicit,
            message_handler=notes,
            mode=CLIENT_CONNECT_MODE,
        ) as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
            error = caught.value.error
            assert error.code == mt.URL_ELICITATION_REQUIRED
            assert isinstance(error.data, dict)
            (elicitation,) = error.data["elicitations"]
            # With a public_url the link is the combiner's own page (authpages).
            assert "/oauth/authorize/" in elicitation["url"]

            page = await _follow_as_user(elicitation["url"])
            assert page.status_code == 200
            assert "is connected" in page.text

            async def _completed() -> list[str]:
                return notes.elicitation_completed()

            done = await poll_until(_completed, timeout=15.0, desc="elicitation/complete")
            assert done == [elicitation["elicitationId"]]

            async def _echo() -> str | None:
                try:
                    return content_text(await client.call_tool("mock_echo", {"message": "hi"}))
                except ToolError:
                    return None

            assert await poll_until(_echo, timeout=30.0, desc="call after auth") == "Echo: hi"

    async def test_protocol_2026_on_an_oauth_upstream(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        """auto can't probe an OAuth server (it would be refused), so 2026 is
        forced: authorization still runs, and calls then ride the 2026 era."""
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path, protocol="2026")
        log = (tmp_path / "combiner.log").read_text()
        assert "Upstream 'mock': 2026 protocol (setting '2026')" in log
        notes = _Notifications()

        async def answer(
            message: str,
            response_type: type[Any] | None,
            params: mt.ElicitRequestParams,
            context: object,
        ) -> dict[str, Any]:
            if not isinstance(params, mt.ElicitRequestFormParams):
                raise AssertionError(f"unexpected elicitation: {message}")
            return {"environment": "prod", "confirm": True, "ticket": "T-1", "replicas": 3}

        async with Client(
            mcp_url, elicitation_handler=answer, message_handler=notes, mode=CLIENT_CONNECT_MODE
        ) as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
            assert isinstance(caught.value.error.data, dict)
            (elicitation,) = caught.value.error.data["elicitations"]
            page = await _follow_as_user(elicitation["url"])
            assert page.status_code == 200, page.text

            async def _echo() -> str | None:
                try:
                    return content_text(await client.call_tool("mock_echo", {"message": "hi"}))
                except (ToolError, MCPError):
                    return None

            assert await poll_until(_echo, timeout=30.0, desc="call after auth") == "Echo: hi"
            # The guard asks by returning InputRequiredResult, which exists only
            # on a 2026 connection: its answer coming back proves the era.
            guard = content_text(await client.call_tool("mock_mock__ask_guard", {}))
            assert guard.startswith("guard: ") and '"action": "accept"' in guard

    async def test_client_without_url_elicitation_gets_the_link_as_text(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)

        async with Client(mcp_url, mode=CLIENT_CONNECT_MODE) as client:
            with pytest.raises(ToolError, match="needs authorization") as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
        # The error carries the combiner's SHORT link: a page naming the server,
        # whose Authorize button leads on to the real authorization URL.
        short = next(w for w in str(caught.value).split() if "/oauth/authorize/" in w)
        async with httpx2.AsyncClient() as http:
            page = await http.get(short, follow_redirects=False)
            assert page.status_code == 200
            assert "Authorize <code>mock</code>" in page.text
            button = _AUTHORIZE_BUTTON.search(page.text)
            assert button is not None
            assert "/oauth/authorize?" in html.unescape(button.group(1))
            assert 'data-seconds="' in page.text  # the expiry countdown
            # Self-contained: no external stylesheets, scripts or images.
            assert not re.search(
                r'(src|href)="https?://(?!127\.0\.0\.1)', page.text.split('id="go"')[0]
            )
            assert page.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
            # ?go=1 keeps the direct redirect for scripts.
            direct = await http.get(short + "?go=1", follow_redirects=False)
            assert direct.status_code == 302
            assert "/oauth/authorize?" in direct.headers["location"]
            # An unknown (expired, used) link gets the styled gone page.
            gone = await http.get(short.rsplit("/", 1)[0] + "/" + "0" * 32)
            assert gone.status_code == 404
            assert "expired or was already used" in gone.text
            assert 'class="mark"' in gone.text

    async def test_bad_state_is_refused_by_the_callback(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        base = mcp_url.removesuffix("/mcp")
        async with httpx2.AsyncClient() as http:
            r = await http.get(f"{base}/oauth/callback/mock", params={"code": "x", "state": "y"})
        assert r.status_code == 400
        assert "state mismatch" in r.text

    async def test_expired_link_is_replaced_on_the_next_call(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        """Once a link expires nothing retries on its own; the next call routed
        to the server re-runs the flow and is handed a fresh link."""
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path, env={"MCP_COMBINER_AUTH_LINK_TTL": "2"})

        async def _link(client: Client[Any]) -> tuple[str, str]:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
            assert caught.value.error.code == mt.URL_ELICITATION_REQUIRED
            assert isinstance(caught.value.error.data, dict)
            (elicitation,) = caught.value.error.data["elicitations"]
            return elicitation["url"], elicitation["elicitationId"]

        async with Client(
            mcp_url, elicitation_handler=_never_elicit, mode=CLIENT_CONNECT_MODE
        ) as client:
            first_url, first_id = await _link(client)
            await asyncio.sleep(2.5)
            second_url, second_id = await _link(client)
            assert (second_url, second_id) != (first_url, first_id)

            page = await _follow_as_user(second_url)
            assert page.status_code == 200, page.text

            async def _echo() -> str | None:
                try:
                    return content_text(await client.call_tool("mock_echo", {"message": "hi"}))
                except (ToolError, MCPError):
                    return None

            assert await poll_until(_echo, timeout=30.0, desc="call after auth") == "Echo: hi"

    async def test_revoked_tokens_force_a_fresh_link(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        """The manual test loop: authorize, call, revoke every token at the
        mock (POST /control/oauth/revoke), and the next call is handed a NEW
        link — refresh fails, the flow goes out-of-band again, never inline."""
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)

        async def _link(client: Client[Any]) -> tuple[str, str]:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
            assert caught.value.error.code == mt.URL_ELICITATION_REQUIRED
            assert isinstance(caught.value.error.data, dict)
            (elicitation,) = caught.value.error.data["elicitations"]
            return elicitation["url"], elicitation["elicitationId"]

        async def _echo(client: Client[Any]) -> str | None:
            try:
                return content_text(await client.call_tool("mock_echo", {"message": "hi"}))
            except (ToolError, MCPError):
                return None

        async with Client(
            mcp_url, elicitation_handler=_never_elicit, mode=CLIENT_CONNECT_MODE
        ) as client:
            first_url, first_id = await _link(client)
            assert (await _follow_as_user(first_url)).status_code == 200
            assert await poll_until(lambda: _echo(client), timeout=30.0) == "Echo: hi"

            async with httpx2.AsyncClient() as http:
                r = await http.post(f"{mock.base_url}/control/oauth/revoke")
            assert r.json()["revoked"]["access_tokens"] >= 1

            second_url, second_id = await _link(client)
            assert second_id != first_id
            async with httpx2.AsyncClient() as http:
                renewal = await http.get(second_url)
            assert "has expired or was revoked" in renewal.text
            assert (await _follow_as_user(second_url)).status_code == 200
            assert await poll_until(lambda: _echo(client), timeout=30.0) == "Echo: hi"

    async def test_the_page_says_who_asked_and_where_it_leads(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        async with Client(
            f"{mcp_url}/page-chat-0001",
            elicitation_handler=_never_elicit,
            mode=CLIENT_CONNECT_MODE,
            client_info=mt.Implementation(name="pi", title="Pi agent", version="1"),
        ) as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
        assert isinstance(caught.value.error.data, dict)
        (elicitation,) = caught.value.error.data["elicitations"]
        assert "?t=" not in elicitation["url"]  # web_auth off: no ticket
        async with httpx2.AsyncClient() as http:
            page = (await http.get(elicitation["url"])).text
        assert "<dt>Signs in at</dt><dd>127.0.0.1</dd>" in page
        assert "Pi agent" in page and "chat page-cha…" in page
        assert "needs your permission to connect" in page  # first sign-in
        # The status page links the pending authorization.
        async with httpx2.AsyncClient() as http:
            status = (await http.get(mcp_url.removesuffix("/mcp") + "/status")).text
        assert f'href="/oauth/authorize/{elicitation["elicitationId"]}"' in status

    async def test_web_auth_gates_the_page_and_tickets_open_it(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        password = "open sesame"
        mcp_url = await self._start(
            procs,
            mock,
            tmp_path,
            web_auth={
                "mode": "password",
                "password_hash": webauth.hash_password(password),
                "exempt_local": False,
            },
        )
        async with Client(
            mcp_url, elicitation_handler=_never_elicit, mode=CLIENT_CONNECT_MODE
        ) as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
        assert isinstance(caught.value.error.data, dict)
        (elicitation,) = caught.value.error.data["elicitations"]
        ticketed = elicitation["url"]
        assert "?t=" in ticketed
        bare = ticketed.split("?", 1)[0]

        async with httpx2.AsyncClient() as http:
            # A leaked bare link: a sign-in form, and no way on to the provider.
            locked = await http.get(bare)
            assert locked.status_code == 401
            assert 'type="password"' in locked.text and "Authorize <code>mock</code>" in locked.text
            assert _AUTHORIZE_BUTTON.search(locked.text) is None
            assert "/oauth/authorize?" not in locked.text
            assert (await http.get(bare + "?go=1")).status_code == 401
            # Another link's ticket doesn't open this one.
            foreign = ticketed.split("t=", 1)[0] + "t=" + "x.y"
            assert (await http.get(foreign)).status_code == 401
            wrong = await http.post(bare, data={"password": "nope"})
            assert wrong.status_code == 401 and "That password isn" in wrong.text

        async with httpx2.AsyncClient() as http:
            signed_in = await http.post(bare, data={"password": password})
            assert signed_in.status_code == 303
            assert webauth.COOKIE in signed_in.headers["set-cookie"]
            page = await http.get(bare)
            assert page.status_code == 200 and _AUTHORIZE_BUTTON.search(page.text)

        # The ticketed link from the client opens straight through (no prompt)
        # and the authorization completes.
        done = await _follow_as_user(ticketed)
        assert done.status_code == 200 and "is connected" in done.text

    async def test_a_proxied_request_is_not_this_machine(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        """tailscale serve connects from loopback; X-Forwarded-For names the
        real browser, which must not inherit the loopback exemption."""
        mock = await oauth_mock()
        mcp_url = await self._start(
            procs,
            mock,
            tmp_path,
            web_auth={"mode": "password", "password_hash": webauth.hash_password("pw")},
        )
        async with Client(mcp_url, mode=CLIENT_CONNECT_MODE) as client:
            with pytest.raises(ToolError) as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
        short = next(w for w in str(caught.value).split() if "/oauth/authorize/" in w)
        bare = short.split("?", 1)[0]
        async with httpx2.AsyncClient() as http:
            assert (await http.get(bare)).status_code == 200  # this machine
            proxied = await http.get(bare, headers={"x-forwarded-for": "100.64.9.9"})
            assert proxied.status_code == 401
            # ...unless the forwarded browser is this machine too.
            own = await http.get(bare, headers={"x-forwarded-for": "127.0.0.1"})
            assert own.status_code == 200


@pytest.mark.e2e
class TestCombinerLoopbackFlow:
    """The local setup (no ``oauth.public_url``): the link redirects to the
    combiner's loopback callback listener, and a completed authorization must
    reconnect the server so the next call succeeds."""

    async def test_link_completes_and_the_retry_succeeds(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        config = write_servers_config(
            tmp_path / "servers.json",
            {
                "mock": {
                    "url": mock.mcp_url,
                    "transport": "http",
                    "auth": {"oauth": {"prompt": "elicit"}},
                }
            },
            oauth={"token_dir": str(tmp_path / "tokens")},
        )
        combiner = await procs.start_combiner(
            config, extra_args=["--log-file", str(tmp_path / "combiner.log")]
        )
        await combiner.wait_server_state("mock", ("auth_failed",))

        async with Client(combiner.mcp_url, mode=CLIENT_CONNECT_MODE) as client:
            with pytest.raises(ToolError, match="needs authorization") as caught:
                await client.call_tool("mock_echo", {"message": "hi"})
            link = next(w for w in str(caught.value).split() if w.startswith("http"))
            page = await _follow_as_user(link)
            assert page.status_code == 200, page.text

            await combiner.wait_server_state("mock", ("ready", "connected"), timeout=30.0)

            async def _echo() -> str | None:
                try:
                    return content_text(await client.call_tool("mock_echo", {"message": "hi"}))
                except ToolError:
                    return None

            assert await poll_until(_echo, timeout=30.0, desc="call after auth") == "Echo: hi"


@pytest.mark.e2e
class TestAuthorizeServerTool:
    """combiner__authorize_server: authorization as a call-time operation on an
    always-mounted tool, for upstreams that mount no tools until authorized."""

    async def _start(
        self,
        procs: ProcFactory,
        mock: ServedOAuthMock,
        tmp_path: Path,
        login_hint: str | None = None,
    ) -> str:
        port = free_port_pair()
        auth: dict[str, Any] | str = (
            {"oauth": {"login_hint": login_hint}} if login_hint is not None else "oauth"
        )
        config = write_servers_config(
            tmp_path / "servers.json",
            {
                "mock": {"url": mock.mcp_url, "transport": "http", "auth": auth},
                "plain": stdio_mock_entry("plain"),
            },
            oauth={
                "public_url": f"http://127.0.0.1:{port}",
                "prompt": "elicit",
                "token_dir": str(tmp_path / "tokens"),
            },
        )
        combiner = await procs.start_combiner(
            config, port=port, extra_args=["--log-file", str(tmp_path / "combiner.log")]
        )
        await combiner.wait_server_state("mock", ("auth_failed",))
        return combiner.mcp_url

    async def test_an_unauthorized_server_gets_a_url_elicitation(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        async with Client(
            mcp_url, elicitation_handler=_never_elicit, mode=CLIENT_CONNECT_MODE
        ) as client:
            # The server mounts nothing until authorized: the tool is the way in.
            names = {t.name for t in await client.list_tools()}
            assert "combiner__authorize_server" in names
            assert not any(n.startswith("mock_") for n in names)

            with pytest.raises(MCPError) as caught:
                await client.call_tool("combiner__authorize_server", {"server_name": "mock"})
            error = caught.value.error
            assert error.code == mt.URL_ELICITATION_REQUIRED
            assert isinstance(error.data, dict)
            (elicitation,) = error.data["elicitations"]
            assert {"url", "message", "elicitationId"} <= set(elicitation)
            assert "'mock'" in elicitation["message"]

            page = await _follow_as_user(elicitation["url"])
            assert page.status_code == 200, page.text

            async def _authorized() -> str | None:
                try:
                    return content_text(
                        await client.call_tool(
                            "combiner__authorize_server", {"server_name": "mock"}
                        )
                    )
                except (ToolError, MCPError):
                    return None

            done = await poll_until(_authorized, timeout=30.0, desc="authorized")
            assert "already authorized" in done

    async def test_clients_without_url_elicitation_get_the_link_as_text(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        async with Client(mcp_url, mode=CLIENT_CONNECT_MODE) as client:
            with pytest.raises(ToolError, match="needs authorization") as caught:
                await client.call_tool("combiner__authorize_server", {"server_name": "mock"})
        assert "/oauth/authorize/" in str(caught.value)

    async def test_unknown_and_non_oauth_servers_are_refused(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path)
        async with Client(mcp_url, mode=CLIENT_CONNECT_MODE) as client:
            with pytest.raises(ToolError, match="No server 'nope'; configured: mock, plain"):
                await client.call_tool("combiner__authorize_server", {"server_name": "nope"})
            with pytest.raises(ToolError, match="doesn't use OAuth"):
                await client.call_tool("combiner__authorize_server", {"server_name": "plain"})

    async def test_login_hint_names_the_account_everywhere(
        self, procs: ProcFactory, oauth_mock: OAuthMockFactory, tmp_path: Path
    ) -> None:
        """Which account to use, when several servers share a provider (Google):
        in the elicitation message, on the page, and to the provider."""
        mock = await oauth_mock()
        mcp_url = await self._start(procs, mock, tmp_path, login_hint="george@example.com")
        async with Client(
            mcp_url, elicitation_handler=_never_elicit, mode=CLIENT_CONNECT_MODE
        ) as client:
            with pytest.raises(MCPError) as caught:
                await client.call_tool("combiner__authorize_server", {"server_name": "mock"})
        assert isinstance(caught.value.error.data, dict)
        (elicitation,) = caught.value.error.data["elicitations"]
        assert "sign in as george@example.com" in elicitation["message"]
        async with httpx2.AsyncClient() as http:
            page = await http.get(elicitation["url"])
        assert "Sign in as <strong>george@example.com</strong>" in page.text
        button = _AUTHORIZE_BUTTON.search(page.text)
        assert button is not None
        assert "login_hint=george%40example.com" in html.unescape(button.group(1))

        # The provider signs that account in and names it: the completion page says so.
        done = await _follow_as_user(elicitation["url"])
        assert "Signed in as <strong>george@example.com</strong>" in done.text


class TestAuthorizePage:
    def _page(self, pending: authprompt.PendingAuthorization, **kw: Any) -> str:
        return bytes(authpages.authorize_page(pending, **kw).body).decode()

    def test_names_provider_scopes_requesters_and_why(self) -> None:
        pending = _pending("gws")
        pending.url = (
            "https://accounts.google.com/o/oauth2/auth?state=s1&scope="
            "openid+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.modify"
        )
        connection, _ = recording_connection(url_elicitation=True)
        pending.note_requester(connection, "abcdef0123456789")
        pending.note_requester(connection, "abcdef0123456789")  # once only
        page = self._page(pending)
        assert "<dt>Signs in at</dt><dd>accounts.google.com</dd>" in page
        assert "openid<br>gmail.modify" in page
        assert page.count("<dt>Asked by</dt>") == 1
        assert "test" in page and "chat abcdef01…" in page
        assert "needs your permission to connect" in page
        pending.renewal = True
        assert "has expired or was revoked" in self._page(pending)

    def test_scripts_run_by_nonce_only(self) -> None:
        page = authpages.authorize_page(_pending("gws"))
        csp = page.headers["content-security-policy"]
        nonce = re.search(r"script-src 'nonce-([^']+)'", csp)
        assert nonce is not None and "unsafe-inline" not in csp.split("script-src", 1)[1]
        tags = re.findall(r"<script[^>]*>", bytes(page.body).decode())
        assert tags and all(f'nonce="{nonce.group(1)}"' in tag for tag in tags)
        again = authpages.authorize_page(_pending("gws")).headers["content-security-policy"]
        assert again != csp  # fresh per response

    def test_sign_in_variant_hides_the_way_on_and_the_details(self) -> None:
        pending = _pending("gws")
        pending.login_hint = "me@example.com"
        connection, _ = recording_connection(url_elicitation=True)
        pending.note_requester(connection, "abcdef0123456789")
        page = self._page(pending, needs_sign_in=True, sign_in_error="That password isn't right.")
        assert 'type="password"' in page and '<form method="post">' in page
        assert html.escape(pending.url, quote=True) not in page
        assert "isn&#x27;t right" in page
        # Only what the bare link already says: no account, requesters, scopes.
        for secret in ("me@example.com", "abcdef01", "Asked by", "Signs in at"):
            assert secret not in page
        assert "Authorize <code>gws</code>" in page


class TestAuthorizedPage:
    def test_names_the_account_the_provider_signed_in(self) -> None:
        page = bytes(authpages.authorized_page("gws", account="me@x.com").body).decode()
        assert "Signed in as <strong>me@x.com</strong>" in page
        assert "expected account" not in page

    def test_warns_when_a_different_account_signed_in(self) -> None:
        page = authpages.authorized_page("gws", account="other@x.com", login_hint="me@x.com")
        text = bytes(page.body).decode()
        assert "Signed in as <strong>other@x.com</strong>" in text
        assert "isn't the expected account" in text and "me@x.com" in text

    def test_falls_back_to_the_requested_account(self) -> None:
        text = bytes(authpages.authorized_page("gws", login_hint="me@x.com").body).decode()
        assert "Requested account: <strong>me@x.com</strong>" in text
        assert "Signed in as" not in text

    def test_says_nothing_about_accounts_without_either(self) -> None:
        text = bytes(authpages.authorized_page("gws").body).decode()
        assert "Signed in as" not in text and "Requested account" not in text
