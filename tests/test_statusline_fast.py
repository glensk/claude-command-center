"""The lean ``ccc statusline`` path answers exactly like ``cli.cmd_statusline`` (tp#1806 B3).

Four proofs:

* **parity** — a matrix of fixture homes rendered in-process by ``cli.main`` and by
  ``entrypoint.main`` under one frozen clock: stdout, exit code, the usage-cache JSON and
  the role-mark file are identical, and the fast path never reached ``cli``;
* **lean imports** — a served statusline in a fresh interpreter loads none of the heavy
  modules;
* **fall-through** — every case the fast path declines reaches ``cli`` with the argv and
  the stdin text intact;
* **defaults** — the session view's and the config read's defaults equal ``Session``'s and
  ``config.DEFAULTS``.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from command_center import cli, config, entrypoint
from command_center.models import Session
from command_center.store import Store

ROOT = Path(__file__).resolve().parents[1]
SID = "sess-fast"
OTHER = "sess-other"
NOW = 1_900_000_000
CWD = "/repo/fast"
DEFAULT_ARGV = ("statusline", "--session", SID, "--capture-usage")
RATE_LIMITS = {
    "five_hour": {"used_percentage": 12.5, "resets_at": NOW + 3 * 3600},
    "seven_day": {"used_percentage": 40, "resets_at": NOW + 4 * 86400},
}
PAYLOAD = json.dumps({"session_id": SID, "cwd": CWD, "rate_limits": RATE_LIMITS})


# ---- fixtures ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _real_accounts_and_staff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``cli`` resolves accounts from config.toml (as the fast path does); a tmp staff root."""
    monkeypatch.setattr(
        config,
        "claude_config_dirs",
        lambda: config.parse_claude_accounts(config.load_config().claude_accounts),
    )
    staff = tmp_path / "staff" / "mail-agent"
    staff.mkdir(parents=True)
    (staff / "agent.yaml").write_text(
        'schema_version: 1\nname: mail-agent\nsymbol: "📧"\nglyph: "\\uf198"\n'
        "tab_name: mail desk\n",
        encoding="utf-8",
    )
    (tmp_path / "tp-cache" / "session-role").mkdir(parents=True)
    monkeypatch.setenv("TP_STAFF_ROOT", str(tmp_path / "staff"))
    monkeypatch.setenv("TP_CACHE_DIR", str(tmp_path / "tp-cache"))
    monkeypatch.delenv("CCC_NO_FASTPATH", raising=False)


def _claude_home() -> Path:
    return config.claude_home()


def _app_home() -> Path:
    return config.app_home()


def _store() -> Store:
    return Store(config.db_path())


def _registry(home: Path, pid: object, sid: str = SID, *, file: str = "", **extra: object) -> None:
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    entry: dict[str, object] = {
        "pid": pid,
        "sessionId": sid,
        "cwd": CWD,
        "status": "idle",
        "updatedAt": 5,
    }
    entry.update(extra)
    name = file or f"{pid}-{sid}"
    (sessions / f"{name}.json").write_text(json.dumps(entry), encoding="utf-8")


def _dead_pid() -> int:
    with subprocess.Popen([sys.executable, "-c", ""]) as proc:  # noqa: S603
        proc.wait()
    return proc.pid


def _transcript(home: Path, records: list[dict]) -> None:
    folder = home / "projects" / CWD.replace("/", "-")
    folder.mkdir(parents=True, exist_ok=True)
    lines = "".join(json.dumps(r) + "\n" for r in records)
    (folder / f"{SID}.jsonl").write_text(lines, encoding="utf-8")


def _config(text: str) -> None:
    _app_home().mkdir(parents=True, exist_ok=True)
    (_app_home() / "config.toml").write_text(text, encoding="utf-8")
    config.invalidate_config_cache()


def _with_aim(store: Store, aim: str = "ship the lean status line: tp#1806") -> None:
    store.ensure(SID, cwd=CWD)
    store.set_aim(SID, aim)


