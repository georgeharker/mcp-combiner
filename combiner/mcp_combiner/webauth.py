"""Browser login for the combiner's web surface (widget pages and relay).

Browsers can't send the inbound bearer, so with ``web_auth.mode: password``
the widget pages (``/ui/<token>/...``) and the widget relay (port + 1) ask for
a password once per ``session_days`` per browser, then ride a session cookie.
Three ways through, checked in order:

- a request from this machine itself (any of its own addresses: loopback,
  and the tailnet/LAN addresses widget URLs name) — exempt;
- a valid session cookie (``mcpc_session``);
- a valid ticket (``?t=``) on a widget URL the combiner handed a
  bearer-authenticated MCP client: exchanged for the cookie, then a redirect
  to the clean URL — so links opened from a client's card never prompt.

Otherwise: a redirect to ``/auth/login`` (pages) or 401 (everything else).
``/oauth/*`` and ``/health`` are never gated. Design decision 01M6WE1DJS.

Token format (shared with the toucan host, which mirrors it):
``<payload_b64url>.<sig_b64url>``, payload compact JSON, sig HMAC-SHA256 over
the payload part. Session: ``{"v":1,"k":"s","iat":..,"exp":..}``; ticket:
``{"v":1,"k":"t","sub":<chat token>,"j":<random id>,"exp":..}`` — redeemable
``web_auth.ticket_uses`` times (counted in memory by ``j``). Passwords:
``scrypt$<n>$<r>$<p>$<salt>$<hash>`` (base64url, unpadded).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

logger = logging.getLogger("mcp-combiner")

COOKIE = "mcpc_session"
TICKET_PARAM = "t"
TICKET_TTL_S = 300
LOGIN_PATH = "/auth/login"
LOGOUT_PATH = "/auth/logout"

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

#: The raw password env (``op run`` style). Read once at serve startup and
#: removed from the environment (:func:`adopt_password_env`), so children —
#: external servers, stdio upstreams — never inherit it.
PASSWORD_ENV = "MCP_COMBINER_WEB_PASSWORD"


# ── passwords ──────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """The stored form of *password* (``web_auth.password_hash``)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_adopted_hash: str | None = None


def adopt_password_env() -> None:
    """Take ``$MCP_COMBINER_WEB_PASSWORD`` out of the environment, keeping only
    its hash (for config loads and reloads)."""
    global _adopted_hash
    raw = os.environ.pop(PASSWORD_ENV, None)
    if raw:
        _adopted_hash = hash_password(raw)


def env_password_hash() -> str | None:
    """A hash of the raw password from the environment, if one was given:
    still in the environment (embedded/tests), or adopted at startup."""
    raw = os.environ.get(PASSWORD_ENV)
    return hash_password(raw) if raw else _adopted_hash


def reset_adopted_password() -> None:
    global _adopted_hash
    _adopted_hash = None


# ── signed tokens (session cookie, tickets) ────────────────────────


class KeyFileError(Exception):
    """The signing key file exists but can't be trusted (owner, mode, symlink)."""


