"""The rules of per-chat collections (mcp_combiner.chatcollections), without
a running combiner: who sees a server, and whether it runs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_combiner import chatcollections, runtime
from mcp_combiner.config import CombinerConfig
from mcp_combiner.runtime import RUNTIME, ChatRef

A: ChatRef = (True, "tok-a")
B: ChatRef = (True, "tok-b")


@pytest.fixture(autouse=True)
def _clean_runtime() -> Iterator[None]:
    runtime.reset()
    yield
    runtime.reset()


def _config(tmp_path: Path, **servers: dict[str, Any]) -> CombinerConfig:
    path = tmp_path / "servers.json"
    path.write_text(
        json.dumps(
            {
                "collections": {"music": {"enabled": False}, "core": {}, "work": {}},
                "mcpServers": {
                    name: {"command": "true", **extra} for name, extra in servers.items()
                },
            }
        )
    )
    return CombinerConfig.load(str(path))


@pytest.fixture
def cfg(tmp_path: Path) -> CombinerConfig:
    return _config(
        tmp_path,
        ableton={"collections": ["music"]},
        github={"collections": ["core", "work"]},
        jira={"collections": ["work"]},
        plain={},
    )


def test_a_chat_without_state_follows_the_combiner(cfg: CombinerConfig) -> None:
    assert chatcollections.hidden_for(cfg, {}, set()) == {"ableton"}


def test_on_wins_across_a_chats_collections(cfg: CombinerConfig) -> None:
    # 'work' off for the chat; github is still in 'core', which is on.
    assert chatcollections.hidden_for(cfg, {"work": False}, set()) == {"ableton", "jira"}
    assert chatcollections.hidden_for(cfg, {"work": False, "core": False}, set()) == {
        "ableton",
        "jira",
        "github",
    }


def test_a_server_turned_on_wins_over_the_chats_collections(cfg: CombinerConfig) -> None:
    assert chatcollections.hidden_for(cfg, {"work": False}, {"jira"}) == {"ableton"}


def test_servers_outside_collections_are_never_hidden_or_managed(cfg: CombinerConfig) -> None:
    assert "plain" not in chatcollections.hidden_for(cfg, {"core": False}, set())
    assert "plain" not in dict(chatcollections.governed(cfg))


def test_demand_comes_only_from_explicit_chat_choices(cfg: CombinerConfig) -> None:
    sessions = RUNTIME.sessions
    assert chatcollections.demanded(cfg, sessions) == {}
    sessions.set_collection(A, "music", True)
    sessions.set_collection(B, "core", False)  # off: never a reason to run
    assert chatcollections.demanded(cfg, sessions) == {"ableton": [A]}
    sessions.set_forced(B, "ableton", True)
    assert chatcollections.demanded(cfg, sessions) == {"ableton": [A, B]}


def test_a_chats_blocklist_cancels_its_demand(cfg: CombinerConfig) -> None:
    RUNTIME.sessions.set_collection(A, "music", True)
    RUNTIME.sessions.disable_token("tok-a", "ableton")
    assert chatcollections.demanded(cfg, RUNTIME.sessions) == {}


def test_should_run(cfg: CombinerConfig) -> None:
    ableton = cfg.servers["ableton"]
    wanted: dict[str, list[ChatRef]] = {"ableton": [A]}
    assert not chatcollections.should_run(cfg, "ableton", ableton, {}, {})
    assert chatcollections.should_run(cfg, "ableton", ableton, wanted, {})
    # An explicit global toggle wins both ways.
    assert not chatcollections.should_run(cfg, "ableton", ableton, wanted, {"ableton": False})
    assert chatcollections.should_run(cfg, "ableton", ableton, {}, {"ableton": True})


def test_disabled_in_the_file_never_runs_for_a_chat(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ableton={"collections": ["music"], "disabled": True})
    ableton = cfg.servers["ableton"]
    assert not chatcollections.should_run(cfg, "ableton", ableton, {"ableton": [A]}, {})


def test_apply_sets_running_state_without_side_effects(cfg: CombinerConfig) -> None:
    RUNTIME.sessions.set_collection(A, "music", True)
    assert chatcollections.apply(cfg) == ["ableton"]
    assert not cfg.servers["ableton"].disabled
    RUNTIME.sessions.forget_chat_collections(A)
    chatcollections.apply(cfg)
    assert cfg.servers["ableton"].disabled


def test_effective_disabled_joins_the_blocklist_and_collections(cfg: CombinerConfig) -> None:
    """A tokenless chat (keyed by its session id): its own blocklist plus the
    servers its collections hide."""
    RUNTIME.config = cfg
    RUNTIME.sessions.set_collection((False, "sid-z"), "work", False)
    RUNTIME.sessions.disable("sid-z", "plain")
    assert RUNTIME.sessions.effective_disabled("sid-z") == {"plain", "ableton", "jira"}


def test_a_server_enabled_for_everyone_is_visible_to_chats_that_follow_the_combiner(
    cfg: CombinerConfig,
) -> None:
    """combiner__enable_server on a server whose collection is off: it runs,
    and every chat that hasn't set that collection itself sees it."""
    RUNTIME.server_overrides["ableton"] = True
    assert "ableton" not in chatcollections.hidden_for(cfg, {}, set())
    # A chat that turned the collection off still doesn't see it.
    assert "ableton" in chatcollections.hidden_for(cfg, {"music": False}, set())


