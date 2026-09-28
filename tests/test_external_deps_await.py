"""The ``ccc await`` dependencies in the external-deps registry."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from extdeps import MissingExternalDependency

from command_center import external_deps


def _script(path: Path, help_text: str, *, executable: bool = True) -> Path:
    path.write_text(f"#!/bin/sh\necho '{help_text}'\n", encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


def test_both_entries_are_declared_without_sibling_paths() -> None:
    for name, env in (("zoho-api.py", "ZOHO_API_BIN"), ("slack_api.py", "SLACK_API_BIN")):
        dep = external_deps.EXTERNAL_DEPS[name]
        assert dep.env == env
        assert not dep.siblings  # the public tree carries no private checkout layout


def test_relative_override_is_canonicalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(tmp_path / "zoho-api.py", "usage: -i/--inbound-since")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZOHO_API_BIN", "./sub/../zoho-api.py")
    (tmp_path / "sub").mkdir()
    found = external_deps.await_exe("zoho-api.py", needed_for="test")
    assert found == str(tmp_path / "zoho-api.py")
    assert os.path.isabs(found)


def test_path_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _script(tmp_path / "slack_api.py", "--dm USER")
    monkeypatch.delenv("SLACK_API_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert external_deps.await_exe("slack_api.py", needed_for="t") == str(tmp_path / "slack_api.py")


def test_missing_dependency_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_API_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(MissingExternalDependency):
        external_deps.await_exe("slack_api.py", needed_for="t")


def test_outdated_copy_without_the_probe_verb_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZOHO_API_BIN", str(_script(tmp_path / "zoho-api.py", "usage: -l")))
    with pytest.raises(MissingExternalDependency):
        external_deps.await_exe("zoho-api.py", needed_for="t")


def test_non_executable_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(tmp_path / "slack_api.py", "--dm", executable=False)
    monkeypatch.setenv("SLACK_API_BIN", str(script))
    with pytest.raises(MissingExternalDependency):
        external_deps.await_exe("slack_api.py", needed_for="t")
