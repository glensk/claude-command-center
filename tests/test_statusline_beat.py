"""The statusline fast path as a ``last_beat`` source (tp#2372 F3b6, Design 7).

``ccc statusline --session X`` (served by :mod:`command_center.entrypoint`, never by
``cli``) writes the session's shared ``last_beat`` under the hook beat's 60 s throttle:
off by default (no shared file touched), a fresh beat writes nothing, a stale one is
written once with one timing row, a held write lock, a fenced store, a stale epoch or no
pinned epoch skips it, neither path imports ``config`` or ``shared_store``, and the output
and exit status never change.
"""

from __future__ import annotations

# pylint: disable=redefined-outer-name  # pytest fixtures
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from leanprobe import ENTRYPOINT_PROBE_HEAD, child_pythonpath, forbidden
from test_shared_store import SHARED_DDL

from command_center import config, entrypoint, shared_store
from command_center.store import Store

SID = "5b0c7a52-1f3e-4d6a-9c1b-2a7e0f4d8c11"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway CLAUDE_HOME with a ``state.db``; returns the app home."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("CCC_HOME", raising=False)
    monkeypatch.delenv("CCC_NO_FASTPATH", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TP_STAFF_ROOT", str(tmp_path / "staff"))
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp-cache"))
    monkeypatch.setenv("TP_TIMING_LOG", str(tmp_path / "timing.jsonl"))
    with Store(config.db_path()) as store:  # epoch 1 pinned, as after a hook's first write
        shared_store.ensure_private(store.conn)
        store.conn.execute("INSERT INTO shared_store_pins (key, value) VALUES ('epoch', '1')")
        store.conn.commit()
    return config.app_home()


def _shared_file(tmp_path: Path, *, last_beat: int | None, row: bool = True) -> Path:
    path = tmp_path / "shared.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(SHARED_DDL)
    if row:
        conn.execute(
            "INSERT INTO ccc_threads (thread_id, kind, root_session, created_at) "
            "VALUES ('t1', 'ccc', ?, 1)",
            (SID,),
        )
        conn.execute(
            "INSERT INTO ccc_sessions (session_id, thread_id, generation, started_at, last_beat)"
            " VALUES (?, 't1', 0, 1, ?)",
            (SID, last_beat),
        )
    conn.commit()
    conn.close()
    return path


def _config(app_home: Path, shared: Path | None) -> None:
    app_home.mkdir(parents=True, exist_ok=True)
    text = f'shared_store_path = "{shared}"\n' if shared is not None else ""
    (app_home / "config.toml").write_text(text, encoding="utf-8")
    config.invalidate_config_cache()


def _beat(shared: Path) -> Any:
    conn = sqlite3.connect(shared)
    try:
        return conn.execute(
            "SELECT last_beat FROM ccc_sessions WHERE session_id = ?", (SID,)
        ).fetchone()[0]
    finally:
        conn.close()


def _serve(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = entrypoint.main(["statusline", "--session", SID])
    return code, capsys.readouterr().out


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_the_throttle_and_guards_are_the_hook_beats() -> None:
    assert entrypoint.BEAT_INTERVAL_MS == shared_store.BEAT_INTERVAL_MS
    assert shared_store.BEAT_BUSY_MS == 0
    assert entrypoint.SHARED_COMPONENT_KEY == shared_store.COMPONENT_KEY
    assert entrypoint.SHARED_COMPONENT_VERSION == shared_store.COMPONENT_VERSION
    assert entrypoint.SHARED_BACKEND_KEY == shared_store.BACKEND_KEY
    assert entrypoint.SHARED_MAINTENANCE == shared_store.MAINTENANCE
    assert entrypoint.SHARED_EPOCH_KEY == shared_store.EPOCH_KEY
    assert f"CREATE TABLE IF NOT EXISTS {entrypoint.SHARED_PINS_TABLE} (" in (
        shared_store.PRIVATE_SCHEMA
    )
    assert entrypoint.STATUSLINE_CONFIG_DEFAULTS["shared_store_path"] == ""


def test_store_off_reads_no_beat_path() -> None:
    assert entrypoint._beat_path({"shared_store_path": ""}) == ""  # pylint: disable=protected-access
    assert entrypoint._beat_path({"shared_store_path": 3}) == ""  # pylint: disable=protected-access
    assert entrypoint._beat_path({}) == ""  # pylint: disable=protected-access


def test_store_off_touches_no_shared_file(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = _shared_file(tmp_path, last_beat=None)
    _config(home, None)
    calls: list[str] = []
    monkeypatch.setattr(entrypoint, "_statusline_beat", lambda *a: calls.append(a[2]))
    code, _ = _serve(capsys)
    assert code == 0
    assert not calls
    assert _beat(shared) is None


def test_a_stale_beat_is_written_once_inside_60_s(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = _shared_file(tmp_path, last_beat=1)
    _config(home, None)
    off_code, off_out = _serve(capsys)
    _config(home, shared)
    before = _now_ms()
    code, out = _serve(capsys)
    assert (code, out) == (off_code, off_out)  # the beat never changes the output
    first = int(_beat(shared))
    assert first >= before
    code, _ = _serve(capsys)
    assert code == 0
    assert int(_beat(shared)) == first  # inside 60 s: nothing written
    rows = [
        json.loads(line)
        for line in (tmp_path / "timing.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(r["kind"], r["verb"], r["db"]) for r in rows] == [
        ("ccc_write", "beat_statusline", str(shared))
    ]
    assert rows[0]["hold_ms"] >= 0


def test_a_fresh_beat_writes_nothing(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh = _now_ms() - 30_000
    shared = _shared_file(tmp_path, last_beat=fresh)
    _config(home, shared)
    assert _serve(capsys)[0] == 0
    assert _beat(shared) == fresh
    assert not (tmp_path / "timing.jsonl").exists()


def test_a_held_write_lock_skips_the_beat(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = _shared_file(tmp_path, last_beat=1)
    _config(home, shared)
    holder = sqlite3.connect(shared, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        code, _ = _serve(capsys)
        elapsed = time.perf_counter() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert code == 0
    assert elapsed < 1.0  # 0 s busy timeout: no wait for the lock
    assert _beat(shared) == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [("backend", "maintenance"), ("ccc_shared_schema", "2"), ("holder_authority_epoch", "2")],
)
def test_a_fenced_store_writes_nothing(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str, value: str
) -> None:
    shared = _shared_file(tmp_path, last_beat=1)
    conn = sqlite3.connect(shared)
    conn.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
    conn.commit()
    conn.close()
    _config(home, shared)
    assert _serve(capsys)[0] == 0
    assert _beat(shared) == 1


def test_nothing_pinned_yet_writes_nothing(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The statusline never pins the epoch; the guarded hook writes do."""
    shared = _shared_file(tmp_path, last_beat=1)
    with Store(config.db_path()) as store:
        store.conn.execute("DELETE FROM shared_store_pins")
        store.conn.commit()
    _config(home, shared)
    assert _serve(capsys)[0] == 0
    assert _beat(shared) == 1


def test_no_row_or_no_file_writes_nothing(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = _shared_file(tmp_path, last_beat=None, row=False)
    _config(home, shared)
    assert _serve(capsys)[0] == 0
    _config(home, tmp_path / "missing.sqlite3")
    assert _serve(capsys)[0] == 0
    assert not (tmp_path / "missing.sqlite3").exists()
    conn = sqlite3.connect(shared)
    assert conn.execute("SELECT COUNT(*) FROM ccc_sessions").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("stale", [False, True], ids=["throttled", "writing"])
def test_a_beat_loads_no_heavy_module(home: Path, tmp_path: Path, stale: bool) -> None:
    """Both the throttled and the writing run stay stdlib-only: no config, no shared_store."""
    seeded = 1 if stale else _now_ms()
    shared = _shared_file(tmp_path, last_beat=seeded)
    _config(home, shared)
    report = tmp_path / "modules.json"
    probe = ENTRYPOINT_PROBE_HEAD + (
        "json.dump({'code': code, 'modules': sorted(sys.modules)}, open(sys.argv[1], 'w'))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath(env)
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe, str(report), "statusline", "--session", SID],
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["code"] == 0
    loaded = [m for m in data["modules"] if forbidden(m) or m == "command_center.shared_store"]
    assert not loaded
    assert (int(_beat(shared)) != seeded) is stale