class Signer:
    """HMAC-SHA256 tokens under one persisted key (rotate it = log everyone out)."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    @classmethod
    def load_or_create(cls, path: Path) -> Signer:
        """The key at *path*, created (0600, atomically) when missing.

        An existing key must be a regular file owned by this user and not
        readable by anyone else — anyone who can read or plant it can mint
        sessions. Creation writes a temp file and hard-links it into place,
        so a reader never sees a half-written key and two first starts agree
        on one key.
        """
        try:
            return cls(_read_key(path))
        except FileNotFoundError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".web-auth-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(secrets.token_bytes(32))
            try:
                os.link(tmp, path)
                logger.info("web_auth: created a signing key at %s", path)
            except FileExistsError:
                pass  # another process won the race: use its key
        finally:
            os.unlink(tmp)
        return cls(_read_key(path))

    def sign(self, claims: dict[str, Any]) -> str:
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        sig = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        return f"{payload}.{_b64(sig)}"

    def verify(self, token: str, kind: str) -> dict[str, Any] | None:
        """The claims of a valid, unexpired *token* of *kind*, else ``None``."""
        payload, _, sig = token.partition(".")
        if not payload or not sig:
            return None
        expected = hmac.new(self._key, payload.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_unb64(sig), expected):
                return None
            claims = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(claims, dict) or claims.get("v") != 1 or claims.get("k") != kind:
            return None
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp < time.time():
            return None
        return claims

    def session(self, days: float) -> str:
        now = int(time.time())
        return self.sign({"v": 1, "k": "s", "iat": now, "exp": now + int(days * 86400)})

    def ticket(self, chat_token: str, ttl_s: int = TICKET_TTL_S) -> str:
        return self.sign(
            {
                "v": 1,
                "k": "t",
                "sub": chat_token,
                "j": secrets.token_urlsafe(12),
                "exp": int(time.time()) + ttl_s,
            }
        )


def _read_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:  # ELOOP: a symlink
        raise KeyFileError(f"web_auth key {path} can't be opened safely: {exc}") from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        getuid = getattr(os, "getuid", None)
        if getuid is not None and st.st_uid != getuid():
            raise KeyFileError(f"web_auth key {path} is owned by another user; delete it")
        if st.st_mode & 0o077:
            raise KeyFileError(
                f"web_auth key {path} is readable by others; `chmod 600 {path}` "
                "(or delete it, which signs every browser out)"
            )
        key = fh.read()
    if len(key) < 32:
        raise KeyFileError(f"web_auth key {path} is truncated; delete it to make a new one")
    return key


class TicketLedger:
    """How many times each ticket (by its ``j``) was redeemed, until it expires."""

    def __init__(self) -> None:
        self._uses: dict[str, tuple[int, float]] = {}

    def redeem(self, claims: dict[str, Any], allowed: int) -> bool:
        jti = claims.get("j")
        exp = claims.get("exp")
        if not isinstance(jti, str) or not jti or not isinstance(exp, (int, float)):
            return False
        now = time.time()
        if exp < now:
            # Signer.verify already refuses these; checked here too so the
            # ledger alone never re-admits a ticket whose entry was pruned.
            return False
        for key in [k for k, (_, e) in self._uses.items() if e < now]:
            del self._uses[key]
        used, _ = self._uses.get(jti, (0, exp))
        if used >= allowed:
            return False
        self._uses[jti] = (used + 1, float(exp))
        return True

    def clear(self) -> None:
        self._uses.clear()


TICKETS = TicketLedger()


# ── "this machine" ─────────────────────────────────────────────────

#: This machine's non-loopback addresses (interfaces, tailnet). Read without a
#: lock on the request path; replaced whole by :func:`refresh_local_addresses`
#: off it. Empty until the first refresh: only loopback counts as local then.
_local: frozenset[str] = frozenset()
_refresh_stop = threading.Event()
_REFRESH_INTERVAL_S = 60.0


def local_addresses() -> frozenset[str]:
    return _local


def refresh_local_addresses() -> frozenset[str]:
    """Look the addresses up now (blocking: runs ``ifconfig``/``ip`` and
    ``tailscale``) and publish them."""
    global _local
    found = set(_interface_addresses())
    from mcp_combiner.tailnet import fresh_tailnet_ip

    ip = fresh_tailnet_ip()
    if ip:
        found.add(ip)
    _local = frozenset(_canonical(a) for a in found)
    return _local


def start_local_refresh(interval_s: float = _REFRESH_INTERVAL_S) -> None:
    """Refresh the addresses now and every *interval_s*, in a daemon thread —
    a laptop's addresses change as it roams."""
    _refresh_stop.clear()

    def loop() -> None:
        while True:
            try:
                refresh_local_addresses()
            except Exception:  # noqa: BLE001 — keep the last good set
                logger.debug("web_auth: address refresh failed", exc_info=True)
            if _refresh_stop.wait(interval_s):
                return

    threading.Thread(target=loop, name="web-auth-addresses", daemon=True).start()


