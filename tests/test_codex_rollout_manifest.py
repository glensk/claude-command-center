"""The persistent per-file rollout evidence manifest (tp#2513 B2).

A new process (memo and manifest state cleared) answers unchanged rollouts from
``<app_home>/codex-rollout-evidence-v1.json`` without opening them; anything invalid,
foreign, unreadable or unwritable falls back to a cold parse with the same answer.
"""

# The usage <-> codex_in_claude import cycle predates this file (pylint reports it on the
# first module that imports both); it is not introduced here.
# pylint: disable=cyclic-import

from __future__ import annotations

import builtins
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from command_center import codex_in_claude as cic
from command_center import config, usage

_WINDOWED = {
    "limit_id": "codex",
    "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1782320400},
    "secondary": {"used_percent": 45.0, "window_minutes": 10080, "resets_at": 1782893849},
}
_MALFORMED = {
    "limit_id": "codex",
    "primary": {"used_percent": 3.0, "window_minutes": 0, "resets_at": 1782320400},
    "secondary": None,
}
_REFUSED = {
    "limit_id": "premium",
    "primary": None,
    "secondary": None,
    "rate_limit_reached_type": "workspace_owner_credits_depleted",
}
_SECRET_TEXT = "rollout-body-text-that-must-never-be-persisted"


def _event(rate_limits: dict, ts: str) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": rate_limits},
        }
    )


def _filler() -> str:
    return json.dumps({"type": "response_item", "text": _SECRET_TEXT})


