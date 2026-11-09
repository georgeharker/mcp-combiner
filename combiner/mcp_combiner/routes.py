"""Session-management and health REST API.

The endpoints external clients (the Neovim plugin, the CLI) use to inspect
sessions and manage per-session server filters by session id or chat token —
without being the session owner. Handlers moved verbatim from
create_combiner; ``register_routes`` re-attaches them.

The token routes resolve tokens through ``RUNTIME.sessions.token_sessions``,
whose values are wire ``mcp-session-id``s — since fastmcp 4 the same ids
``Context.session_id`` (and so the ``/sessions`` listing) reports
(QUESTIONS.md Q1).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from mcp_combiner import authpages, authprompt, webauth, webgate
from mcp_combiner.config import CombinerConfig, HealthResponse, ServerStatusInfo
from mcp_combiner.connections import ConnectionManager
from mcp_combiner.runtime import RUNTIME, connection_session_id
from mcp_combiner.status import build_server_status
from mcp_combiner.toolcache import _notify_session_by_id, notify_chat_tools_changed

logger = logging.getLogger("mcp-combiner")


def register_routes(
    combiner: FastMCP,
    config: CombinerConfig,
    conn_manager: ConnectionManager,
) -> None:
    """Attach the /health and /sessions* routes to *combiner*."""

    # Health endpoint
    @combiner.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        server_statuses: dict[str, ServerStatusInfo] = {
            name: build_server_status(config, conn_manager, name) for name in config.servers
        }
        auth_failed = [n for n in conn_manager._connections if conn_manager.is_auth_failed(n)]
        response = HealthResponse(
            status="ok",
            servers=server_statuses,
            config_path=config.config_path,
            pending_oauth=auth_failed,
        )
        payload = response.model_dump(mode="json")
        # boot_id changes only when this combiner *process* (re)starts. Clients use
        # it to detect a restart and re-register Neovim instances + token binds.
        payload["boot_id"] = RUNTIME.boot_id
        payload["handover_recovery"] = RUNTIME.handover_recovery
        return JSONResponse(payload)

    # OAuth redirect target when ``oauth.public_url`` is set: the user's
    # browser (possibly on another device) lands here after authorizing. Open
    # by design — outside the inbound-bearer surface (combiner_protected_path)
    # — because the browser carries no bearer; the OAuth ``state`` binds the
    # redirect to the pending authorization it answers (authprompt.complete).
    # Short authorization links (authprompt.short_link): redirect to the real,
    # very long authorization URL. Open for the same reason as the callback;
    # the elicitation id is the secret.
    # The page names the server and offers the way on (authpages); ``?go=1``
    # skips it and redirects straight to the provider (scripts, old clients).
    # Under web_auth the page is gated (a leaked link must not let someone
    # else bind their account): links handed to clients carry a ticket, this
    # machine is exempt, anyone else signs in on the page itself — a POST
    # back here — since a proxy may expose only /oauth.
    @combiner.custom_route("/oauth/authorize/{elicitation_id}", methods=["GET", "POST"])
    async def oauth_authorize(request: Request) -> Response:
        elicitation_id = request.path_params["elicitation_id"]
        pending = authprompt.pending_by_elicitation(elicitation_id)
        if pending is None:
            return authpages.gone_page()
        if not webgate.admitted(request):
            redirect = webgate.ticket_redirect(
                request, authprompt.oauth_ticket_subject(elicitation_id)
            )
            if redirect is not None:
                return redirect
            if request.method == "POST":
                refusal = await webgate.check_password(request)
                if refusal is None:
                    response = RedirectResponse(request.url.path, status_code=303)
                    webgate.set_session_cookie(response, request)
                    return response
                page = authpages.authorize_page(
                    pending, needs_sign_in=True, sign_in_error=refusal.message
                )
                page.status_code = refusal.status
                return page
            page = authpages.authorize_page(pending, needs_sign_in=True)
            page.status_code = 401
            return page
        if request.method == "POST":
            return RedirectResponse(request.url.path, status_code=303)
        if request.query_params.get("go"):
            return RedirectResponse(pending.url, status_code=302)
        return authpages.authorize_page(pending)

    # The combiner at a glance, for a person. With web_auth: this machine, or
    # signed in — the sign-in form is on the page itself (POST back here), so
    # a proxy needs to expose only /status. Without web_auth: this machine
    # only (server names and pending authorization links aren't for anyone
    # who can reach the port).
    @combiner.custom_route("/status", methods=["GET", "POST"])
    async def status_page(request: Request) -> Response:
        on = webgate.web_auth_on()
        local = webgate.from_this_machine(request) and (not on or config.web_auth.exempt_local)
        session = on and webgate.signed_in(request)
        if request.method == "POST":
            if not on:
                return PlainTextResponse("Not Found", status_code=404)
            form = await request.form()
            if form.get("action") == "sign-out":
                response = RedirectResponse("/status", status_code=303)
                if session:
                    response.delete_cookie(webauth.COOKIE, path="/")
                return response
            if local or session:
                return RedirectResponse("/status", status_code=303)
            refusal = await webgate.check_password(request)
            if refusal is not None:
                return authpages.status_sign_in_page(refusal.message, refusal.status)
            response = RedirectResponse("/status", status_code=303)
            webgate.set_session_cookie(response, request)
            return response
        if not (local or session):
            if not on:
                return PlainTextResponse("Not Found", status_code=404)
            return authpages.status_sign_in_page()
        rows = []
        for name in config.servers:
            state = build_server_status(config, conn_manager, name).state
            pending = authprompt.pending_for(name)
            tools = RUNTIME.tools.server_tools.get(name)
            rows.append(
                authpages.StatusRow(
                    name=name,
                    state=state,
                    tools=len(tools) if tools is not None else None,
                    authorize_path=(
                        f"/oauth/authorize/{pending.elicitation_id}"
                        if pending is not None and pending.elicit
                        else None
                    ),
                )
            )
        return authpages.status_page(
            rows,
            version=_combiner_version(),
            uptime_s=time.time() - RUNTIME.started_at,
            signed_in=session,
        )

    @combiner.custom_route("/oauth/callback/{server}", methods=["GET"])
    async def oauth_callback(request: Request) -> HTMLResponse:
        server = request.path_params["server"]
        query = request.query_params
        try:
            completed = await authprompt.complete(
                server,
                query.get("code"),
                query.get("state"),
                query.get("error"),
                iss=query.get("iss"),
            )
        except authprompt.CallbackRejected as exc:
            return authpages.failed_page(server, str(exc), status=400)
        except Exception as exc:  # noqa: BLE001 — shown to the user, logged for ops
            logger.warning("OAuth callback for '%s' failed: %s", server, exc)
            return authpages.failed_page(
                server, "The service rejected the token exchange.", status=502
            )
        return authpages.authorized_page(
            server, account=completed.account, login_hint=completed.login_hint
        )

    # The general session map: every correlation the combiner holds around a
    # chat token — downstream wire session, nvim instance bind, and the
    # token-keyed isolated upstream sessions (live and parked tiers).
    # Loopback control plane — tokens and session ids appear in full, same
    # trust level as the /sessions/token routes. Tests and ops both read
    # lifecycle transitions here.
    @combiner.custom_route("/sessions/map", methods=["GET"])
    async def session_map(request: Request) -> JSONResponse:
        from mcp_combiner import nvim_proxy
        from mcp_combiner.isolated import REGISTRY as isolated_registry
        from mcp_combiner.isolated import ParkedSession

        token_instances, _instances = nvim_proxy.snapshot_routing()
        tokens = sorted(set(RUNTIME.sessions.token_sessions) | set(token_instances))
        return JSONResponse(
            {
                "tokens": [
                    {
                        "token": token,
                        "session_id": RUNTIME.sessions.token_sessions.get(token),
                        "instance_id": token_instances.get(token),
                    }
                    for token in tokens
                ],
                "isolated_live": [
                    {
                        "server": server,
                        "token": token,
                        "downstream_sessions": len(entry.session_ids),
                        "upstream_session_id": (
                            entry.client.transport.get_session_id()
                            if hasattr(entry.client.transport, "get_session_id")
                            else None
                        ),
                    }
                    for (server, token), entry in isolated_registry.live.items()
                ],
                "isolated_parked": [
                    {
                        "server": server,
                        "token": token,
                        "upstream_session_id": parked.session_id,
                        "protocol_version": parked.protocol_version,
                    }
                    # Only HTTP/SSE parked entries carry a resumable session
                    # id; stdio ParkedProc entries are running
                    # subprocesses (terminated, not resumed, at forget).
                    for (server, token), parked in isolated_registry.parked.items()
                    if isinstance(parked, ParkedSession)
                ],
                "grace_seconds": isolated_registry.grace,
                "park_ttl_seconds": isolated_registry.ttl,
            }
        )

    # Sanctioned-restart handover: ctl flags the NEXT shutdown to write the
    # one-shot handover payload to *path*. Only `mcp-combiner restart` calls
    # this; an unflagged shutdown (crash, kill, grace expiry) writes nothing.
    @combiner.custom_route("/handover/prepare", methods=["POST"])
    async def handover_prepare(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            path = str(body["path"])
        except Exception:
            return JSONResponse({"error": "body must be {'path': <str>}"}, status_code=400)
        parent = Path(path).expanduser().parent
        if not parent.is_dir():
            return JSONResponse({"error": f"directory does not exist: {parent}"}, status_code=400)
        RUNTIME.handover_path = str(Path(path).expanduser())
        logger.info("handover: shutdown will write %s", RUNTIME.handover_path)
        return JSONResponse({"status": "armed", "path": RUNTIME.handover_path})

    # --- Session management REST API ---
    # These endpoints allow external clients (e.g. the Neovim plugin) to
    # list active MCP sessions and manage per-session server filters by
    # session ID, without needing to be the session owner.

    @combiner.custom_route("/sessions", methods=["GET"])
    async def list_sessions(request: Request) -> JSONResponse:
        """List active MCP sessions with their IDs, client info, and filter state."""
        sessions_out: list[dict[str, Any]] = []
        for connection in RUNTIME.sessions.connections():
            sid = connection_session_id(connection) or str(id(connection))
            blocked = RUNTIME.sessions.disabled.get(sid, set())
            # Extract client info from the MCP initialize handshake
            client_info: dict[str, Any] | None = None
            cp = connection.client_params
            if cp is not None and cp.client_info is not None:
                client_info = {"name": cp.client_info.name, "version": cp.client_info.version}
            entry: dict[str, Any] = {
                "session_id": sid,
                "disabled_servers": sorted(blocked),
            }
            if client_info:
                entry["client_info"] = client_info
            sessions_out.append(entry)
        return JSONResponse({"sessions": sessions_out})

    @combiner.custom_route("/sessions/{session_id}/filter", methods=["GET", "POST", "DELETE"])
    async def manage_session_filter(request: Request) -> JSONResponse:
        """Manage per-session server blocklist by session ID.

        GET: Get current disabled servers for a session.
        POST: Set disabled servers for a session.
              Body: { "disabled_servers": ["server1", "server2"] }
              Or:   { "allowed_servers": ["server1"] } — inverts to disable all others
        DELETE: Clear all session filters for a session.
        """
        session_id = request.path_params.get("session_id", "")
        if not session_id:
            return JSONResponse({"error": "session_id required"}, status_code=400)

        if request.method == "GET":
            disabled = RUNTIME.sessions.disabled.get(session_id, set())
            return JSONResponse(
                {
                    "session_id": session_id,
                    "disabled_servers": sorted(disabled),
                }
            )

        if request.method == "DELETE":
            removed = RUNTIME.sessions.clear_disabled(session_id)
            # Notify the session so its tool list refreshes
            await _notify_session_by_id(session_id)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "cleared",
                    "previously_disabled": sorted(removed) if removed else [],
                }
            )

        # POST — manage disabled servers
        # Accepts:
        #   { "disabled_servers": ["srv1", "srv2"] } — set explicit disable list
        #   { "allowed_servers": ["srv1"] } — allow list, inverts to disable all others
        #   { "enable": "srv1" } — enable a single server (remove from disabled)
        #   { "disable": "srv1" } — disable a single server (add to disabled)
        # Note: allowed_servers=[] means disable ALL servers (not "allow all")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Handle single-server toggle operations first
        enable_server = body.get("enable")
        disable_server = body.get("disable")

        if enable_server is not None:
            if enable_server not in config.servers:
                return JSONResponse({"error": f"Unknown server: {enable_server}"}, status_code=400)
            RUNTIME.sessions.enable(session_id, enable_server)
            await _notify_session_by_id(session_id)
            logger.info("REST: session %s enabled server %s", session_id, enable_server)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "enabled",
                    "server": enable_server,
                    "disabled_servers": RUNTIME.sessions.disabled_snapshot(session_id),
                }
            )

        if disable_server is not None:
            if disable_server not in config.servers:
                return JSONResponse({"error": f"Unknown server: {disable_server}"}, status_code=400)
            RUNTIME.sessions.disable(session_id, disable_server)
            await _notify_session_by_id(session_id)
            logger.info("REST: session %s disabled server %s", session_id, disable_server)
            return JSONResponse(
                {
                    "session_id": session_id,
                    "action": "disabled",
                    "server": disable_server,
                    "disabled_servers": RUNTIME.sessions.disabled_snapshot(session_id),
                }
            )

        # Bulk operations: allowed_servers or disabled_servers
        allowed = body.get("allowed_servers")
        disabled = body.get("disabled_servers")

        # If allowed_servers is provided, compute disabled as inverse
        if allowed is not None:
            if not isinstance(allowed, list):
                return JSONResponse({"error": "allowed_servers must be a list"}, status_code=400)
            allowed_set = set(allowed)
            # Disable all servers not in allowed list (except _combiner meta-server)
            disabled_list = [s for s in config.servers if s not in allowed_set and s != "_combiner"]
        elif disabled is None:
            disabled_list = []
        else:
            disabled_list = list(disabled) if isinstance(disabled, list) else []

        if not isinstance(disabled_list, list):
            return JSONResponse({"error": "disabled_servers must be a list"}, status_code=400)

        # Validate server names
        unknown = [s for s in disabled_list if s not in config.servers]
        if unknown:
            return JSONResponse({"error": f"Unknown servers: {unknown}"}, status_code=400)

        RUNTIME.sessions.set_disabled(session_id, set(disabled_list))

        # Notify the target session
        await _notify_session_by_id(session_id)

        logger.info(
            "REST: session %s filter set to disabled=%s",
            session_id,
            disabled_list,
        )
        return JSONResponse(
            {
                "session_id": session_id,
                "disabled_servers": RUNTIME.sessions.disabled_snapshot(session_id),
            }
        )

    @combiner.custom_route("/sessions/token/{token}", methods=["GET"])
    async def lookup_session_token(request: Request) -> JSONResponse:
        """Look up the combiner session_id associated with a token.

        The token is a UUID generated by the Neovim plugin per chat and
        embedded as the URL path suffix (/mcp/<token>).
        TokenRewriteMiddleware records the mapping when FastMCP assigns the
        session_id on the first initialize response.
        """
        token = request.path_params.get("token", "")
        session_id = RUNTIME.sessions.session_for_token(token)
        if session_id is None:
            return JSONResponse({"error": "token not found"}, status_code=404)
        logger.debug("Token lookup: %s -> %s", token, session_id)
        return JSONResponse({"token": token, "session_id": session_id})

    @combiner.custom_route("/sessions/token/{token}/filter", methods=["GET", "POST", "DELETE"])
    async def manage_token_filter(request: Request) -> JSONResponse:
        """Manage per-session server blocklist by token.

        The token is the stable identifier the Lua plugin holds for both ACP
        and HTTP adapter sessions.  If the token is already mapped to a
        session_id the operation is applied immediately; otherwise it is stored
        as pending and applied by TokenRewriteMiddleware when the client connects.

        GET:    Returns current or pending filter state.
        POST:   Same body format as /sessions/{session_id}/filter.
                If the session is not yet connected, stores as pending.
        DELETE: Clears filter (and any pending state).
        """
        token = request.path_params.get("token", "")
        if not token:
            return JSONResponse({"error": "token required"}, status_code=400)

        session_id = RUNTIME.sessions.session_for_token(token)

        async def _notify_token_sessions() -> None:
            # Best-effort "your tool list changed" nudges. The filter itself is
            # enforced by read-through at request time — these only prompt a
            # connected client to refetch sooner. Both id namespaces are tried
            # (wire mcp-session-id and Context.session_id); misses are no-ops.
            from mcp_combiner import nvim_proxy

            if session_id:
                await _notify_session_by_id(session_id)
            for ctx_sid in nvim_proxy.sessions_for_token(token):
                await _notify_session_by_id(ctx_sid)
            await notify_chat_tools_changed(token)  # 2026-era listen streams

        if request.method == "GET":
            return JSONResponse(
                {
                    "token": token,
                    "session_id": session_id,
                    "connected": session_id is not None,
                    "disabled_servers": RUNTIME.sessions.token_snapshot(token),
                    "hidden_tools": sorted(RUNTIME.sessions.token_hidden_tools.get(token, set())),
                }
            )

        if request.method == "DELETE":
            previously = RUNTIME.sessions.token_snapshot(token)
            RUNTIME.sessions.set_pending(token, None)
            RUNTIME.sessions.set_hidden(RUNTIME.sessions.token_hidden_tools, token, set())
            await _notify_token_sessions()
            return JSONResponse(
                {
                    "token": token,
                    "session_id": session_id,
                    "action": "cleared",
                    "previously_disabled": previously,
                }
            )

        # POST — parse body (same format as /sessions/{id}/filter)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Resolve to a disabled set using the same logic as manage_session_filter
        enable_server = body.get("enable")
        disable_server = body.get("disable")
        allowed = body.get("allowed_servers")
        disabled_list = body.get("disabled_servers")

        def _resolve_disabled(current: set[str]) -> set[str] | None:
            """Return new disabled set or None to clear."""
            if enable_server is not None:
                current.discard(enable_server)
                return current if current else None
            if disable_server is not None:
                current.add(disable_server)
                return current
            if allowed is not None:
                allowed_set = set(allowed)
                d = {s for s in config.servers if s not in allowed_set and s != "_combiner"}
                return d if d else None
            if disabled_list is not None:
                return set(disabled_list) if disabled_list else None
            return current if current else None

        # The token store is CANONICAL (read-through enforcement resolves
        # ctx.session_id → token → this store at request time), so the write
        # is the same whether the chat is connected yet or not — connected
        # sessions additionally get a list_changed nudge to refetch.
        current = set(RUNTIME.sessions.pending_for(token))
        new_disabled = _resolve_disabled(current)
        RUNTIME.sessions.set_pending(token, new_disabled)
        # Per-chat hidden tools (glob patterns): "hidden_tools" replaces the
        # set ([] clears), "hide" / "show" add or remove one pattern.
        hidden = set(RUNTIME.sessions.token_hidden_tools.get(token, set()))
        if isinstance(body.get("hidden_tools"), list):
            hidden = {str(p) for p in body["hidden_tools"] if str(p).strip()}
        if isinstance(body.get("hide"), str):
            hidden.add(body["hide"])
        if isinstance(body.get("show"), str):
            hidden.discard(body["show"])
        RUNTIME.sessions.set_hidden(RUNTIME.sessions.token_hidden_tools, token, hidden)
        await _notify_token_sessions()
        logger.info(
            "REST token filter: token=%s connected=%s disabled=%s",
            token,
            session_id is not None,
            sorted(new_disabled) if new_disabled else [],
        )
        return JSONResponse(
            {
                "token": token,
                "session_id": session_id,
                "connected": session_id is not None,
                "disabled_servers": RUNTIME.sessions.token_snapshot(token),
                "hidden_tools": sorted(RUNTIME.sessions.token_hidden_tools.get(token, set())),
            }
        )


def _combiner_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("mcp-combiner")
    except PackageNotFoundError:
        return "dev"
