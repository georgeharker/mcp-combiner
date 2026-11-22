"""Which MCP protocol era the combiner offers its own clients.

mcp 2 serves both the ``initialize``-handshake era (stateful: a long-lived
session, server-initiated requests, unsolicited notifications) and the
2026-07-28 revision (stateless: per-request connections; elicitation becomes
``InputRequiredResult`` return-and-retry; notifications only on
``subscriptions/listen`` streams). A client probes ``server/discover`` and uses
the new revision when the server lists it.

The combiner translates between the eras for a new-revision client (design:
"Support both MCP eras downstream"): ``subscriptions/listen`` streams per chat
(:mod:`mcp_combiner.listen`), consent and restart prompts as
``InputRequiredResult``, and every kind of upstream input ask bridged both ways
(:mod:`mcp_combiner.asks`, :mod:`mcp_combiner.heldcalls`). It is still offered
only on request: by default the HTTP app answers ``server/discover`` with no
supported versions, so a client that probes (Claude Code does) falls back to
the handshake. ``MCP_COMBINER_OFFER_2026=1`` lifts the pin.

Only the HTTP app is pinned: on an in-memory transport one duplex stream would
carry both the probe and the session, and the probe opens a new-era connection
before the handshake fallback can happen.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import FastMCP
from mcp.server.context import ServerRequestContext
from mcp.types import DiscoverResult, RequestParams, ServerCapabilities

logger = logging.getLogger("mcp-combiner")

OFFER_2026_ENV = "MCP_COMBINER_OFFER_2026"


def offers_2026_era() -> bool:
    """Whether the combiner offers the 2026-07-28 revision to its clients."""
    return os.environ.get(OFFER_2026_ENV, "").strip().lower() in ("1", "true", "yes")


async def _handshake_only_discover(
    ctx: ServerRequestContext[Any, Any], params: RequestParams
) -> DiscoverResult:
    return DiscoverResult(supported_versions=[], capabilities=ServerCapabilities())


def pin_handshake_era(combiner: FastMCP[Any], *, force: bool = False) -> None:
    """Make ``server/discover`` list no supported versions (see module docs).

    Uses the mcp lowlevel server's public ``add_request_handler``, reached via
    fastmcp's ``_mcp_server`` (fastmcp's own route into it). If a fastmcp
    upgrade moves it, the era e2e test fails loudly. ``force`` pins regardless
    of ``MCP_COMBINER_OFFER_2026`` (the mock server's own handshake pin).
    """
    if offers_2026_era() and not force:
        logger.warning(
            "%s set: offering the 2026-07-28 revision to clients",
            OFFER_2026_ENV,
        )
        return
    combiner._mcp_server.add_request_handler(
        "server/discover", RequestParams, _handshake_only_discover
    )