# ---- the parity matrix ------------------------------------------------------------------


def _case_no_row() -> None:
    with _store() as store:
        store.ensure(OTHER, cwd=CWD)


def _case_aim_unset() -> None:
    with _store() as store:
        store.ensure(SID, cwd=CWD)
        store.update_fields(SID, next_step="first line\nsecond line")


def _case_aim_subgoals() -> None:
    with _store() as store:
        _with_aim(store)
        store.set_subgoals(SID, ["one", "two", "three"])
        first = store.list_subgoals(SID)[0]
        store.set_subgoal_checked(first.id, True)


def _case_manual_progress() -> None:
    with _store() as store:
        _with_aim(store)
        store.update_fields(SID, manual_progress=40)


def _case_aim_met() -> None:
    with _store() as store:
        _with_aim(store)
        store.set_subgoals(SID, ["one", "two"])
        store.set_aim_met(SID, True, "verified", 1)


def _case_history_anchor() -> None:
    with _store() as store:
        _with_aim(store, "first goal typed by hand")
        store.set_aim(SID, "second sharper goal")
        store.set_aim(SID, "third goal: tp#1, #2")
        store.update_fields(SID, aim_prev=None)


def _case_history_transition() -> None:
    with _store() as store:
        _with_aim(store, "old goal")
        store.set_aim(SID, "new goal with more words")


def _case_score_low() -> None:
    with _store() as store:
        _with_aim(store)
        store.update_fields(SID, aim_score=10)


def _case_unscored() -> None:
    with _store() as store:
        _with_aim(store)
        store.update_fields(SID, aim_score=-1)


def _case_score_history_off() -> None:
    _config("aim_score = false\naim_history = false\naim_score_threshold = 90\n")
    with _store() as store:
        _with_aim(store, "old goal")
        store.set_aim(SID, "new goal")
        store.update_fields(SID, aim_score=10)


def _case_threshold() -> None:
    _config("aim_score_threshold = 95\n")
    with _store() as store:
        _with_aim(store)
        store.update_fields(SID, aim_score=80)


def _case_staff(tmp_path: Path) -> None:
    (tmp_path / "tp-cache" / "session-role" / SID).write_text("mail-agent\n", encoding="utf-8")
    with _store() as store:
        _with_aim(store, "answer mail: tp#123, #456")


def _case_staff_no_row(tmp_path: Path) -> None:
    (tmp_path / "tp-cache" / "session-role" / SID).write_text("mail-agent\n", encoding="utf-8")
    _case_no_row()


def _case_todos() -> None:
    todos = [["completed", "a done thing"], ["in_progress", "b" * 30], ["pending", "c"]]
    with _store() as store:
        _with_aim(store)
        store.update_fields(SID, todos=json.dumps(todos))


def _case_drift() -> None:
    with _store() as store:
        _with_aim(store)
        store.update_fields(SID, drift_severity="medium", drift_reason="off track", drift_ack_at=0)


def _fire_case(offset: int) -> Callable[[], None]:
    def build() -> None:
        with _store() as store:
            _with_aim(store)
            store.update_fields(SID, fire_at=NOW + offset)

    return build


def _case_armed() -> None:
    with _store() as store:
        _with_aim(store)
        for index, offset in enumerate((2 * 3600, 5 * 3600)):
            draft = f"draft-{index}"
            store.ensure(draft, cwd=CWD)
            store.update_fields(draft, draft=True, fire_at=NOW + offset)


def _live_case(status: str, *, alive: bool = True, **extra: str) -> Callable[[], None]:
    def build() -> None:
        with _store() as store:
            _with_aim(store)
            store.update_fields(SID, status="parked")
        pid = os.getpid() if alive else _dead_pid()
        _registry(_claude_home(), pid, status=status, **extra)

    return build


