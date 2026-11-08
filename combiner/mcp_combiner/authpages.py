"""The pages a person sees during an out-of-band OAuth authorization.

Opened on whatever device the authorization link reached — often a phone, via
a client's card — so they are self-contained (inline CSS and SVG, no external
fetches: the page may load inside a tailnet with no internet) and sized for a
mobile viewport. Three of them:

- the authorize page at ``/oauth/authorize/<id>`` (the short link): which
  server, what happens next, an Authorize button that continues to the
  provider, and a countdown to the link's expiry;
- the gone page: the link expired, was used, or never existed;
- the result page after the provider redirects back to the combiner
  (``/oauth/callback/<server>``): authorized, denied, or failed.
"""

from __future__ import annotations

import html
import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from starlette.responses import HTMLResponse

if TYPE_CHECKING:
    from mcp_combiner.authprompt import PendingAuthorization

#: The combiner mark: several inputs converging on one hub, one output — what
#: the combiner does with MCP servers. Inline, coloured by the page.
#: The hub of docs/assets/logo.svg (the full logo is too wide for a page header).
LOGO = """<svg class="mark" viewBox="258 98 84 84" role="img" aria-label="mcp-combiner">
  <defs>
    <linearGradient id="hubGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3A3B47"/><stop offset="1" stop-color="#141519"/>
    </linearGradient>
    <linearGradient id="hubBezel" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.55"/>
      <stop offset="0.4" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
  </defs>
  <path d="M284,100 H316 A24,24 0 0 1 340,124 V156 A24,24 0 0 1 316,180 H284
    A24,24 0 0 1 260,156 V124 A24,24 0 0 1 284,100 Z" fill="url(#hubGrad)"/>
  <ellipse cx="300" cy="120" rx="32" ry="13" fill="#ffffff" opacity="0.10"/>
  <path d="M284,100 H316 A24,24 0 0 1 340,124 V156 A24,24 0 0 1 316,180 H284
    A24,24 0 0 1 260,156 V124 A24,24 0 0 1 284,100 Z" fill="none"
    stroke="url(#hubBezel)" stroke-width="2"/>
  <circle cx="300" cy="140" r="15" fill="none" stroke="#ffffff" stroke-width="3"/>
  <circle cx="300" cy="140" r="5" fill="#ffffff"/>
</svg>"""

_STYLE = """
:root {
  --bg: #f4f5f7; --card: #ffffff; --text: #1d2330; --muted: #5d6676;
  --accent: #8636c3; --on-accent: #ffffff; --line: #e3e6ec;
  --ok: #2b8a3e; --warn: #c2410c; --bad: #c92a2a; --off: #adb5bd;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #11141a; --card: #1b2029; --text: #e8ebf1; --muted: #9aa3b2;
    --accent: #b77ff0; --on-accent: #170b24; --line: #2b313c;
    --ok: #51cf66; --warn: #ff922b; --bad: #ff6b6b; --off: #5c6370;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 24px 16px; background: var(--bg); color: var(--text);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
    sans-serif;
}
.card {
  width: 100%; max-width: 420px; background: var(--card); border: 1px solid var(--line);
  border-radius: 20px; padding: 28px 24px; box-shadow: 0 8px 30px rgba(0,0,0,.08);
  overflow-wrap: anywhere;
}
.brand { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }
.mark { width: 44px; height: 44px; flex: none; }
.wordmark { font-weight: 650; letter-spacing: -.01em; font-size: 17px; }
.wordmark span { color: var(--muted); font-weight: 500; }
h1 { font-size: 22px; line-height: 1.25; margin: 0 0 10px; letter-spacing: -.01em; }
p { margin: 0 0 14px; color: var(--muted); }
code {
  font: 600 .95em ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--text);
  background: var(--bg); padding: 1px 6px; border-radius: 6px;
}
.button {
  display: block; width: 100%; margin: 22px 0 10px; padding: 14px 18px; border-radius: 12px;
  background: var(--accent); color: var(--on-accent); text-align: center;
  text-decoration: none; font-weight: 650; font-size: 17px;
}
.button[aria-disabled="true"] { opacity: .45; pointer-events: none; }
button.button { border: 0; cursor: pointer; font: inherit; font-weight: 650; }
.field {
  display: block; width: 100%; margin-top: 18px; padding: 13px 14px; border-radius: 12px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); font: inherit;
}
.details {
  display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; margin: 16px 0 0;
  padding: 12px 14px; border-radius: 12px; background: var(--bg); font-size: 14px;
}
.details dt { color: var(--muted); }
.details dd { margin: 0; overflow-wrap: anywhere; }
.dim { color: var(--muted); }
.meta { font-size: 14px; color: var(--muted); text-align: center; margin: 0; }
.warning { color: var(--warn); margin: 12px 0 0; }
.account {
  color: var(--text); background: var(--bg); border: 1px solid var(--line);
  border-radius: 12px; padding: 10px 14px; margin: 0;
}
.status {
  display: inline-flex; align-items: center; gap: 8px; font-weight: 600; margin-bottom: 10px;
}
.status.ok { color: var(--ok); }
.status.warn { color: var(--warn); }
.dot { width: 10px; height: 10px; border-radius: 50%; background: currentColor; }
.servers { list-style: none; margin: 16px 0 0; padding: 0; }
.servers li {
  display: grid; grid-template-columns: 10px 1fr auto; align-items: center; gap: 10px;
  padding: 9px 0; border-top: 1px solid var(--line); font-size: 15px;
}
.servers li:first-child { border-top: 0; }
.servers .ok { color: var(--ok); } .servers .warn { color: var(--warn); }
.servers .bad { color: var(--bad); } .servers .off { color: var(--off); }
.servers .name { color: var(--text); font-weight: 600; }
.servers .state { color: var(--muted); font-size: 13px; text-align: right; }
.servers a { color: var(--accent); font-weight: 600; }
.summary { color: var(--text); font-weight: 600; margin: 0 0 4px; }
button.link {
  background: none; border: 0; padding: 0; color: var(--accent); font: inherit;
  font-size: 14px; cursor: pointer; text-decoration: underline;
}
"""


