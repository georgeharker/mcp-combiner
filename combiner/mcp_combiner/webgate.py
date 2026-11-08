"""The web_auth gate and login routes (policy and tokens: mcp_combiner.webauth).

:class:`WebAuthGate` wraps the main app (for ``/ui/*``) and the widget relay
(everything): a request passes when web_auth is off, comes from this machine,
carries a valid session cookie, or carries a valid ticket — exchanged for the
cookie with a redirect to the clean URL. Otherwise pages go to the login page
and other requests get 401.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from mcp_combiner import authpages, webauth
from mcp_combiner.runtime import RUNTIME

logger = logging.getLogger("mcp-combiner")


def ui_path(path: str) -> bool:
    """The main app's gated surface: the widget pages."""
    return path == "/ui" or path.startswith("/ui/")


def _ticket_subject(request: Request) -> str | None:
    """The chat token a ticket on *request* must name: the ``/ui/<token>/``
    path segment, or the relay's ``pt`` parameter."""
    parts = request.url.path.split("/")
    if len(parts) > 2 and parts[1] == "ui":
        return parts[2]
    return request.query_params.get("pt")


def client_address(request: Request) -> str | None:
    """The browser's address — already the proxied browser's when a local
    reverse proxy forwarded the request (uvicorn's proxy headers, set in
    __main__)."""
    return request.client.host if request.client else None


def admitted(request: Request) -> bool:
    """Off, from this machine, or signed in (a valid session cookie)."""
    config = RUNTIME.config
    signer = RUNTIME.web_signer
    if config is None or signer is None or config.web_auth.mode != "password":
        return True
    if config.web_auth.exempt_local and webauth.is_local(client_address(request)):
        return True
    return signer.verify(request.cookies.get(webauth.COOKIE, ""), "s") is not None


def ticket_redirect(request: Request, subject: str | None) -> Response | None:
    """A request carrying a valid ticket for *subject*: set the session and
    redirect to the same URL without the ticket."""
    signer = RUNTIME.web_signer
    ticket = request.query_params.get(webauth.TICKET_PARAM)
    if signer is None or not ticket or subject is None:
        return None
    claims = signer.verify(ticket, "t")
    if claims is None or claims.get("sub") != subject:
        return None
    config = RUNTIME.config
    uses = config.web_auth.ticket_uses if config is not None else 1
    if not webauth.TICKETS.redeem(claims, uses):
        logger.info("web_auth: a used-up ticket was presented for %s", request.url.path)
        return None
    query = [(k, v) for k, v in request.query_params.multi_items() if k != webauth.TICKET_PARAM]
    clean = request.url.path + (f"?{urlencode(query)}" if query else "")
    response = RedirectResponse(clean, status_code=303)
    set_session_cookie(response, request)
    return response


@dataclass(frozen=True)
class Refusal:
    """Why a posted password was refused, and the status to answer with."""

    message: str
    status: int


TOO_MANY = Refusal("Too many attempts — wait a minute and try again.", 429)
WRONG = Refusal("That password isn't right.", 401)

#: scrypt is ~30 ms of CPU and 16 MB: run it off the event loop, two at a time.
_VERIFY_SLOTS = asyncio.Semaphore(2)


async def check_password(request: Request) -> Refusal | None:
    """Check a posted login form. ``None`` on success.

    The attempt is reserved with the rate limiter BEFORE anything is awaited,
    so concurrent guesses count at once (webauth.LoginLimiter).
    """
    config = RUNTIME.config
    client = client_address(request) or "?"
    if not webauth.LIMITER.begin(client):
        return TOO_MANY
    ok = False
    try:
        form = await request.form()
        password = form.get("password")
        stored = config.web_auth.password_hash if config is not None else None
        if isinstance(password, str) and stored:
            async with _VERIFY_SLOTS:
                ok = await asyncio.to_thread(webauth.verify_password, password, stored)
    finally:
        webauth.LIMITER.end(client, failed=not ok)
    if not ok:
        logger.warning("web_auth: failed login from %s", client)
        return WRONG
    return None


def _secure(request: Request) -> bool:
    return (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )


def set_session_cookie(response: Response, request: Request) -> None:
    signer = RUNTIME.web_signer
    config = RUNTIME.config
    if signer is None or config is None:
        return
    days = config.web_auth.session_days
    response.set_cookie(
        webauth.COOKIE,
        signer.session(days),
        max_age=int(days * 86400),
        httponly=True,
        samesite="lax",
        secure=_secure(request),
        path="/",
    )


class WebAuthGate(BaseHTTPMiddleware):
    def __init__(
        self, app: Any, *, is_gated: Callable[[str], bool], login_origin: str | None = None
    ) -> None:
        super().__init__(app)
        self._is_gated = is_gated
        #: where the login page lives (the relay is another origin: no page).
        self._login_origin = login_origin

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self._is_gated(request.url.path) or admitted(request):
            return await call_next(request)
        redirect = ticket_redirect(request, _ticket_subject(request))
        if redirect is not None:
            return redirect
        accepts_html = "text/html" in request.headers.get("accept", "")
        if self._login_origin is not None and request.method == "GET" and accepts_html:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                f"{webauth.LOGIN_PATH}?next={quote(target, safe='')}", status_code=303
            )
        return PlainTextResponse("login required", status_code=401)


def _safe_next(raw: str | None) -> str:
    """Only same-origin paths (no scheme, no //host). Any backslash is refused:
    browsers read ``/\\host`` as ``//host``."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return "/"
    return raw


def register_login_routes(combiner: FastMCP) -> None:
    @combiner.custom_route(webauth.LOGIN_PATH, methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        config = RUNTIME.config
        if config is None or RUNTIME.web_signer is None or config.web_auth.mode != "password":
            return PlainTextResponse("web login is not enabled", status_code=404)
        target = _safe_next(request.query_params.get("next"))
        if request.method == "GET":
            return authpages.login_page(target)
        refusal = await check_password(request)
        if refusal is not None:
            return authpages.login_page(target, error=refusal.message, status=refusal.status)
        form = await request.form()
        response = RedirectResponse(_safe_next(str(form.get("next") or target)), status_code=303)
        set_session_cookie(response, request)
        return response

    # POST only (a GET shows the button): another site can't sign you out by
    # embedding the URL.
    @combiner.custom_route(webauth.LOGOUT_PATH, methods=["GET", "POST"])
    async def logout(request: Request) -> Response:
        if request.method == "GET":
            return authpages.logout_page()
        response = RedirectResponse(webauth.LOGIN_PATH, status_code=303)
        response.delete_cookie(webauth.COOKIE, path="/")
        return response


def ticket_query(chat_token: str) -> str:
    """``&t=<ticket>`` for a widget URL handed to a bearer-authenticated
    client, or ``""`` when tickets are off."""
    config = RUNTIME.config
    signer = RUNTIME.web_signer
    if (
        config is None
        or signer is None
        or config.web_auth.mode != "password"
        or not config.web_auth.tickets
    ):
        return ""
    return f"&{webauth.TICKET_PARAM}={signer.ticket(chat_token)}"
