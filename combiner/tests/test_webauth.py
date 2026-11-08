"""web_auth: browser login for the widget pages and relay (mcp_combiner.webauth,
mcp_combiner.webgate)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from conftest import CombinerHandle, ProcFactory, http_mock_entry
from fastmcp import Client

from mcp_combiner import webauth
from mcp_combiner.config import CombinerConfig
from mcp_combiner.runtime import CLIENT_CONNECT_MODE

PASSWORD = "correct horse battery staple"
WIDGET = "ui://mockup/mock/widget"


class TestPasswords:
    def test_round_trip_and_rejections(self) -> None:
        stored = webauth.hash_password(PASSWORD)
        assert stored.startswith("scrypt$16384$8$1$")
        assert webauth.verify_password(PASSWORD, stored)
        assert not webauth.verify_password("wrong", stored)
        assert not webauth.verify_password(PASSWORD, "scrypt$broken")
        assert not webauth.verify_password(PASSWORD, "md5$x$y$z$a$b")
        assert webauth.hash_password(PASSWORD) != stored  # salted


class TestSigner:
    def test_sessions_and_tickets(self, tmp_path: Path) -> None:
        signer = webauth.Signer.load_or_create(tmp_path / "web-auth.key")
        assert (tmp_path / "web-auth.key").stat().st_mode & 0o777 == 0o600
        session = signer.session(30)
        assert signer.verify(session, "s") is not None
        assert signer.verify(session, "t") is None  # kinds don't cross
        ticket = signer.ticket("chat-1")
        claims = signer.verify(ticket, "t")
        assert claims is not None and claims["sub"] == "chat-1"

    def test_tampered_expired_and_foreign_tokens_fail(self, tmp_path: Path) -> None:
        signer = webauth.Signer.load_or_create(tmp_path / "key")
        token = signer.session(30)
        payload, sig = token.split(".")
        assert signer.verify(f"{payload}x.{sig}", "s") is None
        expired = signer.sign({"v": 1, "k": "s", "iat": 0, "exp": int(time.time()) - 1})
        assert signer.verify(expired, "s") is None
        other = webauth.Signer.load_or_create(tmp_path / "other-key")
        assert other.verify(token, "s") is None
        assert signer.verify("garbage", "s") is None

    def test_the_key_persists(self, tmp_path: Path) -> None:
        token = webauth.Signer.load_or_create(tmp_path / "key").session(1)
        assert webauth.Signer.load_or_create(tmp_path / "key").verify(token, "s") is not None


class TestThisMachine:
    def test_loopback_and_own_addresses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(webauth, "_interface_addresses", lambda: {"192.168.1.20"})
        monkeypatch.setattr("mcp_combiner.tailnet.tailnet_ip", lambda: "100.66.1.2")
        webauth.reset_local_addresses()
        try:
            assert webauth.is_local("127.0.0.1") and webauth.is_local("::1")
            assert webauth.is_local("100.66.1.2") and webauth.is_local("192.168.1.20")
            assert webauth.is_local("::ffff:192.168.1.20")
            assert not webauth.is_local("100.66.9.9") and not webauth.is_local(None)
        finally:
            webauth.reset_local_addresses()


def _password_config(tmp_path: Path, **web_auth: object) -> Path:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"servers": {}, "web_auth": {"mode": "password", **web_auth}}))
    return path


def test_password_mode_needs_a_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_COMBINER_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("MCP_COMBINER_WEB_PASSWORD_HASH", raising=False)
    with pytest.raises(ValueError, match="MCP_COMBINER_WEB_PASSWORD"):
        CombinerConfig.load(str(_password_config(tmp_path)))


def test_a_raw_password_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_COMBINER_WEB_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("MCP_COMBINER_WEB_PASSWORD", PASSWORD)
    stored = CombinerConfig.load(str(_password_config(tmp_path))).web_auth.password_hash
    assert stored is not None and webauth.verify_password(PASSWORD, stored)
    assert not webauth.verify_password("wrong", stored)
    # A configured hash wins over the raw password.
    other = webauth.hash_password("the artifact")
    config = CombinerConfig.load(str(_password_config(tmp_path, password_hash=other)))
    assert config.web_auth.password_hash == other


# ── end to end ─────────────────────────────────────────────────────


async def _combiner(procs: ProcFactory, tmp_path: Path, *, exempt_local: bool) -> CombinerHandle:
    mock = await procs.start_http_mock("mockup")
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps(
            {
                "servers": {"mockup": http_mock_entry(mock.port)},
                "oauth": {"token_dir": str(tmp_path / "tokens")},
                "web_auth": {
                    "mode": "password",
                    "password_hash": webauth.hash_password(PASSWORD),
                    "exempt_local": exempt_local,
                },
            }
        )
    )
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


def _widget_page(combiner: CombinerHandle, token: str) -> str:
    return f"{combiner.base_url}/ui/{token}/?resource={WIDGET}"


HTML = {"accept": "text/html"}


@pytest.mark.e2e
class TestLogin:
    async def test_login_then_the_widget_page_and_relay(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        page = _widget_page(combiner, "web-login-0001")
        relay = f"http://127.0.0.1:{combiner.port + 1}/resource"
        async with httpx2.AsyncClient(timeout=10.0) as http:
            r = await http.get(page, headers=HTML)
            assert r.status_code == 303
            assert r.headers["location"].startswith("/auth/login?next=%2Fui%2Fweb-login-0001")
            assert (await http.get(page)).status_code == 401  # not a page request
            assert (await http.get(relay, params={"pt": "web-login-0001"})).status_code == 401

            login = await http.get(f"{combiner.base_url}{r.headers['location']}")
            assert login.status_code == 200 and 'type="password"' in login.text
            assert "form-action 'self'" in login.headers["content-security-policy"]

            bad = await http.post(
                f"{combiner.base_url}/auth/login", data={"password": "nope", "next": "/x"}
            )
            assert bad.status_code == 401 and "That password isn" in bad.text

            good = await http.post(
                f"{combiner.base_url}/auth/login",
                data={"password": PASSWORD, "next": "/ui/web-login-0001/"},
            )
            assert good.status_code == 303 and good.headers["location"] == "/ui/web-login-0001/"
            cookie = good.headers["set-cookie"]
            assert webauth.COOKIE in cookie and "HttpOnly" in cookie
            assert "samesite=lax" in cookie.lower()

            assert (await http.get(page, headers=HTML)).status_code == 200
            # Cookies are per host, not port: the relay accepts the same login.
            relayed = await http.get(relay, params={"resource": WIDGET, "pt": "web-login-0001"})
            assert relayed.status_code != 401

            await http.get(f"{combiner.base_url}/auth/logout")
            assert (await http.get(page, headers=HTML)).status_code == 303

    async def test_next_is_same_origin_only(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        async with httpx2.AsyncClient(timeout=10.0) as http:
            for evil in (
                "https://evil.example/",
                "//evil.example/",
                "/\\evil.example/",
                "javascript:x",
            ):
                r = await http.post(
                    f"{combiner.base_url}/auth/login", data={"password": PASSWORD, "next": evil}
                )
                assert r.headers["location"] == "/"

    async def test_failures_are_rate_limited(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        async with httpx2.AsyncClient(timeout=10.0) as http:
            codes = [
                (
                    await http.post(f"{combiner.base_url}/auth/login", data={"password": "no"})
                ).status_code
                for _ in range(6)
            ]
            assert codes[:5] == [401] * 5 and codes[5] == 429
            # Even the right password waits out the window.
            r = await http.post(f"{combiner.base_url}/auth/login", data={"password": PASSWORD})
            assert r.status_code == 429

    async def test_a_widget_url_from_an_mcp_client_logs_in_by_ticket(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        token = "web-ticket-0001"
        async with Client(f"{combiner.mcp_url}/{token}", mode=CLIENT_CONNECT_MODE) as client:
            result = await client.call_tool("open_widget", {"uri": WIDGET, "hold": False})
        text = " ".join(getattr(c, "text", "") for c in result.content)
        match = re.search(r"interactive: (\S+)", text)
        assert match is not None, text
        url = match.group(1)
        ticket = parse_qs(urlsplit(url).query)["t"][0]
        assert ticket
        async with httpx2.AsyncClient(timeout=10.0) as http:
            r = await http.get(url, headers=HTML)
            assert r.status_code == 303
            assert "t=" not in r.headers["location"]  # the clean URL
            assert webauth.COOKIE in r.headers["set-cookie"]
            assert (
                await http.get(f"{combiner.base_url}{r.headers['location']}", headers=HTML)
            ).status_code == 200

    async def test_a_ticket_for_another_chat_is_refused(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        signer = webauth.Signer.load_or_create(tmp_path / "web-auth.key")
        ticket = signer.ticket("chat-a-000001")
        async with httpx2.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                f"{combiner.base_url}/ui/chat-b-000001/?resource={WIDGET}&t={ticket}",
                headers=HTML,
            )
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")

    async def test_oauth_pages_and_health_stay_open(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        async with httpx2.AsyncClient(timeout=10.0) as http:
            assert (await http.get(f"{combiner.base_url}/health")).status_code == 200
            gone = await http.get(f"{combiner.base_url}/oauth/authorize/{'0' * 32}", headers=HTML)
            assert gone.status_code == 404 and "expired or was already used" in gone.text

    async def test_this_machine_is_exempt(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=True)
        async with httpx2.AsyncClient(timeout=10.0) as http:
            r = await http.get(_widget_page(combiner, "web-local-0001"), headers=HTML)
        assert r.status_code == 200
