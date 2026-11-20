"""tp#2266 B7: ``tools/retire_vault_files.py`` archives ccc's vault trees before removing them.

Every test runs on the tmp vault the autouse ``_isolate_vault_dirs`` fixture points the
loaded config at, with the store under the tmp ``CCC_HOME``.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from command_center import config
from command_center.store import Store

_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "retire_vault_files.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("retire_vault_files", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rvf = _load()


def _tasks(tmp_path: Path) -> Path:
    return tmp_path / "vault" / "01-llm" / "ccc-tasks"


@pytest.fixture(autouse=True)
def _mirror_roots_in_raw_toml(tmp_path: Path) -> None:
    """The mirror root keys left ``Config`` (tp#2266 B10): the tool reads them from the raw
    config.toml (as on a live install whose file still carries them), so pin them under
    the tmp vault there — never the historic ``~/obsidian`` defaults."""
    tasks = _tasks(tmp_path)
    cfg_file = config.config_path()
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(
        "".join(
            f'{key} = "{tasks / name}"\n'
            for key, name in (
                ("running_dir", "running"),
                ("done_dir", "done"),
                ("sessions_dir", "sessions"),
            )
        ),
        encoding="utf-8",
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _mirror(kind: str, sid: str | None = None) -> str:
    return f"---\nccc_mirror: {kind}\nsession_id: {sid or uuid.uuid4()}\n---\n\n# AIM\nx\n"


def _job(sid: str | None = None) -> str:
    return f'---\nid: "abc"\nsession_id: "{sid or uuid.uuid4()}"\nstatus: future\n---\n# AIM\n'


_DASH = "---\nccc_generated: true\n---\n```dataviewjs\n```\n"
_FOREIGN = "---\ntitle: my own note\n---\nhand-written\n"


def _mirror_tree(tmp_path: Path) -> dict[str, Path]:
    tasks = _tasks(tmp_path)
    return {
        "run": _write(tasks / "running" / "llms" / "ccc" / "a-1.md", _mirror("running")),
        "done": _write(tasks / "done" / "home" / "tp" / "b-2.md", _mirror("done")),
        "sess": _write(tasks / "sessions" / "home" / "tp" / "c-3.md", _mirror("session")),
        "dash_run": _write(tasks / "running.md", _DASH),
        "dash_park": _write(tasks / "parked.md", _DASH),
        "dash_done": _write(tasks / "done" / "done.md", _DASH),
        "foreign": _write(tasks / "sessions" / "home" / "notes.md", _FOREIGN),
        "pad": _write(tasks / "new-prompt.md", "---\nsession_id: \n---\n"),
    }


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _archive_dir(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def _main(tmp_path: Path, *args: str) -> int:
    code = rvf.main([*args, "-a", str(_archive_dir(tmp_path))])
    assert isinstance(code, int)
    return code


def _archives(tmp_path: Path) -> list[Path]:
    adir = _archive_dir(tmp_path)
    return sorted(adir.glob("*.tar.gz")) if adir.exists() else []


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "-h"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    for flag in ("-k", "-a", "-x"):
        assert flag in result.stdout
    assert os.access(_SCRIPT, os.X_OK)


def test_dry_run_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _mirror_tree(tmp_path)
    before = _snapshot(tmp_path)
    assert _main(tmp_path, "-k", "mirrors") == 0
    assert _snapshot(tmp_path) == before
    assert not _archive_dir(tmp_path).exists()
    out = capsys.readouterr().out
    assert "6 file(s) to archive" in out
    assert "dry run" in out


def test_execute_archives_then_removes_and_keeps_foreign(tmp_path: Path) -> None:
    files = _mirror_tree(tmp_path)
    expected = {k: p.read_bytes() for k, p in files.items()}
    assert _main(tmp_path, "-k", "mirrors", "-x") == 0
    for key in ("run", "done", "sess", "dash_run", "dash_park", "dash_done"):
        assert not files[key].exists(), key
    assert files["foreign"].read_bytes() == expected["foreign"]  # no marker: kept
    assert files["pad"].exists()
    tasks = _tasks(tmp_path)
    assert not (tasks / "running").exists()  # emptied root removed
    assert not (tasks / "done").exists()
    assert (tasks / "sessions" / "home").is_dir()  # still holds the foreign note
    assert not (tasks / "sessions" / "home" / "tp").exists()
    (archive,) = _archives(tmp_path)
    assert archive.stat().st_mode & 0o777 == 0o600
    vault = tmp_path / "vault"
    with tarfile.open(archive, "r:gz") as tar:
        names = sorted(tar.getnames())
        for key in ("run", "done", "sess", "dash_run", "dash_park", "dash_done"):
            member = tar.extractfile(files[key].relative_to(vault).as_posix())
            assert member is not None
            assert member.read() == expected[key]
    assert len(names) == 6
    assert "01-llm/ccc-tasks/sessions/home/notes.md" not in names


def test_archive_written_and_verified_before_any_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _mirror_tree(tmp_path)
    events: list[str] = []
    real_verify = rvf.verify_archive
    real_unlink = Path.unlink

    def _verify(path: Path, take: list[object]) -> list[str]:
        events.append("verify")
        return list(real_verify(path, take))

    def _unlink(self: Path, missing_ok: bool = False) -> None:
        if self.suffix == ".md":
            assert self.exists()
            events.append("unlink")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(rvf, "verify_archive", _verify)
    monkeypatch.setattr(Path, "unlink", _unlink)
    assert _main(tmp_path, "-k", "mirrors", "-x") == 0
    assert events[0] == "verify"
    assert events.count("unlink") == 6
    assert not files["run"].exists()


def test_truncated_archive_aborts_with_nothing_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _mirror_tree(tmp_path)
    before = _snapshot(tmp_path / "vault")
    real_write = rvf.write_archive

    def _truncating(path: Path, take: list[object]) -> None:
        real_write(path, take)
        size = path.stat().st_size
        with path.open("r+b") as handle:
            handle.truncate(size // 2)

    monkeypatch.setattr(rvf, "write_archive", _truncating)
    assert _main(tmp_path, "-k", "mirrors", "-x") == 1
    assert _snapshot(tmp_path / "vault") == before
    assert "nothing removed" in capsys.readouterr().err


def test_archive_missing_member_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mirror_tree(tmp_path)
    before = _snapshot(tmp_path / "vault")
    real_write = rvf.write_archive
    monkeypatch.setattr(rvf, "write_archive", lambda path, take: real_write(path, take[:-1]))
    assert _main(tmp_path, "-k", "mirrors", "-x") == 1
    assert _snapshot(tmp_path / "vault") == before


def test_source_changed_after_archiving_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _mirror_tree(tmp_path)
    real_write = rvf.write_archive

    def _then_edit(path: Path, take: list[object]) -> None:
        real_write(path, take)
        files["run"].write_text(_mirror("running") + "late write\n", encoding="utf-8")

    monkeypatch.setattr(rvf, "write_archive", _then_edit)
    before = set(_snapshot(tmp_path / "vault"))
    assert _main(tmp_path, "-k", "mirrors", "-x") == 1
    assert set(_snapshot(tmp_path / "vault")) == before  # every file still there
    assert files["run"].read_text(encoding="utf-8").endswith("late write\n")


def test_rerun_after_success_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _mirror_tree(tmp_path)
    assert _main(tmp_path, "-k", "mirrors", "-x") == 0
    after = _snapshot(tmp_path)
    capsys.readouterr()
    assert _main(tmp_path, "-k", "mirrors", "-x") == 0
    assert _snapshot(tmp_path) == after
    assert len(_archives(tmp_path)) == 1
    assert "nothing to retire" in capsys.readouterr().out


def test_existing_archive_never_overwritten(tmp_path: Path) -> None:
    _mirror_tree(tmp_path)
    adir = _archive_dir(tmp_path)
    adir.mkdir()
    first = rvf.archive_path(adir, "mirrors", "2026-11-18")
    first.write_bytes(b"older archive")
    assert rvf.archive_path(adir, "mirrors", "2026-11-18").name == (
        "ccc-tasks-mirrors-2026-11-18-2.tar.gz"
    )
    assert first.read_bytes() == b"older archive"


def test_archive_dir_inside_vault_refused(tmp_path: Path) -> None:
    _mirror_tree(tmp_path)
    before = _snapshot(tmp_path / "vault")
    inside = tmp_path / "vault" / "archive"
    assert rvf.main(["-k", "mirrors", "-x", "-a", str(inside)]) == 2
    assert _snapshot(tmp_path / "vault") == before
    assert not inside.exists()


def test_mirrors_refused_while_a_switch_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mirror_tree(tmp_path)
    loaded = config.load_config
    # The switch is gone from ``Config`` (tp#2266 B10); an older ccc's config object that
    # still carries it must still be refused.
    monkeypatch.setattr(
        config, "load_config", lambda: SimpleNamespace(**vars(loaded()), mirror_sessions=True)
    )
    before = _snapshot(tmp_path)
    assert _main(tmp_path, "-k", "mirrors", "-x") == 2
    assert _snapshot(tmp_path) == before


# --------------------------------------------------------------------------- #
# -k future
# --------------------------------------------------------------------------- #
def _future_tree(tmp_path: Path) -> dict[str, Path]:
    tasks = _tasks(tmp_path)
    return {
        "job": _write(tasks / "future" / "llms" / "ccc" / "j-1.md", _job()),
        "arch": _write(tasks / "future" / "_archive" / "llms" / "j-2.md", _job()),
        "trash": _write(tasks / "delete" / "j-3.md", _job()),
        "dash_future": _write(tasks / "future.md", _DASH),
        "dash_delete": _write(tasks / "delete" / "delete.md", _DASH),
        "foreign": _write(tasks / "future" / "ideas.md", _FOREIGN),
        "bad_id": _write(tasks / "future" / "x.md", '---\nsession_id: "nope"\n---\n'),
        "pad": _write(tasks / "new-prompt.md", "---\nsession_id: \n---\n"),
        "mirror": _write(tasks / "running" / "a.md", _mirror("running")),
    }


def _store_with_draft(future_file: str | None) -> None:
    with Store(config.db_path()) as store:
        sid = str(uuid.uuid4())
        store.create_draft(sid, "/tmp", "an aim")
        if future_file is not None:
            store.update_fields(sid, future_file=future_file)


def test_future_refused_while_a_draft_has_future_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _future_tree(tmp_path)
    _store_with_draft("01-llm/ccc-tasks/future/llms/ccc/j-1.md")
    before = _snapshot(tmp_path / "vault")
    assert _main(tmp_path, "-k", "future", "-x") == 2
    assert _main(tmp_path, "-k", "future") == 2
    assert _snapshot(tmp_path / "vault") == before
    assert "--detach" in capsys.readouterr().err


def test_future_refused_while_future_files_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _future_tree(tmp_path)
    _store_with_draft(None)
    loaded = config.load_config
    monkeypatch.setattr(
        config, "load_config", lambda: dataclasses.replace(loaded(), future_files=True)
    )
    before = _snapshot(tmp_path / "vault")
    assert _main(tmp_path, "-k", "future", "-x") == 2
    assert _snapshot(tmp_path / "vault") == before
    assert "future_files is on" in capsys.readouterr().err


def test_future_refused_without_store(tmp_path: Path) -> None:
    _future_tree(tmp_path)
    assert not config.db_path().exists()
    assert _main(tmp_path, "-k", "future", "-x") == 2


def test_future_execute_takes_jobs_and_dashboards_only(tmp_path: Path) -> None:
    files = _future_tree(tmp_path)
    _store_with_draft(None)
    assert _main(tmp_path, "-k", "future", "-x") == 0
    for key in ("job", "arch", "trash", "dash_future", "dash_delete"):
        assert not files[key].exists(), key
    for key in ("foreign", "bad_id", "pad", "mirror"):
        assert files[key].exists(), key
    tasks = _tasks(tmp_path)
    assert not (tasks / "delete").exists()
    assert not (tasks / "future" / "_archive").exists()
    (archive,) = _archives(tmp_path)
    assert archive.name.startswith("ccc-tasks-future-")
    with tarfile.open(archive, "r:gz") as tar:
        assert len(tar.getnames()) == 5
    assert _main(tmp_path, "-k", "future", "-x") == 0  # re-run: no-op
    assert len(_archives(tmp_path)) == 1


def test_classify_reasons(tmp_path: Path) -> None:
    md = _write(tmp_path / "x" / "a.md", _mirror("running"))
    assert rvf.classify(md, "mirrors") is None
    assert rvf.classify(md, "future") is None  # a mirror carries a UUID session_id too
    other = _write(tmp_path / "x" / "b.md", "---\nccc_mirror: elsewhere\n---\n")
    assert rvf.classify(other, "mirrors") == "no ccc_mirror marker"
    txt = _write(tmp_path / "x" / "c.txt", _mirror("running"))
    assert rvf.classify(txt, "mirrors") == "not a .md file"
    link = tmp_path / "x" / "d.md"
    link.symlink_to(md)
    assert rvf.classify(link, "mirrors") == "symlink"
    plain = _write(tmp_path / "x" / "e.md", "no frontmatter\n")
    assert rvf.classify(plain, "mirrors") == "no frontmatter"
    quoted = _write(tmp_path / "x" / "f.md", "---\ntags: [a]\nccc_mirror: 'session'\n---\n")
    assert rvf.classify(quoted, "mirrors") is None
    nested = _write(tmp_path / "x" / "g.md", "---\nmeta:\n  ccc_mirror: running\n---\n")
    assert rvf.classify(nested, "mirrors") == "no ccc_mirror marker"  # top level only
    body_only = _write(tmp_path / "x" / "h.md", "# t\n---\nccc_generated: true\n---\n")
    assert rvf.classify(body_only, "mirrors") == "no frontmatter"