def _page(title: str, body: str, status: int = 200, script: str = "") -> HTMLResponse:
    # Scripts run under a per-response nonce, so nothing injected inline could.
    nonce = secrets.token_urlsafe(16)
    script = script.replace("<script>", f'<script nonce="{nonce}">')
    document = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name=robots content="noindex,nofollow">'
        f"<title>{html.escape(title)} · mcp-combiner</title><style>{_STYLE}</style></head>"
        '<body><main class="card">'
        f'<div class="brand">{LOGO}<div class="wordmark">mcp<span>-combiner</span></div></div>'
        f"{body}</main>{script}</body></html>"
    )
    # Pages carrying an authorization URL must not be cached or framed.
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
            "img-src data:; form-action 'self'; frame-ancestors 'none'"
        ),
    }
    return HTMLResponse(document, status_code=status, headers=headers)


def authorize_page(
    pending: PendingAuthorization, *, sign_in_error: str | None = None, needs_sign_in: bool = False
) -> HTMLResponse:
    """The short link's page: which server, why, who asked, where you're sent
    and what it asks for — then the way on. With web_auth and no session, the
    way on is a password form posting back here (this page may be the only
    combiner path a proxy exposes)."""
    name = html.escape(pending.server)
    seconds = max(0, math.floor(pending.expires_at - time.monotonic()))
    # Locked (no session yet): only what the bare link already says — the
    # account, scopes, requesters and provider wait for the sign-in.
    account = (
        f'<p class="account">Sign in as <strong>{html.escape(pending.login_hint)}</strong></p>'
        if pending.login_hint and not needs_sign_in
        else ""
    )
    details = (
        '<p class="meta">Sign in to see what is being requested.</p>'
        if needs_sign_in
        else _details(pending)
    )
    why = (
        f"Your saved sign-in for <strong>{name}</strong> has expired or was revoked, so the "
        "combiner needs your permission again."
        if pending.renewal
        else f"Your MCP combiner needs your permission to connect to <strong>{name}</strong>."
    )
    if needs_sign_in:
        problem = (
            f'<div class="status warn"><span class="dot"></span>{html.escape(sign_in_error)}</div>'
            if sign_in_error
            else ""
        )
        way_on = (
            f"{problem}"
            '<form method="post"><input class="field" type="password" name="password" '
            'autocomplete="current-password" placeholder="Combiner web password" required '
            'autofocus aria-label="Combiner web password">'
            '<button class="button" id="go" type="submit">Sign in to continue</button></form>'
        )
    else:
        way_on = (
            f'<a class="button" id="go" href="{html.escape(pending.url, quote=True)}" '
            f'rel="noreferrer">Authorize {name}</a>'
        )
    body = (
        f"<h1>Authorize <code>{name}</code></h1>"
        f"<p>{why} You'll sign in with the service and approve access; then you're sent "
        "back here.</p>"
        f"{account}{details}{way_on}"
        f'<p class="meta" id="left" data-seconds="{seconds}">'
        f"This link expires in {_duration(seconds)}.</p>"
    )
    script = """<script>
(function () {
  var left = document.getElementById("left"), go = document.getElementById("go");
  var end = Date.now() + 1000 * Number(left.dataset.seconds);
  function fmt(s) { var m = Math.floor(s / 60), r = s % 60;
    return m ? m + " min " + (r < 10 ? "0" : "") + r + " s" : r + " s"; }
  function tick() {
    var s = Math.max(0, Math.round((end - Date.now()) / 1000));
    if (s === 0) {
      left.textContent = "This link has expired. Ask for a new one from your app.";
      go.setAttribute("aria-disabled", "true"); go.removeAttribute("href"); go.disabled = true;
      return;
    }
    left.textContent = "This link expires in " + fmt(s) + ".";
    setTimeout(tick, 1000);
  }
  tick();
})();
</script>"""
    return _page(f"Authorize {pending.server}", body, script=script)