# ── expiring abandoned chats ─────────────────────────────────────────


def test_expiry_removes_only_that_chats_vote(cfg: CombinerConfig) -> None:
    """Two chats want 'music'; the first to go leaves the server wanted by
    the other; once both are gone nothing wants it."""
    RUNTIME.config = cfg
    limit = chatcollections.expiry_seconds()
    sessions = RUNTIME.sessions
    sessions.set_collection(A, "music", True)
    sessions.set_collection(B, "music", True)
    sessions.chat_seen[A] = 0.0
    sessions.chat_seen[B] = limit  # seen later
    assert chatcollections.sweep_once(now=limit + 1) == [A]
    assert chatcollections.demanded(cfg, sessions) == {"ableton": [B]}
    assert chatcollections.sweep_once(now=2 * limit + 1) == [B]
    assert chatcollections.demanded(cfg, sessions) == {}
    assert sessions.chat_seen == {}


def test_a_global_setting_survives_every_chat_expiring(cfg: CombinerConfig) -> None:
    RUNTIME.config = cfg
    RUNTIME.sessions.set_collection(A, "core", False)
    RUNTIME.sessions.chat_seen[A] = 0.0
    chatcollections.sweep_once(now=chatcollections.expiry_seconds() + 1)
    github = cfg.servers["github"]
    assert chatcollections.should_run(cfg, "github", github, {}, {})  # 'core' is on in the file


def test_a_recent_or_unclocked_chat_is_kept(cfg: CombinerConfig) -> None:
    RUNTIME.config = cfg
    limit = chatcollections.expiry_seconds()
    RUNTIME.sessions.set_collection(A, "music", True)
    RUNTIME.sessions.chat_seen[A] = 10.0
    assert chatcollections.sweep_once(now=limit) == []  # not idle long enough
    # State restored by a handover has no clock: it starts at the first sweep.
    RUNTIME.sessions.set_collection(B, "music", True)
    del RUNTIME.sessions.chat_seen[B]
    assert chatcollections.sweep_once(now=5 * limit) == [A]
    assert RUNTIME.sessions.chat_seen[B] == 5 * limit


def test_a_request_restarts_the_clock_only_for_chats_with_state(cfg: CombinerConfig) -> None:
    sessions = RUNTIME.sessions
    sessions.touch_chat(A)
    assert A not in sessions.chat_seen
    sessions.set_collection(A, "music", True)
    sessions.chat_seen[A] = 0.0
    sessions.touch_chat(A)
    assert sessions.chat_seen[A] > 0.0


def test_expiry_follows_the_isolation_timers(tmp_path: Path) -> None:
    path = tmp_path / "servers.json"
    path.write_text(
        json.dumps(
            {
                "isolation": {"grace_seconds": 2.0, "park_ttl_seconds": 3.0},
                "collections": {"music": {}},
                "mcpServers": {},
            }
        )
    )
    RUNTIME.config = CombinerConfig.load(str(path))
    assert chatcollections.expiry_seconds() == 5.0
