"""`mcp-combiner --version` answers before the server's imports: clients (the pi
extension) probe it on every start, and importing uvicorn and the server stack
first made it take over a second."""

from __future__ import annotations

import subprocess
import sys

from mcp_combiner import __version__

_PROBE = """
import runpy, sys
sys.argv = ["mcp-combiner", "--version"]
try:
    runpy.run_module("mcp_combiner", run_name="__main__")
except SystemExit as exit:
    code = exit.code
print("EXIT", code, "uvicorn" in sys.modules, "mcp_combiner.server" in sys.modules)
"""


def test_version_skips_the_server_imports() -> None:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert out[0] == f"mcp-combiner {__version__}"
    assert out[-1] == "EXIT 0 False False"


def test_version_output_matches_the_cli_parser() -> None:
    """The fast path prints what argparse's version action would."""
    import argparse
    import contextlib
    import io

    from mcp_combiner.__main__ import _add_serve_args  # imports the full CLI

    parser = argparse.ArgumentParser(prog="mcp-combiner")
    _add_serve_args(parser)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
        parser.parse_args(["--version"])
    assert buffer.getvalue().strip() == f"mcp-combiner {__version__}"