def _case_halted() -> None:
    _live_case("busy")()
    _transcript(
        _claude_home(),
        [
            {"type": "user", "message": {"content": "go"}},
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {"content": [{"type": "text", "text": "You've hit your session limit"}]},
            },
        ],
    )


def _case_done_busy() -> None:
    _live_case("busy")()
    with _store() as store:
        store.update_fields(SID, done=True)


def _case_handed_off() -> None:
    _live_case("idle")()
    with _store() as store:
        store.update_fields(SID, handed_off_to="successor")


def _case_sdk_ignored() -> None:
    _live_case("busy", entrypoint="sdk-cli")()


def _case_other_live() -> None:
    _live_case("busy")()
    _registry(_claude_home(), os.getpid(), sid=OTHER, status="waiting")


def _case_two_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "claude-work"
    work.mkdir()
    _config(f'claude_accounts = ["private={_claude_home()}", "work={work}"]\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work))
    with _store() as store:
        _with_aim(store)
    _registry(work, os.getpid(), status="waiting")
    _registry(_claude_home(), _dead_pid(), status="busy", updatedAt=99)  # dead: no conflict


def _case_unknown_account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    _case_aim_subgoals()


def _case_seeded_usage() -> None:
    _case_aim_subgoals()
    _app_home().mkdir(parents=True, exist_ok=True)
    seeded = {
        "captured_at": NOW - 10,
        "five_hour": {"used_percentage": 50.0, "resets_at": NOW + 3 * 3600},
        "seven_day": None,
        "oauth_fetched_at": NOW - 60,
        "oauth_backoff_until": 7,
    }
    (_app_home() / "usage.json").write_text(json.dumps(seeded), encoding="utf-8")


CASES: dict[str, Callable[..., None]] = {
    "no_row": _case_no_row,
    "aim_unset": _case_aim_unset,
    "aim_subgoals": _case_aim_subgoals,
    "manual_progress": _case_manual_progress,
    "aim_met": _case_aim_met,
    "history_anchor": _case_history_anchor,
    "history_transition": _case_history_transition,
    "score_low": _case_score_low,
    "unscored": _case_unscored,
    "score_history_off": _case_score_history_off,
    "threshold": _case_threshold,
    "staff": _case_staff,
    "staff_no_row": _case_staff_no_row,
    "todos": _case_todos,
    "drift": _case_drift,
    "fire_future": _fire_case(3 * 3600),
    "fire_grace": _fire_case(-30),
    "fire_overdue": _fire_case(-3600),
    "armed": _case_armed,
    "live_busy": _live_case("busy"),
    "live_idle": _live_case("idle"),
    "live_waiting": _live_case("waiting"),
    "live_shell": _live_case("shell"),
    "live_dead": _live_case("busy", alive=False),
    "halted": _case_halted,
    "done_busy": _case_done_busy,
    "handed_off": _case_handed_off,
    "sdk_ignored": _case_sdk_ignored,
    "other_live": _case_other_live,
    "two_accounts": _case_two_accounts,
    "unknown_account": _case_unknown_account,
    "seeded_usage": _case_seeded_usage,
}


def _effects(tmp_path: Path) -> dict[str, bytes]:
    """Every file the statusline may write: usage caches and role-mark files."""
    out: dict[str, bytes] = {}
    for folder in (_app_home(), tmp_path / "ccc-role-mark"):
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.name.startswith("usage") or folder.name == "ccc-role-mark":
                    out[str(path)] = path.read_bytes()
    return out


def _restore(tmp_path: Path, snapshot: dict[str, bytes]) -> None:
    for name in _effects(tmp_path):
        Path(name).unlink()
    for name, data in snapshot.items():
        Path(name).write_bytes(data)


def _fail_cli(argv: list[str] | None = None) -> int:
    raise AssertionError(f"fast path fell through to cli: {argv}")


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    main: Callable[[list[str]], int],
    argv: tuple[str, ...],
    stdin: str,
) -> tuple[int, bytes]:
    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: float(NOW))
        patch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(list(argv))
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, captured.out.encode()


