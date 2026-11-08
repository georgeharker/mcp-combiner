"""web_auth: browser login for the widget pages and relay (mcp_combiner.webauth,
mcp_combiner.webgate)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from conftest import CombinerHandle, ProcFactory, http_mock_entry
from fastmcp import Client

from mcp_combiner import webauth
from mcp_combiner.__main__ import relay_config
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

    def test_tickets_carry_a_unique_id(self, tmp_path: Path) -> None:
        signer = webauth.Signer.load_or_create(tmp_path / "key")
        first, second = (signer.verify(signer.ticket("c"), "t") for _ in range(2))
        assert first is not None and second is not None and first["j"] != second["j"]


class TestKeyFile:
    def test_refuses_a_key_others_can_read(self, tmp_path: Path) -> None:
        path = tmp_path / "key"
        webauth.Signer.load_or_create(path)
        path.chmod(0o644)
        with pytest.raises(webauth.KeyFileError, match="chmod 600"):
            webauth.Signer.load_or_create(path)

    def test_refuses_a_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        webauth.Signer.load_or_create(real)
        (tmp_path / "link").symlink_to(real)
        with pytest.raises(webauth.KeyFileError):
            webauth.Signer.load_or_create(tmp_path / "link")

    def test_refuses_a_truncated_key(self, tmp_path: Path) -> None:
        path = tmp_path / "key"
        path.write_bytes(b"short")
        path.chmod(0o600)
        with pytest.raises(webauth.KeyFileError, match="truncated"):
            webauth.Signer.load_or_create(path)

    def test_creation_leaves_no_temp_files(self, tmp_path: Path) -> None:
        webauth.Signer.load_or_create(tmp_path / "key")
        assert [p.name for p in tmp_path.iterdir()] == ["key"]


class TestTicketLedger:
    def test_uses_are_counted_per_ticket(self) -> None:
        ledger = webauth.TicketLedger()
        exp = time.time() + 60
        one = {"j": "a", "exp": exp}
        assert ledger.redeem(one, 1) and not ledger.redeem(one, 1)
        two = {"j": "b", "exp": exp}
        assert ledger.redeem(two, 2) and ledger.redeem(two, 2) and not ledger.redeem(two, 2)
        assert not ledger.redeem({"exp": exp}, 5)  # no id: never redeemable

    def test_expired_entries_are_dropped(self) -> None:
        ledger = webauth.TicketLedger()
        ledger.redeem({"j": "old", "exp": time.time() - 1}, 1)
        ledger.redeem({"j": "new", "exp": time.time() + 60}, 1)
        assert list(ledger._uses) == ["new"]
        assert not ledger.redeem({"j": "old", "exp": time.time() - 1}, 1)  # pruned, still refused


class TestLimiter:
    def test_concurrent_attempts_count_before_they_finish(self) -> None:
        limiter = webauth.LoginLimiter(per_client=5, overall=100)
        assert all(limiter.begin("1.2.3.4") for _ in range(5))
        assert not limiter.begin("1.2.3.4")  # five still in flight
        assert limiter.begin("5.6.7.8")  # another address has its own budget

    def test_successes_free_the_slot_failures_keep_it(self) -> None:
        limiter = webauth.LoginLimiter(per_client=2, overall=100)
        for _ in range(3):
            assert limiter.begin("a")
            limiter.end("a", failed=False)
        limiter.begin("a")
        limiter.end("a", failed=True)
        limiter.begin("a")
        limiter.end("a", failed=True)
        assert not limiter.begin("a")

    def test_the_overall_budget_spans_addresses(self) -> None:
        limiter = webauth.LoginLimiter(per_client=5, overall=3)
        for client in ("a", "b", "c"):
            assert limiter.begin(client)
            limiter.end(client, failed=True)
        assert not limiter.begin("d")

    def test_bounded_and_pruned(self) -> None:
        limiter = webauth.LoginLimiter(per_client=5, overall=10**6, max_clients=3, window_s=60)
        for i in range(10):
            limiter.begin(f"10.0.0.{i}")
            limiter.end(f"10.0.0.{i}", failed=True)
        assert limiter.tracked() == 3  # oldest evicted
        limiter.window_s = 0.0
        time.sleep(0.01)
        limiter.begin("x")
        limiter.end("x", failed=False)
        assert limiter.tracked() == 0  # stale windows dropped, success adds nothing


class TestThisMachine:
    def test_loopback_and_own_addresses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(webauth, "_interface_addresses", lambda: {"192.168.1.20"})
        monkeypatch.setattr("mcp_combiner.tailnet.fresh_tailnet_info", lambda: (None, "100.66.1.2"))
        webauth.reset_local_addresses()
        try:
            # Before the first refresh only loopback counts (fail closed).
            assert webauth.is_local("127.0.0.1") and not webauth.is_local("192.168.1.20")
            webauth.refresh_local_addresses()
            assert webauth.is_local("127.0.0.1") and webauth.is_local("::1")
            assert webauth.is_local("100.66.1.2") and webauth.is_local("192.168.1.20")
            assert webauth.is_local("::ffff:192.168.1.20")
            assert not webauth.is_local("100.66.9.9") and not webauth.is_local(None)
            # A refresh follows the machine as its addresses change.
            monkeypatch.setattr(webauth, "_interface_addresses", lambda: {"10.1.1.1"})
            webauth.refresh_local_addresses()
            assert webauth.is_local("10.1.1.1") and not webauth.is_local("192.168.1.20")
        finally:
            webauth.reset_local_addresses()

    def test_the_host_header_must_name_this_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(webauth, "_interface_addresses", lambda: {"192.168.1.20"})
        monkeypatch.setattr(
            "mcp_combiner.tailnet.fresh_tailnet_info", lambda: ("box.tail.ts.net.", "100.66.1.2")
        )
        webauth.reset_local_addresses()
        try:
            webauth.set_local_names({"combiner.example.com"})
            webauth.refresh_local_addresses()
            mine: tuple[str, ...] = (
                "localhost:9741",
                "127.0.0.1:9741",
                "[::1]:9741",
                "192.168.1.20:9741",
                "100.66.1.2",
                "Box.Tail.TS.net:9741",
                "combiner.example.com",
            )
            strangers: tuple[str | None, ...] = (
                "attacker.example:9741",
                "evil.localhost.example",
                "10.9.9.9",
                "",
                None,
            )
            assert [h for h in mine if not webauth.is_local_host(h)] == []
            assert [h for h in strangers if webauth.is_local_host(h)] == []
            # Names configured after a refresh count at once, and survive the next one.
            webauth.set_local_names({"later.example"})
            assert webauth.is_local_host("later.example:443")
            webauth.refresh_local_addresses()
            assert webauth.is_local_host("later.example")
        finally:
            webauth.reset_local_addresses()

    def test_parses_ifconfig_and_ip_output(self) -> None:
        mac = (
            "en0: flags=8863<UP>\n\tinet 192.168.1.5 netmask 0xffffff00\n"
            "\tinet6 fe80::1%en0 prefixlen 64\n"
        )
        old_linux = "eth0  Link encap:Ethernet\n      inet addr:10.0.0.7  Bcast:10.0.0.255\n"
        ip_o = (
            "2: eth0    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0\n"
            "3: eth0    inet6 fd00::2/64 scope global\n"
        )
        assert webauth.parse_addresses(mac) == {"192.168.1.5", "fe80::1%en0"}
        assert webauth.parse_addresses(old_linux) == {"10.0.0.7"}
        assert webauth.parse_addresses(ip_o) == {"172.17.0.2", "fd00::2"}


class TestPasswordEnv:
    def test_adopted_and_removed_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(webauth.PASSWORD_ENV, PASSWORD)
        try:
            webauth.adopt_password_env()
            assert webauth.PASSWORD_ENV not in os.environ  # children won't inherit it
            stored = webauth.env_password_hash()
            assert stored is not None and webauth.verify_password(PASSWORD, stored)
        finally:
            webauth.reset_adopted_password()


def _password_config(tmp_path: Path, **web_auth: object) -> Path:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"servers": {}, "web_auth": {"mode": "password", **web_auth}}))
    return path


@pytest.mark.parametrize(
    ("web_auth", "message"),
    [
        ("password", "must be an object"),
        (
            {"mode": "password", "password_hash": "scrypt$x", "exempt_local": "false"},
            "true or false",
        ),
        ({"mode": "password", "password_hash": "scrypt$x", "tickets": 1}, "true or false"),
        ({"mode": "off", "exempt_locall": False}, "unknown setting"),
        ({"mode": "password", "password_hash": "scrypt$x", "session_days": "30"}, "a number"),
        ({"mode": "password", "password_hash": "scrypt$x", "session_days": 0}, "positive"),
        ({"mode": "password", "password_hash": "scrypt$x", "ticket_uses": 0}, "whole number"),
        ({"mode": "password", "password_hash": "scrypt$x", "ticket_uses": True}, "whole number"),
    ],
)
def test_bad_settings_fail_the_load(tmp_path: Path, web_auth: object, message: str) -> None:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"servers": {}, "web_auth": web_auth}))
    with pytest.raises(ValueError, match=message):
        CombinerConfig.load(str(path))


class TestStatusPage:
    @staticmethod
    def _page(*, signed_in: bool = False) -> str:
        from mcp_combiner import authpages

        rows = [
            authpages.StatusRow("github", "ready", tools=42),
            authpages.StatusRow("gws", "auth_failed", authorize_path="/oauth/authorize/abc"),
            authpages.StatusRow("<b>odd</b>", "unreachable"),
            authpages.StatusRow("playwright", "disabled"),
        ]
        page = authpages.status_page(rows, version="0.22.1", uptime_s=7500.0, signed_in=signed_in)
        return bytes(page.body).decode()

    def test_states_counts_and_links(self) -> None:
        page = self._page()
        assert "1 of 3 servers up" in page  # disabled servers aren't counted
        assert "Version 0.22.1 · up 2 h 5 min" in page
        assert '<span class="dot ok"></span><span class="name">github</span>' in page
        assert "ready · 42 tools" in page
        assert '<a href="/oauth/authorize/abc">Authorize</a>' in page
        assert '<span class="dot bad"></span>' in page and '<span class="dot off"></span>' in page
        assert "<b>odd</b>" not in page and "&lt;b&gt;odd&lt;/b&gt;" in page
        # disabled servers sort last
        assert page.index("playwright") > page.index("odd")
        assert "Sign out" not in page
        assert "Sign out" in self._page(signed_in=True)


def test_the_relay_gets_the_same_tls(tmp_path: Path) -> None:
    import argparse

    args = argparse.Namespace(ssl_certfile="/c.pem", ssl_keyfile="/k.pem")
    config = relay_config(object(), args)
    assert (config.ssl_certfile, config.ssl_keyfile) == ("/c.pem", "/k.pem")
    assert config.proxy_headers and config.forwarded_allow_ips == "127.0.0.1,::1"


def test_unset_auth_variables_are_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from mcp_combiner.config import interpolate_value

    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with caplog.at_level("WARNING", logger="mcp-combiner"):
        assert interpolate_value("Bearer ${NOT_SET_ANYWHERE}") == "Bearer "
        assert interpolate_value("${NOT_SET_ANYWHERE:-x}") == "x"  # a default: quiet
    assert [r.getMessage() for r in caplog.records] == [
        "auth setting refers to ${NOT_SET_ANYWHERE}, which is not set"
    ]


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


async def _combiner_without_web_auth(procs: ProcFactory, tmp_path: Path) -> CombinerHandle:
    mock = await procs.start_http_mock("mockup")
    config = tmp_path / "servers.json"
    config.write_text(
        json.dumps(
            {
                "servers": {"mockup": http_mock_entry(mock.port)},
                "oauth": {"token_dir": str(tmp_path / "tokens")},
            }
        )
    )
    combiner = await procs.start_combiner(config)
    await combiner.wait_server_state("mockup", ("ready",))
    return combiner


REBOUND = {"host": "attacker.example:9741"}
PROXIED = {"x-forwarded-for": "100.64.9.9"}


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

            # Sign-out is a POST; a GET only shows the button.
            shown = await http.get(f"{combiner.base_url}/auth/logout")
            assert shown.status_code == 200 and 'method="post"' in shown.text
            assert (await http.get(page, headers=HTML)).status_code == 200
            await http.post(f"{combiner.base_url}/auth/logout")
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

    async def test_concurrent_guesses_are_limited_and_dont_stall_the_server(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Guesses fired at once count before any finishes; and the password
        checks run off the event loop, so /health stays quick meanwhile."""
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        url = f"{combiner.base_url}/auth/login"
        async with httpx2.AsyncClient(timeout=30.0) as http:

            async def guess(source: str) -> int:
                headers = {"x-forwarded-for": source}
                return (await http.post(url, data={"password": "no"}, headers=headers)).status_code

            async def health_times() -> list[float]:
                times = []
                for _ in range(10):
                    start = time.monotonic()
                    await http.get(f"{combiner.base_url}/health")
                    times.append(time.monotonic() - start)
                return times

            same = await asyncio.gather(*(guess("100.64.1.1") for _ in range(20)))
            assert same.count(401) <= 5 and same.count(429) >= 15
            # 24 addresses x 1 guess: under the overall budget, all scrypt'd.
            many, times = await asyncio.gather(
                asyncio.gather(*(guess(f"100.64.2.{i}") for i in range(24))), health_times()
            )
            assert many == [401] * 24
            assert max(times) < 0.3, times

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
        # Used up (ticket_uses defaults to 1): a second browser gets the login.
        async with httpx2.AsyncClient(timeout=10.0) as http:
            again = await http.get(url, headers=HTML)
            assert again.status_code == 303
            assert again.headers["location"].startswith("/auth/login")

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

    async def test_a_rebinding_page_is_not_this_machine(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        """Loopback, but Host names a stranger's domain: a DNS-rebinding page."""
        combiner = await _combiner(procs, tmp_path, exempt_local=True)
        async with httpx2.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                _widget_page(combiner, "web-rebind-001"), headers={**HTML, **REBOUND}
            )
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")

    async def test_this_machine_is_exempt(self, procs: ProcFactory, tmp_path: Path) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=True)
        async with httpx2.AsyncClient(timeout=10.0) as http:
            r = await http.get(_widget_page(combiner, "web-local-0001"), headers=HTML)
        assert r.status_code == 200


