"""stale_install_note: the stale-process suffix for lazy ImportErrors.

Design note: an in-place reinstall deletes the module directory a running
combiner was launched from; its cached package keeps the dead path, so the
first lazy submodule import (``resetnotice``, on the first restart call)
fails with "cannot import name" citing a nonexistent file — indistinguishable
from a release bug without the suffix. The suffix fires only when the loaded
package directory is really gone; anything else stays unexplained.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from mcp_combiner.runtime import stale_install_note


def _fake_package(monkeypatch: pytest.MonkeyPatch, dirpath: str) -> None:
    """Point sys.modules["mcp_combiner"] at a package in a fake directory."""
    pkg = types.ModuleType("mcp_combiner")
    pkg.__file__ = f"{dirpath}/__init__.py"
    pkg.__path__ = [dirpath]
    monkeypatch.setitem(sys.modules, "mcp_combiner", pkg)


def test_suffix_when_package_dir_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_package(monkeypatch, str(tmp_path / "deleted-install" / "mcp_combiner"))
    exc = ImportError("cannot import name 'resetnotice' from 'mcp_combiner'")
    note = stale_install_note(exc)
    assert "stale" in note
    assert "deleted-install" in note  # names the dead path, so the story is checkable
    assert "restart --force" in note


def test_no_suffix_when_package_dir_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real package is installed in this test environment.
    assert stale_install_note(ImportError("cannot import name 'resetnotice'")) == ""


def test_no_suffix_for_non_import_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_package(monkeypatch, str(tmp_path / "deleted" / "mcp_combiner"))
    assert stale_install_note(ValueError("unrelated")) == ""


def test_no_suffix_when_package_missing_from_sys_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "mcp_combiner")  # never in this process
    assert stale_install_note(ImportError("cannot import name 'x'")) == ""
