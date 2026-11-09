"""Telling a chat its per-chat state on a server was reset.

An isolated server's per-chat state (an open project, a drawing) lives in the
chat's upstream session. When that session ends without the chat asking (the
server restarted, or a dead session was replaced), the model may still hold
references into it. So the chat's NEXT call to that server carries a short
note saying so, once per reset. Design decision 01M70BRB5N.
"""

from __future__ import annotations

_pending: dict[tuple[str, str], str] = {}


def record(server: str, token: str, why: str = "the server restarted") -> None:
    """Note that *token*'s state on *server* was reset (shown on its next call)."""
    _pending[(server, token)] = why


def record_all(server: str, tokens: list[str], why: str = "the server restarted") -> None:
    for token in tokens:
        record(server, token, why)


def consume(server: str, token: str) -> str | None:
    """The note for *token*'s next call to *server*, once."""
    why = _pending.pop((server, token), None)
    if why is None:
        return None
    return (
        f"Note: {server}'s per-chat state for this chat was reset ({why}). Anything it "
        "held for you before (an open project, documents) is gone; set it up again "
        "if you still need it."
    )


def reset() -> None:
    _pending.clear()
