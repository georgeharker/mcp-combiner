"""Upstream servers' MCP ``instructions``, served on demand (design 01M8FG9JMW).

A server's instructions arrive in its ``initialize`` result (handshake era) or
its ``server/discover`` result (2026-07-28). The combiner does not concatenate
them into its own: they would be stale (servers connect after a client
initializes, and come and go per chat) and, across many servers, large. They
are recorded here as each upstream session receives them (the per-server
session class in :mod:`mcp_combiner.upstream_session`) and handed out by
``combiner__server_instructions``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("mcp-combiner")

#: server -> its latest instructions ("" when it sent none).
_by_server: dict[str, str] = {}


def record(server: str, text: object) -> None:
    """Remember *server*'s instructions (from its latest initialize/discover)."""
    value = text.strip() if isinstance(text, str) else ""
    if _by_server.get(server) != value:
        logger.info(
            "Instructions for '%s': %s",
            server,
            f"{len(value)} chars" if value else "none",
        )
    _by_server[server] = value


def get(server: str) -> str | None:
    """*server*'s instructions: the text, "" when it sent none, or ``None`` when
    the combiner hasn't connected to it yet."""
    return _by_server.get(server)


def reset() -> None:
    _by_server.clear()