def _parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...] = DEFAULT_ARGV,
    stdin: str = PAYLOAD,
) -> tuple[bytes, dict[str, bytes]]:
    before = _effects(tmp_path)
    cli_result = _run(monkeypatch, capsys, cli.main, argv, stdin)
    cli_effects = _effects(tmp_path)
    _restore(tmp_path, before)
    monkeypatch.setattr(cli, "main", _fail_cli)
    fast_result = _run(monkeypatch, capsys, entrypoint.main, argv, stdin)
    assert fast_result == cli_result
    assert _effects(tmp_path) == cli_effects
    return fast_result[1], cli_effects


@pytest.mark.parametrize("case", sorted(CASES))
def test_parity_matrix(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    build = CASES[case]
    wanted = build.__code__.co_varnames[: build.__code__.co_argcount]
    available = {"tmp_path": tmp_path, "monkeypatch": monkeypatch}
    build(**{name: available[name] for name in wanted})
    out, effects = _parity(tmp_path, monkeypatch, capsys)
    assert out  # every case with a session id prints rows
    usage_files = [name for name in effects if "/usage" in name and name.endswith(".json")]
    assert usage_files or case == "unknown_account"


def test_parity_staff_writes_role_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _case_staff(tmp_path)
    out, effects = _parity(tmp_path, monkeypatch, capsys)
    assert "\U0001f3ad mail" in out.decode()  # role marker + glyph, then the tab name
    assert "#123, #456" in out.decode()
    assert effects[str(tmp_path / "ccc-role-mark" / SID)] == "\uf198\n".encode()


def test_parity_two_accounts_writes_the_work_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _case_two_accounts(tmp_path, monkeypatch)
    out, effects = _parity(tmp_path, monkeypatch, capsys)
    assert "waiting_input" in out.decode()
    assert {Path(name).name.split("-")[1] for name in effects} == {"work"}  # cache + its lock


@pytest.mark.parametrize(
    "argv",
    [
        ("statusline", "--capture-usage"),
        ("statusline", "--capture-usage", "--session="),
        ("statusline",),
        ("statusline", "--session", "first", "--session=" + SID),
        ("statusline", "--session", SID),
    ],
)
def test_parity_argv_forms(
    argv: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _case_aim_subgoals()
    out, _ = _parity(tmp_path, monkeypatch, capsys, argv)
    assert out


@pytest.mark.parametrize(
    "stdin",
    [
        json.dumps({"rate_limits": RATE_LIMITS}),
        "",
        "not json",
        "[1, 2]",
        json.dumps({"session_id": 7, "rate_limits": RATE_LIMITS}),
    ],
)
def test_parity_no_session_id(
    stdin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No id anywhere: the usage write only (when the payload has one), no output, exit 0."""
    out, effects = _parity(tmp_path, monkeypatch, capsys, ("statusline", "--capture-usage"), stdin)
    assert out == b""
    assert bool(effects) == ("rate_limits" in stdin)


def test_parity_without_capture_leaves_stdin_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _case_aim_subgoals()
    out, effects = _parity(tmp_path, monkeypatch, capsys, ("statusline", "--session", SID))
    assert out and not effects


# ---- lean imports -----------------------------------------------------------------------

FORBIDDEN = (
    "command_center.cli",
    "command_center.config",
    "command_center.models",
    "command_center.store",
    "command_center.usage",
    "command_center.park",
    "command_center.tabsymbol",
    "command_center.accounts",
    "command_center.adapters",
    "dataclasses",
    "argparse",
    "rich",
    "textual",
)

_PROBE = """
import json, sys
from command_center import entrypoint
try:
    code = entrypoint.main(sys.argv[2:])
except SystemExit as exc:
    code = exc.code
sys.stdout.flush()
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump({"code": code, "modules": sorted(sys.modules)}, fh)
"""


def _forbidden(module: str) -> bool:
    return any(module == name or module.startswith(name + ".") for name in FORBIDDEN)


def _pythonpath(env: Mapping[str, str]) -> str:
    """ROOT prepended to *env*'s PYTHONPATH, so the conftest child guard stays on it."""
    return os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)


def _probe(tmp_path: Path, stdin: str, *argv: str) -> tuple[dict, bytes]:
    report = tmp_path / "report.json"
    drop = {"CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR", "CCC_NO_FASTPATH"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = _pythonpath(env)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROBE, str(report), *argv],
        input=stdin.encode(),
        capture_output=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=True,
    )
    return json.loads(report.read_text(encoding="utf-8")), proc.stdout


def test_served_statusline_loads_no_heavy_module(tmp_path: Path) -> None:
    (tmp_path / "tp-cache" / "session-role" / SID).write_text("mail-agent\n", encoding="utf-8")
    _config("aim_score_threshold = 60\n")
    _case_halted()
    with _store() as store:
        store.update_fields(SID, todos=json.dumps([["pending", "x"]]), fire_at=NOW)
    report, out = _probe(tmp_path, PAYLOAD, *DEFAULT_ARGV)
    assert report["code"] == 0
    assert "halted" in out.decode()
    assert (_app_home() / "usage.json").is_file()
    assert (tmp_path / "ccc-role-mark" / SID).is_file()
    modules = report["modules"]
    assert "command_center.livestatus" in modules
    assert [m for m in modules if _forbidden(m)] == []


def test_kill_switch_reaches_cli_in_a_real_process(tmp_path: Path) -> None:
    _case_aim_subgoals()
    served, served_out = _probe(tmp_path, PAYLOAD, *DEFAULT_ARGV)
    os.environ["CCC_NO_FASTPATH"] = "1"
    try:
        report = tmp_path / "report.json"
        env = dict(os.environ, PYTHONPATH=_pythonpath(os.environ))
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", _PROBE, str(report), *DEFAULT_ARGV],
            input=PAYLOAD.encode(),
            capture_output=True,
            env=env,
            cwd=tmp_path,
            timeout=60,
            check=True,
        )
    finally:
        del os.environ["CCC_NO_FASTPATH"]
    slow = json.loads(report.read_text(encoding="utf-8"))
    assert "command_center.cli" in slow["modules"]
    assert "command_center.cli" not in served["modules"]
    assert (slow["code"], proc.stdout) == (served["code"], served_out)