def gone_page() -> HTMLResponse:
    """A short link that expired, was already used, or never existed."""
    body = (
        '<div class="status warn"><span class="dot"></span>Link no longer valid</div>'
        "<h1>This authorization link has expired or was already used</h1>"
        "<p>Links are good for 15 minutes and work once. Go back to your app and try "
        "again: the next request that needs this server issues a fresh link (an "
        "assistant can also call <code>combiner__authorize_server</code>).</p>"
    )
    return _page("Link expired", body, status=404)


def authorized_page(
    server: str, account: str | None = None, login_hint: str | None = None
) -> HTMLResponse:
    """After the provider redirects back: done, and as whom. The account is
    the one the provider named (id_token) when it did, else the configured
    hint — labelled as such, since nothing confirmed it."""
    name = html.escape(server)
    if account:
        who = f'<p class="account">Signed in as <strong>{html.escape(account)}</strong></p>'
        if login_hint and account.lower() != login_hint.lower():
            who += (
                f'<p class="warning">That isn\'t the expected account '
                f"(<strong>{html.escape(login_hint)}</strong>). If it's wrong, "
                "authorize again and pick that one.</p>"
            )
    elif login_hint:
        who = (
            f'<p class="account">Requested account: <strong>{html.escape(login_hint)}</strong></p>'
        )
    else:
        who = ""
    body = (
        '<div class="status ok"><span class="dot"></span>Authorized</div>'
        f"<h1><code>{name}</code> is connected</h1>"
        f"{who}"
        "<p>You can close this page and return to your app. If a request was waiting "
        "on this, retry it.</p>"
    )
    return _page(f"{server} authorized", body)


def failed_page(server: str, reason: str, status: int) -> HTMLResponse:
    name = html.escape(server)
    body = (
        '<div class="status warn"><span class="dot"></span>Not authorized</div>'
        f"<h1>Authorizing <code>{name}</code> didn't complete</h1>"
        f"<p>{html.escape(reason)}</p>"
        "<p>Return to your app and try again for a fresh link.</p>"
    )
    return _page(f"{server} not authorized", body, status=status)


def _duration(seconds: int) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min {rest:02d} s" if minutes else f"{rest} s"


def login_page(next_path: str, error: str | None = None, status: int = 200) -> HTMLResponse:
    """web_auth's login (mcp_combiner.webgate): one password field."""
    problem = (
        f'<div class="status warn"><span class="dot"></span>{html.escape(error)}</div>'
        if error
        else ""
    )
    body = (
        f"{problem}<h1>Sign in</h1>"
        "<p>Enter the combiner's web password to open widgets and resources from this "
        "browser. You'll stay signed in here for a while.</p>"
        '<form method="post" action="/auth/login">'
        f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">'
        '<input class="field" type="password" name="password" autocomplete="current-password" '
        'placeholder="Password" required autofocus aria-label="Password">'
        '<button class="button" type="submit">Sign in</button>'
        "</form>"
    )
    return _page("Sign in", body, status=status)