def _rollout(home: Path, name: str, lines: list[str], mtime: int) -> Path:
    day = home / "sessions" / "2026" / "10" / "26"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-26T10-00-00-{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _seat(home: Path) -> dict[str, Path]:
    """A seat with a windowed, a refused, a malformed and a negative (no block) rollout."""
    return {
        "windowed": _rollout(
            home, "windowed", [_filler(), _event(_WINDOWED, "2026-10-20T09:00:00Z")], 1782000001
        ),
        "refused": _rollout(
            home, "refused", [_event(_REFUSED, "2026-10-21T09:00:00Z"), _filler()], 1782000002
        ),
        "malformed": _rollout(
            home, "malformed", [_event(_MALFORMED, "2026-10-19T09:00:00Z")], 1782000003
        ),
        "negative": _rollout(home, "negative", [_filler(), _filler()], 1782000004),
    }


def _new_process() -> None:
    """Forget everything a process keeps in memory: the memo and the loaded manifest."""
    cic._rollout_memo.clear()  # noqa: SLF001
    cic._rollout_manifest_state.update(path=None, disk={})  # noqa: SLF001


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    _new_process()
    config.app_home().mkdir(parents=True, exist_ok=True)
    yield
    _new_process()


def _manifest() -> Path:
    return config.app_home() / "codex-rollout-evidence-v1.json"


def _count_opens(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Wrap ``codex_in_claude.open`` and count opens of rollout files by realpath."""
    opens: dict[str, int] = {}
    real_open = builtins.open

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        name = os.fspath(file)
        if os.path.basename(name).startswith("rollout-"):
            key = os.path.realpath(name)
            opens[key] = opens.get(key, 0) + 1
        return real_open(file, *args, **kwargs)  # pylint: disable=consider-using-with

    monkeypatch.setattr(cic, "open", counting_open, raising=False)
    return opens


def _verdict(home: Path) -> tuple[Any, ...]:
    """Every answer the rollout evidence feeds, for one home."""
    files = sorted(
        (home / "sessions").glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    per_file = tuple(
        (cic._latest_rate_limits_event(p), cic._latest_limit_block(p))  # noqa: SLF001
        for p in files
    )
    return (
        cic._codex_rate_snapshot(home),  # noqa: SLF001
        cic.codex_refusal(home),
        usage._codex_rollout_snapshot(files, 0),  # noqa: SLF001
        per_file,
    )


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _partition(home: Path) -> str:
    return cic._rollout_partition(  # noqa: SLF001
        _real(home / "sessions" / "2026" / "10" / "26" / "rollout-x.jsonl")
    )


# ── round trip ───────────────────────────────────────────────────────────────────────
def test_warm_manifest_reads_no_rollout_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "seat"
    files = _seat(home)
    cold = _verdict(home)
    assert cold[1] is not None  # the refusal is the newest block of the seat
    raw = _manifest().read_text(encoding="utf-8")
    assert _SECRET_TEXT not in raw  # parsed fields only, never rollout text
    payload = json.loads(raw)
    assert payload["version"] == 1
    entries = payload["homes"][_partition(home)]
    assert set(entries) == {_real(p) for p in files.values()}
    # Negative results are persisted too: no rate_limits line at all.
    assert entries[_real(files["negative"])]["rate"] is None
    assert entries[_real(files["negative"])]["refusal"] is None
    assert not list(config.app_home().glob("*.tmp"))

    _new_process()
    opens = _count_opens(monkeypatch)
    assert _verdict(home) == cold
    assert not opens


def test_unchanged_scan_does_not_rewrite(tmp_path: Path) -> None:
    home = tmp_path / "seat"
    _seat(home)
    _verdict(home)
    before = _manifest().stat().st_ino
    _new_process()
    _verdict(home)
    assert _manifest().stat().st_ino == before


# ── invalid manifests: ignored, cold parse, rewritten ────────────────────────────────
def _drop_version(payload: dict, _home: str, _victim: str) -> None:
    payload["version"] = 2


def _bool_version(payload: dict, _home: str, _victim: str) -> None:
    payload["version"] = True


def _homes_list(payload: dict, _home: str, _victim: str) -> None:
    payload["homes"] = []


def _foreign_home(payload: dict, home: str, _victim: str) -> None:
    payload["homes"]["/somewhere/else/.codex"] = payload["homes"].pop(home)


def _bad_fingerprint(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["fingerprint"] = "1,2,3,4"


def _bool_fingerprint(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["fingerprint"][0] = True


def _short_fingerprint(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["fingerprint"].pop()


def _str_percent(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["rate"]["windows"][0]["used_percent"] = "12.5"


def _empty_rate(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["rate"] = {}


def _bool_captured(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["rate"]["captured_at"] = False


def _int_reached_type(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["refusal"]["reached_type"] = 7


def _duplicate_window(payload: dict, home: str, victim: str) -> None:
    windows = payload["homes"][home][victim]["rate"]["windows"]
    windows.append(dict(windows[0]))


def _zero_window(payload: dict, home: str, victim: str) -> None:
    payload["homes"][home][victim]["rate"]["windows"][0]["window_minutes"] = 0


def _relative_path(payload: dict, home: str, victim: str) -> None:
    entries = payload["homes"][home]
    entries[os.path.basename(victim)] = entries.pop(victim)


_WHOLE_FILE: dict[str, Callable[[dict, str, str], None]] = {
    "wrong_version": _drop_version,
    "bool_version": _bool_version,
    "homes_not_a_dict": _homes_list,
    "wrong_home": _foreign_home,
}
_ONE_ENTRY: dict[str, Callable[[dict, str, str], None]] = {
    "fingerprint_str": _bad_fingerprint,
    "fingerprint_bool": _bool_fingerprint,
    "fingerprint_short": _short_fingerprint,
    "percent_str": _str_percent,
    "rate_empty": _empty_rate,
    "captured_bool": _bool_captured,
    "reached_type_int": _int_reached_type,
    "duplicate_window": _duplicate_window,
    "zero_window": _zero_window,
    "relative_path": _relative_path,
}


@pytest.mark.parametrize("case", sorted(_WHOLE_FILE) + sorted(_ONE_ENTRY))
def test_invalid_manifest_is_ignored_and_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    home = tmp_path / "seat"
    files = _seat(home)
    cold = _verdict(home)
    good = json.loads(_manifest().read_text(encoding="utf-8"))
    payload = json.loads(json.dumps(good))
    victim = _real(files["refused"] if case == "reached_type_int" else files["windowed"])
    mutate = _WHOLE_FILE.get(case) or _ONE_ENTRY[case]
    mutate(payload, _partition(home), victim)
    _manifest().write_text(json.dumps(payload), encoding="utf-8")

    _new_process()
    opens = _count_opens(monkeypatch)
    assert _verdict(home) == cold
    if case in _WHOLE_FILE:
        assert set(opens) == {_real(p) for p in files.values()}
    else:
        assert set(opens) == {victim}
    rewritten = json.loads(_manifest().read_text(encoding="utf-8"))
    assert rewritten["homes"][_partition(home)] == good["homes"][_partition(home)]


def test_corrupt_json_is_ignored_and_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "seat"
    files = _seat(home)
    cold = _verdict(home)
    good = _manifest().read_text(encoding="utf-8")
    _manifest().write_text(good[: len(good) // 2], encoding="utf-8")
    _new_process()
    opens = _count_opens(monkeypatch)
    assert _verdict(home) == cold
    assert set(opens) == {_real(p) for p in files.values()}
    assert json.loads(_manifest().read_text(encoding="utf-8")) == json.loads(good)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_manifest_is_ignored_and_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "seat"
    files = _seat(home)
    cold = _verdict(home)
    good = json.loads(_manifest().read_text(encoding="utf-8"))
    _manifest().chmod(0)
    _new_process()
    opens = _count_opens(monkeypatch)
    assert _verdict(home) == cold
    assert set(opens) == {_real(p) for p in files.values()}
    assert json.loads(_manifest().read_text(encoding="utf-8")) == good


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_unwritable_state_dir_falls_back_to_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "seat"
    files = _seat(home)
    app_home = config.app_home()
    app_home.chmod(0o500)
    try:
        cold = _verdict(home)
        assert not _manifest().exists()
        assert not list(app_home.glob("*.tmp"))
        _new_process()
        opens = _count_opens(monkeypatch)
        assert _verdict(home) == cold
        assert set(opens) == {_real(p) for p in files.values()}
    finally:
        app_home.chmod(0o700)


def test_missing_state_dir_falls_back_to_parsing(tmp_path: Path) -> None:
    home = tmp_path / "seat"
    _seat(home)
    config.app_home().rmdir()
    cold = _verdict(home)
    assert not config.app_home().exists()  # a cache write never creates ccc's state dir
    _new_process()
    assert _verdict(home) == cold


# ── invalidation and pruning ─────────────────────────────────────────────────────────
def test_changed_fingerprint_invalidates_only_that_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "seat"
    files = _seat(home)
    _verdict(home)

    target = files["windowed"]
    with target.open("a", encoding="utf-8") as handle:  # append: size moves
        handle.write(_event(_REFUSED, "2026-10-27T09:00:00Z") + "\n")
    os.utime(target, (1782000001, 1782000001))
    _new_process()
    opens = _count_opens(monkeypatch)
    verdict = _verdict(home)
    assert opens == {_real(target): 1}
    assert verdict[1] is not None
    assert verdict[1].captured_at == cic._event_timestamp(  # noqa: SLF001
        {"timestamp": "2026-10-27T09:00:00Z"}, 0
    )
    stored = json.loads(_manifest().read_text(encoding="utf-8"))
    entry = stored["homes"][_partition(home)][_real(target)]
    assert tuple(entry["fingerprint"]) == cic._rollout_fingerprint(target.stat())  # noqa: SLF001


def test_same_size_same_mtime_replacement_is_reparsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "seat"
    files = _seat(home)
    _verdict(home)
    target = files["windowed"]
    st = target.stat()
    replacement = target.read_bytes().replace(b"12.5", b"99.5")
    assert len(replacement) == st.st_size
    tmp = target.with_name("replacement.tmp")
    tmp.write_bytes(replacement)
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, target)
    assert target.stat().st_ino != st.st_ino

    _new_process()
    opens = _count_opens(monkeypatch)
    rate = cic._codex_rate_snapshot(home)  # noqa: SLF001
    assert opens == {_real(target): 1}
    assert rate is not None
    assert rate.windows[300].used_percent == 99.5


def test_files_leaving_the_scan_set_are_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cic, "_CODEX_SCAN_LIMIT", 2)
    home = tmp_path / "seat"
    old = _rollout(home, "old", [_event(_WINDOWED, "2026-10-18T09:00:00Z")], 1782000001)
    mid = _rollout(home, "mid", [_event(_WINDOWED, "2026-10-19T09:00:00Z")], 1782000002)
    new = _rollout(home, "new", [_event(_REFUSED, "2026-10-20T09:00:00Z")], 1782000003)
    cic.codex_refusal(home)

    def stored() -> set[str]:
        payload = json.loads(_manifest().read_text(encoding="utf-8"))
        return set(payload["homes"].get(_partition(home), {}))

    assert stored() == {_real(mid), _real(new)}
    os.utime(old, (1782000009, 1782000009))  # old becomes newest: mid leaves the set
    _new_process()
    cic.codex_refusal(home)
    assert stored() == {_real(old), _real(new)}


# ── two homes ────────────────────────────────────────────────────────────────────────
def test_a_write_for_one_home_keeps_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    files_a = _seat(home_a)
    _seat(home_b)
    _verdict(home_a)
    _verdict(home_b)
    payload = json.loads(_manifest().read_text(encoding="utf-8"))
    assert set(payload["homes"]) == {_partition(home_a), _partition(home_b)}
    b_entries = payload["homes"][_partition(home_b)]

    # A later process changes only home A: B's partition survives the read-merge-write.
    with files_a["negative"].open("a", encoding="utf-8") as handle:
        handle.write(_filler() + "\n")
    os.utime(files_a["negative"], (1782000004, 1782000004))
    _new_process()
    opens = _count_opens(monkeypatch)
    _verdict(home_a)
    assert opens == {_real(files_a["negative"]): 1}
    payload = json.loads(_manifest().read_text(encoding="utf-8"))
    assert payload["homes"][_partition(home_b)] == b_entries
    assert set(payload["homes"]) == {_partition(home_a), _partition(home_b)}


def test_interleaved_writers_lose_only_performance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    files_a = _seat(home_a)
    _seat(home_b)
    # Process X scans A and writes; process Y read the manifest before X's write landed
    # and now writes B from that stale read, which drops X's partition for A.
    verdict_a = _verdict(home_a)
    _manifest().unlink()
    _new_process()
    verdict_b = _verdict(home_b)
    payload = json.loads(_manifest().read_text(encoding="utf-8"))
    assert set(payload["homes"]) == {_partition(home_b)}

    _new_process()
    opens = _count_opens(monkeypatch)
    assert _verdict(home_a) == verdict_a  # A is reparsed: same verdict, more bytes
    assert set(opens) == {_real(p) for p in files_a.values()}
    assert _verdict(home_b) == verdict_b
    payload = json.loads(_manifest().read_text(encoding="utf-8"))
    assert set(payload["homes"]) == {_partition(home_a), _partition(home_b)}