def test_bad_registry_pid_exits_like_cli_in_a_real_process(tmp_path: Path) -> None:
    """Rule 6: cli's own ValueError (exit 1) is reproduced, not masked."""
    _case_aim_subgoals()
    _registry(_claude_home(), "not-a-number", file="badpid")
    report = tmp_path / "report.json"
    drop = {"CLAUDE_CONFIG_DIR", "CCC_NO_FASTPATH"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = _pythonpath(env)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "command_center", *DEFAULT_ARGV],
        input=PAYLOAD.encode(),
        capture_output=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 1
    assert b"ValueError" in proc.stderr
    assert proc.stdout == b""
    assert not report.exists()


# ---- fall-through -----------------------------------------------------------------------


def _fallthrough(
    monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...], stdin: str = PAYLOAD
) -> tuple[list[str], str]:
    """Run ``entrypoint.main``; return the argv and the stdin text ``cli`` received."""
    seen: list[tuple[list[str], str]] = []

    def fake_cli(args: list[str] | None = None) -> int:  # pylint: disable=unused-argument
        seen.append((list(args or []), sys.stdin.read()))
        return 0

    monkeypatch.setattr(cli, "main", fake_cli)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    assert entrypoint.main(list(argv)) == 0
    assert len(seen) == 1, "the fast path answered instead of cli"
    return seen[0]