def stop_local_refresh() -> None:
    _refresh_stop.set()


def reset_local_addresses() -> None:
    global _local
    _local = frozenset()


def is_local(client_host: str | None) -> bool:
    if not client_host:
        return False
    canonical = _canonical(client_host)
    try:
        if ipaddress.ip_address(canonical).is_loopback:
            return True
    except ValueError:
        return False
    return canonical in _local


def _canonical(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:a.b.c.d is a.b.c.d
    return str(mapped or ip)


def _interface_addresses() -> set[str]:
    """``ifconfig -a``, else (Linux without net-tools) ``ip -o addr``."""
    for argv in (["ifconfig", "-a"], ["ip", "-o", "addr", "show"]):
        exe = shutil.which(argv[0])
        if exe is None:
            continue
        try:
            out = subprocess.run(
                [exe, *argv[1:]], capture_output=True, text=True, timeout=2.0, check=False
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        return parse_addresses(out)
    return set()


def parse_addresses(text: str) -> set[str]:
    """Addresses from ``ifconfig`` (``inet 1.2.3.4 …``, ``inet addr:1.2.3.4``)
    or ``ip -o addr`` (``2: eth0 inet 1.2.3.4/24 …``) output."""
    found: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        for i, word in enumerate(parts[:-1]):
            if word in ("inet", "inet6"):
                found.add(parts[i + 1].removeprefix("addr:").split("/", 1)[0])
                break
    return found


# ── login rate limit ───────────────────────────────────────────────


class LoginLimiter:
    """Failed-login budgets: per client address and across all of them.

    :meth:`begin` reserves an attempt synchronously — before the caller awaits
    anything — so concurrent attempts count against the budget immediately
    rather than after their (slow) password checks finish. The global budget
    stops guessing spread over many addresses, at the price of locking the
    password form for everyone during such an attack (this machine and ticket
    links still get in). Bounded: at most ``max_clients`` addresses tracked,
    empty windows dropped.
    """

    def __init__(
        self,
        per_client: int = 5,
        overall: int = 30,
        window_s: float = 60.0,
        max_clients: int = 10_000,
    ) -> None:
        self.per_client = per_client
        self.overall = overall
        self.window_s = window_s
        self.max_clients = max_clients
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._all: deque[float] = deque()
        self._in_flight: dict[str, int] = {}

    def begin(self, client: str) -> bool:
        """Reserve an attempt for *client*; ``False`` when over budget."""
        now = time.monotonic()
        self._prune(now)
        window = self._failures.get(client)
        if window is not None:
            cutoff = now - self.window_s
            while window and window[0] < cutoff:
                window.popleft()
        mine = len(window or ()) + self._in_flight.get(client, 0)
        everyone = len(self._all) + sum(self._in_flight.values())
        if mine >= self.per_client or everyone >= self.overall:
            return False
        self._in_flight[client] = self._in_flight.get(client, 0) + 1
        return True

    def end(self, client: str, *, failed: bool) -> None:
        left = self._in_flight.get(client, 0) - 1
        if left > 0:
            self._in_flight[client] = left
        else:
            self._in_flight.pop(client, None)
        if not failed:
            return
        now = time.monotonic()
        window = self._failures.pop(client, None) or deque()
        window.append(now)
        self._failures[client] = window  # most recent last
        self._all.append(now)
        while len(self._failures) > self.max_clients:
            self._failures.popitem(last=False)

    def tracked(self) -> int:
        return len(self._failures) + len(self._in_flight)

    def _prune(self, now: float) -> None:
        """Drop expired failures. Clients are ordered by their latest failure,
        so whole stale clients come off the front."""
        cutoff = now - self.window_s
        while self._all and self._all[0] < cutoff:
            self._all.popleft()
        while self._failures:
            client, window = next(iter(self._failures.items()))
            if window and window[-1] >= cutoff:
                break
            del self._failures[client]

    def reset(self) -> None:
        self._failures.clear()
        self._all.clear()
        self._in_flight.clear()


LIMITER = LoginLimiter()
