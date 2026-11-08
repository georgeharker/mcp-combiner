"""The web_auth gate and login routes (policy and tokens: mcp_combiner.webauth).

:class:`WebAuthGate` wraps the main app (for ``/ui/*``) and the widget relay
(everything): a request passes when web_auth is off, comes from this machine,
carries a valid session cookie, or carries a valid ticket — exchanged for the
cookie with a redirect to the clean URL. Otherwise pages go to the login page
and other requests get 401.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
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
        config = RUNTIME.config
        signer = RUNTIME.web_signer
        if (
            config is None
            or signer is None
            or config.web_auth.mode != "password"
            or not self._is_gated(request.url.path)
        ):
            return await call_next(request)
        client = request.client.host if request.client else None
        if config.web_auth.exempt_local and webauth.is_local(client):
            return await call_next(request)
        if signer.verify(request.cookies.get(webauth.COOKIE, ""), "s") is not None:
            return await call_next(request)
        ticket = request.query_params.get(webauth.TICKET_PARAM)
        if ticket:
            claims = signer.verify(ticket, "t")
            if claims is not None and claims.get("sub") == _ticket_subject(request):
                query = [(k, v) for k, v in request.query_params.multi_items() if k != "t"]
                clean = request.url.path + (f"?{urlencode(query)}" if query else "")
                response = RedirectResponse(clean, status_code=303)
                set_session_cookie(response, request)
                return response
        accepts_html = "text/html" in request.headers.get("accept", "")
        if self._login_origin is not None and request.method == "GET" and accepts_html:
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(
                f"{webauth.LOGIN_PATH}?next={quote(target, safe='')}", status_code=303
            )
        return PlainTextResponse("login required", status_code=401)


def _safe_next(raw: str | None) -> str:
    """Only same-origin paths (no scheme, no //host)."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\\\" in raw:
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
        client = request.client.host if request.client else "?"
        if webauth.too_many_failures(client):
            return authpages.login_page(
                target, error="Too many attempts — wait a minute and try again.", status=429
            )
        form = await request.form()
        password = form.get("password")
        stored = config.web_auth.password_hash or ""
        if not isinstance(password, str) or not webauth.verify_password(password, stored):
            webauth.record_failure(client)
            logger.warning("web_auth: failed login from %s", client)
            return authpages.login_page(target, error="That password isn't right.", status=401)
        response = RedirectResponse(_safe_next(str(form.get("next") or target)), status_code=303)
        set_session_cookie(response, request)
        return response

    @combiner.custom_route(webauth.LOGOUT_PATH, methods=["GET", "POST"])
    async def logout(request: Request) -> Response:
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