@pytest.mark.parametrize(
    "tail",
    [
        ("-h",),
        ("--help",),
        ("--sess", SID),
        ("--session", SID, "--capture"),
        ("--print-glyph",),
        ("--session", SID, "--print-glyph"),
        ("--session",),
        ("--session", "-x"),
        ("--session", SID, "extra"),
        ("--capture-usage=1",),
        ("--unknown",),
    ],
)
def test_rule1_other_argv_reaches_cli(
    tail: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _case_aim_subgoals()
    argv = ("statusline", *tail)
    assert _fallthrough(monkeypatch, argv) == (list(argv), PAYLOAD)


def test_rule2_missing_db_reaches_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not config.db_path().exists()
    assert _fallthrough(monkeypatch, DEFAULT_ARGV) == (list(DEFAULT_ARGV), PAYLOAD)


def test_rule2_old_schema_reaches_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    config.db_path().parent.mkdir(parents=True)
    con = sqlite3.connect(config.db_path())
    con.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, aim TEXT)")
    con.execute("INSERT INTO sessions VALUES (?, 'x')", (SID,))
    con.commit()
    con.close()
    assert _fallthrough(monkeypatch, DEFAULT_ARGV) == (list(DEFAULT_ARGV), PAYLOAD)


def test_rule2_corrupt_db_reaches_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    config.db_path().parent.mkdir(parents=True)
    config.db_path().write_bytes(b"this is not a database" * 100)
    assert _fallthrough(monkeypatch, DEFAULT_ARGV) == (list(DEFAULT_ARGV), PAYLOAD)


