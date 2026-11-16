"""The CLI when no combiner answers: one clear message, whatever wrapped the
connection failure, with command-specific advice (authorize)."""

from __future__ import annotations

import argparse
import socket

import httpx2
import pytest

from mcp_combiner import ctl


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mcp-combiner")
    sub = parser.add_subparsers(dest="command")
    ctl.add_ctl_parsers(sub)
    return parser.parse_args(list(argv))


def _closed_port() -> int:
    with socket.socket() as sock:  # bound then released: nothing listens
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def test_connection_failures_are_recognised_however_wrapped() -> None:
    refused = httpx2.ConnectError("All connection attempts failed")
    assert ctl._unreachable(refused)
    assert ctl._unreachable(ConnectionRefusedError())
    try:  # fastmcp's wrapping
        try:
            raise refused
        except httpx2.ConnectError as inner:
            raise RuntimeError("Client failed to connect") from inner
    except RuntimeError as wrapped:
        assert ctl._unreachable(wrapped)
    grouped = ExceptionGroup("task group", [ValueError("x"), RuntimeError("y")])
    grouped.exceptions[1].__cause__ = refused  # anyio's groups
    assert ctl._unreachable(grouped)
    assert not ctl._unreachable(RuntimeError("Unknown tool"))
    assert not ctl._unreachable(ValueError("bad JSON"))


@pytest.mark.parametrize(
    ("argv", "hint"),
    [
        (("authorize", "gws"), True),
        (("enable", "gws"), False),
        (("status",), False),
        (("profile", "list"), False),
    ],
)
def test_commands_explain_an_unreachable_combiner(
    capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], hint: bool
) -> None:
    port = _closed_port()
    args = _parse(*argv, "--port", str(port))
    assert ctl.run(args) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"cannot reach the combiner at http://127.0.0.1:{port}")
    assert "mcp-combiner start" in err and "--url" in err
    assert ("Authorization runs inside the combiner" in err) is hint
    assert "Traceback" not in err


def test_other_errors_are_reported_as_themselves(capsys: pytest.CaptureFixture[str]) -> None:
    args = _parse("status")

    async def boom(_args: argparse.Namespace) -> int:
        raise ValueError("something else")

    args.func = boom
    assert ctl.run(args) == 1
    assert capsys.readouterr().err.strip() == "error: something else"
