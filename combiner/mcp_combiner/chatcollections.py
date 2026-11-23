"""Per-chat collection toggles (design 01M84GRGJ6).

A collection is a named group of servers (``"collections"`` in the config). It
is on or off for the whole combiner; on top of that, a chat can turn one on or
off for itself, or turn a single server on (a per-chat server toggle wins over
the chat's collections). Two questions follow, for every server that belongs to
a collection:

- **Is it available to this chat?** :func:`hidden_for` — at least one of its
  collections is on for the chat (the chat's own setting, else the global one),
  or the chat turned the server itself on. Hidden servers join the chat's
  blocklist (``SessionRegistry.effective_disabled``), so listings, calls and
  every other per-chat filter path apply it.
- **Does it run?** :func:`should_run` — a global toggle of the server itself
  decides when there is one; otherwise it runs if it isn't disabled in the file
  and a collection of it is on globally, or some chat wants it (:func:`demanded`).
  Turning a collection on in one chat therefore starts its servers, visible
  only to the chats that want them; they stop once nothing wants them.

:func:`reconcile` starts and stops servers to match, after any change;
:func:`apply` sets the same state without side effects (startup, reload).
Servers outside every collection are never touched here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from mcp_combiner.config import CombinerConfig, ServerConfig
from mcp_combiner.runtime import RUNTIME, ChatRef, SessionRegistry

logger = logging.getLogger("mcp-combiner")

_lock = asyncio.Lock()


def governed(config: CombinerConfig) -> Iterator[tuple[str, ServerConfig]]:
    """The servers that belong to at least one loaded collection."""
    for name, srv in config.servers.items():
        if any(c in config.collections for c in srv.collections):
            yield name, srv


def on_for_chat(
    config: CombinerConfig, srv: ServerConfig, state: dict[str, bool], forced_on: bool = False
) -> bool:
    """Whether any of *srv*'s collections is on for a chat with *state*. A
    server turned on explicitly for everyone (*forced_on*: combiner__enable_server)
    counts as on for every collection the chat hasn't set itself."""
    return any(
        state.get(c, forced_on or config.collections[c].enabled)
        for c in srv.collections
        if c in config.collections
    )


def hidden_for(config: CombinerConfig, state: dict[str, bool], forced: set[str]) -> set[str]:
    """The servers a chat with collection *state* and explicitly-on *forced*
    servers doesn't see because of collections."""
    overrides = RUNTIME.server_overrides
    return {
        name
        for name, srv in governed(config)
        if name not in forced
        and not on_for_chat(config, srv, state, forced_on=overrides.get(name) is True)
    }


def _wants(config: CombinerConfig, sessions: SessionRegistry, ref: ChatRef) -> Iterator[str]:
    """The collection-governed servers chat *ref* itself asks to have running:
    one of their collections is on for it explicitly, or it turned them on."""
    state = sessions.collections_of(ref)
    forced = sessions.forced_of(ref)
    blocked = sessions.blocked_of(ref)
    for name, srv in governed(config):
        if name in blocked:
            continue
        if name in forced or any(state.get(c) is True for c in srv.collections):
            yield name


def demanded(config: CombinerConfig, sessions: SessionRegistry) -> dict[str, list[ChatRef]]:
    """Every collection-governed server some chat wants running, with the chats."""
    out: dict[str, list[ChatRef]] = {}
    for ref in sessions.chats_with_collection_state():
        for name in _wants(config, sessions, ref):
            out.setdefault(name, []).append(ref)
    return out


def should_run(
    config: CombinerConfig,
    name: str,
    srv: ServerConfig,
    wanted: dict[str, list[ChatRef]],
    overrides: dict[str, bool],
) -> bool:
    """Whether collection-governed server *name* should be running."""
    if name in overrides:
        return overrides[name]
    if srv.disabled_in_file:
        return False
    on_globally = any(
        config.collections[c].enabled for c in srv.collections if c in config.collections
    )
    return on_globally or name in wanted


def apply(config: CombinerConfig) -> list[str]:
    """Set every collection-governed server's ``disabled`` to match, without
    starting or stopping anything (startup before mounting; a reloaded config
    before diffing it). Returns the servers that run only because a chat wants
    them."""
    wanted = demanded(config, RUNTIME.sessions)
    by_chats: list[str] = []
    for name, srv in governed(config):
        run = should_run(config, name, srv, wanted, RUNTIME.server_overrides)
        srv.disabled = not run
        if (
            run
            and name in wanted
            and not any(
                config.collections[c].enabled for c in srv.collections if c in config.collections
            )
        ):
            by_chats.append(name)
    if by_chats:
        logger.info("collections: running for chats that turned them on: %s", ", ".join(by_chats))
    return by_chats


@dataclass
class Outcome:
    started: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = []
        if self.started:
            parts.append(f"started {', '.join(self.started)}")
        if self.stopped:
            parts.append(f"stopped {', '.join(self.stopped)}")
        if self.failed:
            parts.append(f"failed: {', '.join(self.failed)} (see combiner__status)")
        return "; ".join(parts) or "no servers changed"


def _went_wrong(result: str) -> bool:
    lowered = result.lower()
    return lowered.startswith("error") or "failed" in lowered


async def reconcile() -> Outcome:
    """Start and stop collection-governed servers to match the global and
    per-chat state. Serialized: concurrent changes reconcile one at a time."""
    config = RUNTIME.config
    start, stop = RUNTIME.start_server, RUNTIME.stop_server
    outcome = Outcome()
    if config is None or start is None or stop is None:
        return outcome
    async with _lock:
        wanted = demanded(config, RUNTIME.sessions)
        for name, srv in list(governed(config)):
            run = should_run(config, name, srv, wanted, RUNTIME.server_overrides)
            if run and srv.disabled:
                result = await start(name)
                (outcome.failed if _went_wrong(result) else outcome.started).append(name)
            elif not run and not srv.disabled:
                result = await stop(name)
                (outcome.failed if _went_wrong(result) else outcome.stopped).append(name)
    if outcome.started or outcome.stopped or outcome.failed:
        logger.info("collections: %s", outcome.describe())
    return outcome


def visibility(config: CombinerConfig, ref: ChatRef | None) -> dict[str, bool]:
    """Each collection-governed server: visible to chat *ref* (and running)?"""
    sessions = RUNTIME.sessions
    hidden = hidden_for(config, sessions.collections_of(ref), sessions.forced_of(ref))
    blocked = sessions.blocked_of(ref)
    return {
        name: not srv.disabled and name not in hidden and name not in blocked
        for name, srv in sorted(governed(config))
    }


def forget_session(session_id: str) -> None:
    """A tokenless chat's session ended (an explicit DELETE): drop its
    collection state and stop what only it wanted. A tokened chat's state is
    kept — the token outlives any one session."""
    ref: ChatRef = (False, session_id)
    if not RUNTIME.sessions.forget_chat_collections(ref):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(reconcile(), name="collections-reconcile")