@pytest.mark.parametrize(
    "extra",
    [{"effort": "high"}, {"effortLevel": "low"}, {"model": {"reasoning_effort": "xhigh"}}],
)
def test_rule3_effort_payload_reaches_cli(extra: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    _case_aim_subgoals()
    payload = json.dumps({"session_id": SID, **extra})
    assert _fallthrough(monkeypatch, DEFAULT_ARGV, payload) == (list(DEFAULT_ARGV), payload)


def test_rule4_live_conflict_reaches_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "claude-work"
    work.mkdir()
    _config(f'claude_accounts = ["private={_claude_home()}", "work={work}"]\n')
    _case_aim_subgoals()
    # The conflict is on ANOTHER id: cli's discover() warns for every group.
    _registry(_claude_home(), os.getpid(), sid=OTHER, file="a")
    _registry(work, os.getpid(), sid=OTHER, file="b")
    assert _fallthrough(monkeypatch, DEFAULT_ARGV) == (list(DEFAULT_ARGV), PAYLOAD)


@pytest.mark.parametrize("text", ["this is [[ not toml", "aim_score = \n"])
def test_rule5_unparsable_config_reaches_cli(text: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _case_aim_subgoals()
    _config(text)
    assert _fallthrough(monkeypatch, DEFAULT_ARGV) == (list(DEFAULT_ARGV), PAYLOAD)


@pytest.mark.parametrize(
    "setup",
    ["bad_pid", "bad_threshold", "bad_accounts"],
)
def test_rule6_any_read_error_reaches_cli(setup: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _case_aim_subgoals()
    if setup == "bad_pid":
        _registry(_claude_home(), "abc", file="badpid")
    elif setup == "bad_threshold":
        _config('aim_score_threshold = "high"\n')
    else:
        _config("claude_accounts = 3\n")
    assert _fallthrough(monkeypatch, DEFAULT_ARGV) == (list(DEFAULT_ARGV), PAYLOAD)


@pytest.mark.parametrize(
    "argv",
    [DEFAULT_ARGV, ("aim", "--session", SID), ("statusline", "--capture-usage")],
)
def test_rule7_kill_switch_reaches_cli(
    argv: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _case_aim_subgoals()
    monkeypatch.setenv("CCC_NO_FASTPATH", "1")
    assert _fallthrough(monkeypatch, argv) == (list(argv), PAYLOAD)


def test_fallthrough_without_stdin_read_keeps_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--session`` without ``--capture-usage``: stdin is never touched, cli still has it."""
    argv = ("statusline", "--session", SID, "--print-glyph")
    stream = io.StringIO(PAYLOAD)
    seen: list[object] = []

    def fake_cli(args: list[str] | None = None) -> int:  # pylint: disable=unused-argument
        seen.append(sys.stdin)
        return 0

    monkeypatch.setattr(cli, "main", fake_cli)
    monkeypatch.setattr(sys, "stdin", stream)
    assert entrypoint.main(list(argv)) == 0
    assert seen == [stream] and stream.tell() == 0


def test_unreadable_stdin_falls_through_with_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(io.StringIO):
        def read(self, size: int | None = -1) -> str:
            raise OSError("gone")

    seen: list[str] = []

    def fake_cli(args: list[str] | None = None) -> int:  # pylint: disable=unused-argument
        seen.append(sys.stdin.read())
        return 0

    monkeypatch.setattr(cli, "main", fake_cli)
    monkeypatch.setattr(sys, "stdin", Broken())
    assert entrypoint.main(list(DEFAULT_ARGV)) == 0  # no DB: falls through
    assert seen == [""]


# ---- argv parser and defaults -----------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "parsed"),
    [
        ([], (None, False)),
        (["--capture-usage"], (None, True)),
        (["--session", "s"], ("s", False)),
        (["--session=s", "--capture-usage"], ("s", True)),
        (["--capture-usage", "--session", "s"], ("s", True)),
        (["--session", "a", "--session=b"], ("b", False)),
        (["--session="], ("", False)),
        (["--session", ""], ("", False)),
        (["--session", "a=b"], ("a=b", False)),
    ],
)
def test_parse_statusline_argv_accepts(argv: list[str], parsed: tuple[str | None, bool]) -> None:
    assert entrypoint.parse_statusline_argv(argv) == parsed


@pytest.mark.parametrize(
    "argv",
    [["-h"], ["--session"], ["--session", "-1"], ["--sess=s"], ["--print-glyph"], ["s"], ["--"]],
)
def test_parse_statusline_argv_declines(argv: list[str]) -> None:
    with pytest.raises(entrypoint._Unsupported):  # pylint: disable=protected-access
        entrypoint.parse_statusline_argv(argv)


def test_session_view_defaults_equal_session_defaults() -> None:
    fields = {f.name: f for f in dataclasses.fields(Session)}
    names = (*entrypoint.STATUSLINE_COLUMNS, "first_aim", "first_short_aim")
    for name in names:
        field = fields[name]
        assert field.default is not dataclasses.MISSING, name
        assert getattr(entrypoint.SessionView, name) == field.default, name


def test_session_view_covers_every_rendered_attribute() -> None:
    """Every attribute ``slrender.SessionView`` declares (plus the status inputs) is read."""
    import inspect  # pylint: disable=import-outside-toplevel

    from command_center import slrender  # pylint: disable=import-outside-toplevel

    source = inspect.getsource(slrender)
    protocol = source.split("class SessionView(Protocol):", 1)[1].split("\n\n\n", 1)[0]
    declared = {
        line.split("def ", 1)[1].split("(", 1)[0]
        for line in protocol.splitlines()
        if line.strip().startswith("def ")
    }
    read = {*entrypoint.STATUSLINE_COLUMNS, "first_aim", "first_short_aim"}
    assert declared <= read
    assert {"status", "done", "handed_off_to"} <= read


def test_config_defaults_equal_config_defaults() -> None:
    for key, value in entrypoint.STATUSLINE_CONFIG_DEFAULTS.items():
        assert config.DEFAULTS[key] == value, key


def test_view_reads_the_shared_first_aim_sql() -> None:
    from command_center import slrender, store  # pylint: disable=import-outside-toplevel

    assert slrender.FIRST_AIM_SELECT in store._SESSION_SELECT  # pylint: disable=protected-access