def _details(pending: PendingAuthorization) -> str:
    """Where the user is sent, what it asks for, who asked and when."""
    split = urlsplit(pending.url)
    rows = [("Signs in at", html.escape(split.hostname or "?"))]
    scopes = parse_qs(split.query).get("scope", [""])[0].split()
    if scopes:
        rows.append(("Access", "<br>".join(html.escape(_scope_label(s)) for s in scopes)))
    for requester in pending.requested_by[:3]:
        label = html.escape(requester.client or "an MCP client")
        if requester.chat:
            label += f' <span class="dim">chat {html.escape(requester.chat[:8])}…</span>'
        rows.append(("Asked by", label))
    started = datetime.fromtimestamp(pending.started_at).strftime("%H:%M")
    rows.append(("Started", started))
    cells = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<dl class="details">{cells}</dl>'


def _scope_label(scope: str) -> str:
    """Google's scope URLs, shortened to their meaningful tail."""
    if scope.startswith("https://www.googleapis.com/auth/"):
        return scope.removeprefix("https://www.googleapis.com/auth/")
    return scope


def logout_page() -> HTMLResponse:
    """Sign out is a POST (a cross-site GET can't do it): this is its button."""
    body = (
        "<h1>Sign out</h1>"
        "<p>Sign this browser out of the combiner's widgets and authorization pages.</p>"
        '<form method="post" action="/auth/logout">'
        '<button class="button" type="submit">Sign out</button></form>'
    )
    return _page("Sign out", body)


@dataclass(frozen=True)
class StatusRow:
    """One server on the status page."""

    name: str
    state: str
    tools: int | None = None
    #: the short link's path, when an authorization is pending.
    authorize_path: str | None = None


#: lifecycle state → (dot class, label)
_STATES = {
    "ready": ("ok", "ready"),
    "connected": ("ok", "connected"),
    "started": ("warn", "starting"),
    "connecting": ("warn", "connecting"),
    "reconnecting": ("warn", "reconnecting"),
    "auth_failed": ("warn", "needs authorization"),
    "disconnected": ("bad", "disconnected"),
    "unreachable": ("bad", "unreachable"),
    "failed": ("bad", "failed"),
    "disabled": ("off", "disabled"),
}


def status_page(
    rows: list[StatusRow], *, version: str, uptime_s: float, signed_in: bool
) -> HTMLResponse:
    """The combiner at a glance (``/status``): every server's state, pending
    authorizations, version and uptime. Refreshes itself every 20 s."""
    healthy = sum(1 for r in rows if _STATES.get(r.state, ("warn", ""))[0] == "ok")
    live = sum(1 for r in rows if r.state != "disabled")
    items = []
    for row in sorted(rows, key=lambda r: (r.state == "disabled", r.name.lower())):
        tone, label = _STATES.get(row.state, ("warn", row.state))
        detail = html.escape(label)
        if row.tools:
            detail += f" · {row.tools} tool{'s' if row.tools != 1 else ''}"
        if row.authorize_path:
            detail = f'<a href="{html.escape(row.authorize_path, quote=True)}">Authorize</a>'
        items.append(
            f'<li><span class="dot {tone}"></span>'
            f'<span class="name">{html.escape(row.name)}</span>'
            f'<span class="state">{detail}</span></li>'
        )
    sign_out = (
        '<form method="post"><input type="hidden" name="action" value="sign-out">'
        '<button class="link" type="submit">Sign out</button></form>'
        if signed_in
        else ""
    )
    body = (
        "<h1>Status</h1>"
        f'<p class="summary">{healthy} of {live} servers up</p>'
        f'<p class="dim">Version {html.escape(version)} · up {_uptime(uptime_s)}</p>'
        f'<ul class="servers">{"".join(items)}</ul>'
        f'<div class="meta" style="margin-top:18px">{sign_out}</div>'
    )
    script = "<script>setTimeout(function () { location.reload(); }, 20000);</script>"
    return _page("Status", body, script=script)


def status_sign_in_page(error: str | None = None, status: int = 401) -> HTMLResponse:
    """``/status`` before sign-in: the password form, posting back to itself
    (a proxy may expose only ``/status``)."""
    problem = (
        f'<div class="status warn"><span class="dot"></span>{html.escape(error)}</div>'
        if error
        else ""
    )
    body = (
        f"{problem}<h1>Status</h1>"
        "<p>Sign in with the combiner's web password to see its servers.</p>"
        '<form method="post"><input type="hidden" name="action" value="sign-in">'
        '<input class="field" type="password" name="password" autocomplete="current-password" '
        'placeholder="Password" required autofocus aria-label="Password">'
        '<button class="button" type="submit">Sign in</button></form>'
    )
    return _page("Status", body, status=status)


def _uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} days"