@pytest.mark.e2e
class TestStatus:
    async def test_without_web_auth_only_this_machine_sees_it(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner_without_web_auth(procs, tmp_path)
        url = f"{combiner.base_url}/status"
        async with httpx2.AsyncClient(timeout=10.0) as http:
            page = await http.get(url)
            assert page.status_code == 200
            assert '<span class="name">mockup</span>' in page.text
            assert "servers up" in page.text and "Sign out" not in page.text
            assert page.headers["cache-control"] == "no-store"
            assert (await http.get(url, headers=REBOUND)).status_code == 404
            assert (await http.get(url, headers=PROXIED)).status_code == 404
            assert (await http.post(url, data={"password": "x"})).status_code == 404

    async def test_with_web_auth_sign_in_on_the_page(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=False)
        url = f"{combiner.base_url}/status"
        async with httpx2.AsyncClient(timeout=10.0) as http:
            locked = await http.get(url)
            assert locked.status_code == 401 and 'type="password"' in locked.text
            assert "mockup" not in locked.text
            wrong = await http.post(url, data={"action": "sign-in", "password": "no"})
            assert wrong.status_code == 401 and "That password isn" in wrong.text
            right = await http.post(url, data={"action": "sign-in", "password": PASSWORD})
            assert right.status_code == 303 and webauth.COOKIE in right.headers["set-cookie"]
            page = await http.get(url)
            assert page.status_code == 200 and "mockup" in page.text and "Sign out" in page.text
            out = await http.post(url, data={"action": "sign-out"})
            assert out.status_code == 303
            assert (await http.get(url)).status_code == 401

    async def test_with_web_auth_this_machine_is_exempt_but_not_strangers(
        self, procs: ProcFactory, tmp_path: Path
    ) -> None:
        combiner = await _combiner(procs, tmp_path, exempt_local=True)
        url = f"{combiner.base_url}/status"
        async with httpx2.AsyncClient(timeout=10.0) as http:
            mine = await http.get(url)
            assert mine.status_code == 200 and "Sign out" not in mine.text
            assert (await http.get(url, headers=REBOUND)).status_code == 401
            assert (await http.get(url, headers=PROXIED)).status_code == 401
