"""Tests for ``command_center.codex_switch`` — the Codex ``/switch`` planner.

Everything here is hermetic: the seat registry, the oracle and the e-mail lookup are
monkeypatched, ``HOME`` points at ``tmp_path`` so the pop of ``$CODEX_HOME`` inside
:func:`codex_switch.seats` can never reach the developer's real ``~/.codex``, and the
launch-record directory is redirected under ``tmp_path``.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center import codex_switch as cs
from command_center import config
from command_center.snapshot import PsRow

THREAD = "01a0909f-4ece-7900-a64f-2f29d0cf53ea"
RELATIVE = Path("sessions/2026/09/11") / f"rollout-2026-09-11T15-19-14-{THREAD}.jsonl"


@pytest.fixture(autouse=True)
def _records_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cs, "record_dir", lambda: _mk(tmp_path / "records"))
    monkeypatch.setattr(cs, "_log", lambda *_: None)


def _mk(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# trigger + hook input
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (" /switch", (True, "", False)),
        ("/switch", (True, "", False)),
        ("//switch", (True, "", False)),
        ("/switch/", (True, "", False)),
        ("switch", (True, "", False)),
        ("SWITCH de", (True, "de", False)),
        (" /switch gl! ", (True, "gl", True)),
        ("/switch albert@example.org", (True, "albert@example.org", False)),
        (" /switch de/quit", (False, "", False)),  # a stray `/quit` never looks like a seat
        ("/switch de extra", (False, "", False)),
        ("switch the cluster to the new node pool", (False, "", False)),
        ("please /switch", (False, "", False)),
        ("", (False, "", False)),
    ],
)
def test_switch_target(prompt: str, expected: tuple[bool, str, bool]) -> None:
    assert cs.switch_target(prompt) == expected


def test_parse_hook_input_reads_the_fields() -> None:
    raw = json.dumps(
        {
            "session_id": THREAD,
            "turn_id": "t",
            "transcript_path": "/x/sessions/2026/09/11/rollout-1-abc.jsonl",
            "cwd": "/repo",
            "hook_event_name": "UserPromptSubmit",
            "prompt": " /switch",
        }
    )
    hook = cs.parse_hook_input(raw)
    assert (hook.session_id, hook.cwd, hook.prompt) == (THREAD, "/repo", " /switch")


@pytest.mark.parametrize("raw", ["", "not json", "[1]", json.dumps({"hook_event_name": "Stop"})])
def test_parse_hook_input_refuses_junk(raw: str) -> None:
    with pytest.raises(ValueError):
        cs.parse_hook_input(raw)


def test_home_of_transcript() -> None:
    assert cs.home_of_transcript("/Users/a/.codex/sessions/2026/09/11/rollout-x.jsonl") == Path(
        "/Users/a/.codex"
    )
    assert cs.home_of_transcript("relative/sessions/2026/09/11/r.jsonl") is None
    assert cs.home_of_transcript("/Users/a/.codex/other/2026/09/11/r.jsonl") is None
    assert cs.home_of_transcript("") is None


# --------------------------------------------------------------------------- #
# seats
# --------------------------------------------------------------------------- #
def _three(tmp_path: Path) -> list[cs.Seat]:
    return [
        cs.Seat("default", tmp_path / ".codex"),
        cs.Seat("private", tmp_path / ".codex-private"),
        cs.Seat("de", tmp_path / ".codex-de"),
    ]


def test_seat_aliases_parse_tolerantly() -> None:
    assert cs.seat_aliases(["work=default", "GL=private", "junk", "=x", "y=", "gl=de"]) == {
        "work": "default",
        "gl": "private",
    }


def test_codex_seat_aliases_is_a_known_config_key() -> None:
    assert "codex_seat_aliases" in config.DEFAULTS
    assert config.load_config().codex_seat_aliases == []


def test_resolve_seat_by_label_alias_and_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = _three(tmp_path)
    aliases = {"work": "default", "gl": "private"}
    monkeypatch.setattr(
        cs, "seat_email", lambda seat: {"de": "a.de@example.org"}.get(seat.label, "")
    )
    assert cs.resolve_seat("de", seats, aliases).label == "de"
    assert cs.resolve_seat("WORK", seats, aliases).label == "default"
    assert cs.resolve_seat("gl", seats, aliases).label == "private"
    assert cs.resolve_seat("A.DE@example.org", seats, aliases).label == "de"
    with pytest.raises(cs.SwitchError, match="unknown seat 'nope'"):
        cs.resolve_seat("nope", seats, aliases)


def test_next_seat_follows_the_oracle_and_skips_the_current(tmp_path: Path) -> None:
    seats = _three(tmp_path)
    assert cs.next_seat(seats[0], seats, ["default", "de", "private"]).label == "de"
    assert cs.next_seat(seats[2], seats, ["de", "private"]).label == "private"
    with pytest.raises(cs.SwitchError, match="no other seat has headroom"):
        cs.next_seat(seats[0], seats, ["default"])
    with pytest.raises(cs.SwitchError, match="no other seat"):
        cs.next_seat(seats[0], seats, [])


def test_seats_drops_codex_home_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "pinned"))
    monkeypatch.setattr(config, "codex_homes", lambda: {"default": tmp_path / ".codex"})
    assert [seat.label for seat in cs.seats()] == ["default"]
    assert "CODEX_HOME" not in os.environ


# --------------------------------------------------------------------------- #
# process discovery + argv/env
# --------------------------------------------------------------------------- #
def _table() -> dict[int, PsRow]:
    return {
        1: PsRow(ppid=0, tty="", stat="Ss", command="/sbin/launchd"),
        100: PsRow(ppid=1, tty="ttys006", stat="Ss", command="-zsh"),
        200: PsRow(
            ppid=100, tty="ttys006", stat="S+", command="python3 tp.py open 210 -H -e codex"
        ),
        300: PsRow(ppid=200, tty="ttys006", stat="S+", command="codex -C /repo -c x=y prompt"),
        350: PsRow(ppid=300, tty="ttys006", stat="S", command="/x/bin/codex-code-mode-host"),
        400: PsRow(ppid=300, tty="ttys006", stat="S", command="python3 hooks/codex-switch-hook.py"),
        500: PsRow(ppid=400, tty="ttys006", stat="S", command="ccc codex-switch"),
    }


def test_find_codex_pid_walks_to_the_outermost_exact_program() -> None:
    table = _table()
    assert cs.find_codex_pid(500, table) == 300
    assert cs.find_codex_pid(350, table) == 300  # the helper is not codex
    assert cs.find_codex_pid(200, table) == 0  # tp mentions codex, is not codex
    table[600] = PsRow(ppid=300, tty="ttys006", stat="S", command="codex app-server")
    assert cs.find_codex_pid(600, table) == 300  # a codex under codex → the outer one
    assert cs.find_codex_pid(999, table) == 0


def test_shell_pid_of_finds_the_tab_shell() -> None:
    assert cs.shell_pid_of(300, _table()) == 100
    assert cs.shell_pid_of(100, _table()) == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="KERN_PROCARGS2 is a macOS sysctl")
def test_procargs_reads_this_very_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_SWITCH_PROBE", "yes")
    exe, argv, env = cs.procargs(os.getpid())
    assert "python" in exe.lower() and argv
    # setenv after exec is invisible to the kernel buffer — that is exactly why
    # env_carry does not diff against the SHELL's buffer.
    assert env.get("CCC_SWITCH_PROBE") is None
    assert "PATH" in env
    with pytest.raises(OSError):
        cs.procargs(0)


def test_relaunch_options_keeps_the_allow_list_in_every_spelling() -> None:
    argv = [
        "codex",
        "--ask-for-approval",
        "never",
        "-C",
        "/repo",
        "-c",
        'default_permissions="hardened-review"',
        "-mgpt-5.6-sol",
        "--model=gpt-5.6-sol",
        "-anever",
        "-cfoo=bar",
        "--search",
        "-i",
        "/tmp/shot.png",
        "--image=/tmp/other.png",
        "Review of tp#210 with spaces\nand a newline",
    ]
    assert cs.relaunch_options(argv) == [
        "--ask-for-approval",
        "never",
        "-C",
        "/repo",
        "-c",
        'default_permissions="hardened-review"',
        "-mgpt-5.6-sol",
        "--model=gpt-5.6-sol",
        "-anever",
        "-cfoo=bar",
        "--search",
    ]


def test_relaunch_options_keeps_options_after_resume_and_drops_its_selectors() -> None:
    argv = ["codex", "-m", "x", "resume", "--last", "--all", "-c", "a=b", "-C", "/w", "old-id"]
    assert cs.relaunch_options(argv) == ["-m", "x", "-c", "a=b", "-C", "/w"]
    assert cs.relaunch_options(["codex", "resume", THREAD, "--include-non-interactive"]) == []
    assert cs.relaunch_options(["codex"]) == []


@pytest.mark.parametrize(
    ("argv", "match"),
    [
        (["codex", "exec", "--json", "do it"], "not an interactive TUI"),
        (["codex", "-m", "x", "review"], "not an interactive TUI"),
        (["codex", "--frobnicate", "p"], "unknown codex option --frobnicate"),
        (["codex", "-z", "p"], "unknown codex option -z"),
        (["codex", "--remote", "ws://h:1", "p"], "remote app-server"),
        (["codex", "resume", "--remote-auth-token-env=T", THREAD], "remote app-server"),
        (["codex", "-m"], "has no value"),
    ],
)
def test_relaunch_options_refuses(argv: list[str], match: str) -> None:
    with pytest.raises(cs.SwitchError, match=match):
        cs.relaunch_options(argv)


def test_option_value_takes_the_last_spelling() -> None:
    names = frozenset({"-p", "--profile"})
    assert cs.option_value(["-p", "one", "--profile=two", "-pthree"], names) == "three"
    assert cs.option_value(["-m", "x"], names) == ""


def test_env_carry_keeps_launcher_families_only() -> None:
    codex_env = {
        "AI_PUSH_LOCAL_ONLY": "1",
        "TP_REVIEW_CODEX_EFFORT": "xhigh",
        "CODEX_HOME": "/Users/a/.codex",  # replaced, never carried
        "CODEX_SQLITE_HOME": "/elsewhere",  # storage routing — never
        "CODEX_IN_CLAUDE_IGNORE_QUOTA": "1",
        "CODEX_ACCESS_TOKEN": "x",  # secret-shaped
        "OPENAI_API_KEY": "sk-secret",  # not a family, and secret-shaped
        "CCC_INTERNAL": "1",  # never
        "HOMEBREW_PREFIX": "/opt/homebrew",  # a .zshrc export: not a launcher family
        "TP_SAME": "same",
        "TP_CTRL": "a\nb",  # control character
        "AI_LONG": "x" * 3000,
    }
    assert cs.env_carry(codex_env, {"TP_SAME": "same"}) == {
        "AI_PUSH_LOCAL_ONLY": "1",
        "CODEX_IN_CLAUDE_IGNORE_QUOTA": "1",
        "TP_REVIEW_CODEX_EFFORT": "xhigh",
    }


def test_profile_mismatch(tmp_path: Path) -> None:
    src, dst = _mk(tmp_path / "src"), _mk(tmp_path / "dst")
    assert cs.profile_mismatch(["-m", "x"], src, dst) == ""
    assert "no " in cs.profile_mismatch(["-p", "rev"], src, dst)
    (src / "rev.config.toml").write_text("a = 1\n", encoding="utf-8")
    (dst / "rev.config.toml").write_text("a = 2\n", encoding="utf-8")
    assert "differs" in cs.profile_mismatch(["--profile=rev"], src, dst)
    (dst / "rev.config.toml").write_text("a = 1\n", encoding="utf-8")
    assert cs.profile_mismatch(["-prev"], src, dst) == ""
    (dst / "rev.config.toml").unlink()
    (dst / "rev.config.toml").symlink_to(src / "rev.config.toml")
    assert cs.profile_mismatch(["-p", "rev"], src, dst) == ""


# --------------------------------------------------------------------------- #
# launch records, markers, trampoline
# --------------------------------------------------------------------------- #
def _record(tmp_path: Path, **overrides: object) -> cs.LaunchRecord:
    fields: dict[str, object] = {
        "token": "0123456789abcdef",
        "thread_id": THREAD,
        "exe": "/Users/a/.local/bin/codex",
        "options": ["-C", "/repo", "-c", 'default_permissions="hardened-review"'],
        "env": {"AI_PUSH_LOCAL_ONLY": "1"},
        "source_home": str(tmp_path / ".codex"),
        "target_home": str(tmp_path / ".codex-de"),
        "source_label": "default",
        "target_label": "de",
        "transcript": str(RELATIVE),
        "created": 1,
    }
    fields.update(overrides)
    return cs.LaunchRecord(**fields)  # type: ignore[arg-type]


def test_record_round_trip_is_private_and_pruned(tmp_path: Path) -> None:
    record = _record(tmp_path)
    stale = cs.record_dir() / "deadbeefdeadbeef.json"
    stale.write_text("{}", encoding="utf-8")
    os.utime(stale, (1, 1))
    path = cs.write_record(record)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert cs.read_record(record.token) == record
    assert not stale.exists()
    with pytest.raises(cs.SwitchError, match="malformed launch token"):
        cs.read_record("../etc/passwd")
    with pytest.raises(cs.SwitchError, match="no usable launch record"):
        cs.read_record("feedfacefeedface")


def test_exec_argv_env_pins_the_seat_and_strips_routing_names(tmp_path: Path) -> None:
    record = _record(tmp_path)
    base = {"PATH": "/bin", "CODEX_HOME": "/stale", "CODEX_SQLITE_HOME": "/x", "TERM": "xterm"}
    argv, env = cs.exec_argv_env(record, base_env=base)
    assert argv == ["codex", *record.options, "resume", THREAD]
    assert env == {
        "PATH": "/bin",
        "TERM": "xterm",
        "AI_PUSH_LOCAL_ONLY": "1",
        "CODEX_HOME": str(tmp_path / ".codex-de"),
    }
    assert cs.exec_argv_env(record, source=True, base_env=base)[1]["CODEX_HOME"] == str(
        tmp_path / ".codex"
    )
    assert cs.exec_line(record.token) == "ccc codex-switch-exec 0123456789abcdef"
    assert cs.exec_line(record.token, source=True).endswith(" -S")


def test_pending_marker_expires() -> None:
    cs.mark_pending(THREAD, "0123456789abcdef")
    assert cs.pending_token(THREAD) == "0123456789abcdef"
    os.utime(cs.pending_path(THREAD), (1, 1))
    assert cs.pending_token(THREAD) == ""
    cs.clear_pending(THREAD)
    assert not cs.pending_path(THREAD).exists()


# --------------------------------------------------------------------------- #
# rollout port
# --------------------------------------------------------------------------- #
META = json.dumps({"type": "session_meta", "payload": {"id": THREAD}}) + "\n"
USER = json.dumps({"type": "response_item", "payload": {"role": "user", "content": "hi"}}) + "\n"


def _limits(percent: int, reached: str | None = None) -> str:
    return (
        json.dumps(
            {
                "timestamp": "2026-09-11T13:29:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 5}},
                    "rate_limits": {
                        "primary": {"used_percent": percent},
                        "rate_limit_reached_type": reached,
                    },
                },
            }
        )
        + "\n"
    )


def _write(home: Path, lines: list[str]) -> Path:
    path = home / RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def test_normalize_rollout_line_nulls_rate_limits_only() -> None:
    scrubbed = json.loads(cs.normalize_rollout_line(_limits(100, "x")))
    assert scrubbed["payload"]["rate_limits"] is None
    assert scrubbed["payload"]["info"] == {"total_token_usage": {"input_tokens": 5}}
    plain = '{"type":"response_item","payload":{"role":"user","content":"rate_limits in text"}}\n'
    assert cs.normalize_rollout_line(plain) == plain
    assert cs.normalize_rollout_line('{"rate_limits": broken\n') == '{"rate_limits": broken\n'
    assert cs.normalize_rollout_line(_limits(1)) == cs.normalize_rollout_line(_limits(2))


def test_merge_rollout_keeps_destination_bytes_and_scrubs_the_tail() -> None:
    a1, a2, b1, a3 = _limits(10), _limits(20), _limits(30), _limits(40)
    # first hop A→B: nothing at B yet → the whole file scrubbed
    at_b = cs.merge_rollout([META, a1, USER, a2], [])
    assert at_b == [META, cs.normalize_rollout_line(a1), USER, cs.normalize_rollout_line(a2)]
    # B appends its own event; hop back B→A must keep A's ORIGINAL a1/a2 bytes and scrub b1
    back = cs.merge_rollout([*at_b, b1, USER], [META, a1, USER, a2])
    assert back == [META, a1, USER, a2, cs.normalize_rollout_line(b1), USER]
    # and the return trip A→B keeps B's b1 bytes (its own telemetry) and scrubs A's new a3
    again = cs.merge_rollout([*back, a3], [*at_b, b1, USER])
    assert again == [*at_b, b1, USER, cs.normalize_rollout_line(a3)]


def test_merge_rollout_refuses_divergence() -> None:
    with pytest.raises(cs.SwitchError, match="more line"):
        cs.merge_rollout([META], [META, USER])
    with pytest.raises(cs.SwitchError, match="differs .* at line 2"):
        cs.merge_rollout([META, USER], [META, _limits(1)])


def test_port_rollout_mirrors_the_path_ports_the_index_and_sets_aside_divergence(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    src_home, dst_home = Path(record.source_home), Path(record.target_home)
    src = _write(src_home, [META, _limits(100, "x"), USER])
    os.chmod(src, 0o600)
    (src_home / "session_index.jsonl").write_text(
        json.dumps({"id": THREAD, "thread_name": "Review"}) + "\n" + json.dumps({"id": "o"}) + "\n",
        encoding="utf-8",
    )
    dst_home.mkdir()
    ported = cs.port_rollout(record)
    assert ported == dst_home / RELATIVE
    lines = ported.read_text(encoding="utf-8").splitlines(keepends=True)
    assert lines[0] == META and lines[2] == USER
    assert json.loads(lines[1])["payload"]["rate_limits"] is None
    assert oct(ported.stat().st_mode & 0o777) == "0o600"
    assert (dst_home / "session_index.jsonl").read_text(encoding="utf-8") == (
        json.dumps({"id": THREAD, "thread_name": "Review"}) + "\n"
    )
    # the thread comes back with one more turn: the old copy is a prefix → merged in place
    src.write_text(META + _limits(100, "x") + USER + USER, encoding="utf-8")
    assert cs.port_rollout(record).read_text(encoding="utf-8").count("hi") == 2
    # a diverged copy (resumed on the target since) is set aside, never overwritten
    ported.write_text(META + USER + USER + USER + USER, encoding="utf-8")
    with pytest.raises(cs.SwitchError, match="set aside as"):
        cs.port_rollout(record)
    assert not ported.exists()
    assert list(ported.parent.glob("*.switch-diverged-*"))
    with pytest.raises(cs.SwitchError, match="is not the rollout of thread"):
        cs.port_rollout(_record(tmp_path, thread_id="deadbeef-0000"))


def test_port_rollout_refuses_a_live_target_writer_and_a_foreign_sqlite_row(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    src_home, dst_home = Path(record.source_home), Path(record.target_home)
    _write(src_home, [META, USER])
    locks = _mk(dst_home / "thread-writer-locks")
    lock = locks / f"{THREAD}.lock"
    lock.touch()
    assert cs.writer_lock_held(dst_home, THREAD) is False  # a stale file is harmless
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert cs.writer_lock_held(dst_home, THREAD) is True
        with pytest.raises(cs.SwitchError, match="live Codex holds"):
            cs.port_rollout(record)
    finally:
        os.close(fd)
    db = dst_home / "state_5.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table threads (id text primary key, rollout_path text not null)")
    conn.execute("insert into threads values (?, ?)", (THREAD, "/elsewhere/rollout.jsonl"))
    conn.commit()
    conn.close()
    assert cs.sqlite_rollout_path(dst_home, THREAD) == "/elsewhere/rollout.jsonl"
    with pytest.raises(cs.SwitchError, match="different rollout"):
        cs.port_rollout(record)
    conn = sqlite3.connect(db)
    conn.execute("update threads set rollout_path = ?", (str(dst_home / RELATIVE),))
    conn.commit()
    conn.close()
    assert cs.port_rollout(record) == dst_home / RELATIVE
    assert cs.sqlite_rollout_path(tmp_path / "nowhere", THREAD) is None


# --------------------------------------------------------------------------- #
# planning end to end (hermetic)
# --------------------------------------------------------------------------- #
def _fake_procargs(pids: dict[int, tuple[str, list[str], dict[str, str]]]):
    def _procargs(pid: int) -> tuple[str, list[str], dict[str, str]]:
        if pid not in pids:
            raise OSError(3, "No such process")
        return pids[pid]

    return _procargs


def _hook(tmp_path: Path, prompt: str = " /switch") -> cs.HookInput:
    return cs.HookInput(
        session_id=THREAD,
        transcript_path=str(tmp_path / ".codex" / RELATIVE),
        cwd="/repo",
        prompt=prompt,
    )


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ranked: list[str]) -> None:
    seats = _three(tmp_path)
    for seat in seats:
        seat.home.mkdir(exist_ok=True)
        (seat.home / "auth.json").write_text("{}", encoding="utf-8")
    _write(seats[0].home, [META, USER])
    monkeypatch.setattr(cs, "seats", lambda: seats)
    monkeypatch.setattr(
        cs, "seat_aliases", lambda entries=None: {"work": "default", "gl": "private"}
    )
    monkeypatch.setattr(cs, "ranked_labels", lambda: (ranked, ""))
    monkeypatch.setattr(cs, "seat_email", lambda seat: "")
    monkeypatch.setattr(
        cs,
        "procargs",
        _fake_procargs(
            {
                300: (
                    "/Users/a/.local/bin/codex",
                    ["codex", "-m", "gpt-5.6-sol", "a prompt"],
                    {
                        "AI_PUSH_LOCAL_ONLY": "1",
                        "CODEX_HOME": str(tmp_path / ".codex"),
                        "PATH": "/bin",
                    },
                ),
                100: ("/bin/zsh", ["-zsh"], {"PATH": "/bin"}),
            }
        ),
    )


def test_plan_switch_picks_the_oracles_best_other_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch, ["default", "de"])  # private held → not listed
    plan = cs.plan_switch(_hook(tmp_path), "", start_pid=500, table=_table())
    assert (plan.source.label, plan.target.label, plan.codex_pid) == ("default", "de", 300)
    record = plan.record
    assert record.options == ["-C", "/repo", "-m", "gpt-5.6-sol"]
    assert record.env == {"AI_PUSH_LOCAL_ONLY": "1"}
    assert record.transcript == str(RELATIVE)
    assert (record.source_home, record.target_home) == (
        str(tmp_path / ".codex"),
        str(tmp_path / ".codex-de"),
    )
    assert plan.env_names == ("AI_PUSH_LOCAL_ONLY",)
    assert plan.target_note == ""


def test_plan_switch_explicit_seat_needs_force_when_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch, ["default", "de"])
    with pytest.raises(cs.SwitchError, match=r"not available per ccc's seat oracle.*/switch gl!"):
        cs.plan_switch(_hook(tmp_path), "gl", start_pid=500, table=_table())
    plan = cs.plan_switch(_hook(tmp_path), "gl", force=True, start_pid=500, table=_table())
    assert plan.target.label == "private"
    assert "forced past" in plan.target_note


def test_plan_switch_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch, ["default", "de"])
    table = _table()
    with pytest.raises(cs.SwitchError, match="already runs on seat 'default'"):
        cs.plan_switch(_hook(tmp_path), "work", start_pid=500, table=table)
    with pytest.raises(cs.SwitchError, match="unknown seat"):
        cs.plan_switch(_hook(tmp_path), "nope", start_pid=500, table=table)
    with pytest.raises(cs.SwitchError, match="could not find the codex process"):
        cs.plan_switch(_hook(tmp_path), "de", start_pid=200, table=table)
    background = dict(table)
    background[300] = PsRow(ppid=200, tty="ttys006", stat="S", command="codex prompt")
    with pytest.raises(cs.SwitchError, match="not the foreground process"):
        cs.plan_switch(_hook(tmp_path), "de", start_pid=500, table=background)
    with pytest.raises(cs.SwitchError, match="names no thread id"):
        cs.plan_switch(cs.HookInput("", "", "/repo", " /switch"), "de", start_pid=500, table=table)
    with pytest.raises(cs.SwitchError, match="no usable transcript_path"):
        cs.plan_switch(
            cs.HookInput(THREAD, "/nope/sessions/1/2/3/r.jsonl", "/repo", ""),
            "de",
            start_pid=500,
            table=table,
        )
    cs.mark_pending(THREAD, "0123456789abcdef")
    with pytest.raises(cs.SwitchError, match="already in flight"):
        cs.plan_switch(_hook(tmp_path), "de", start_pid=500, table=table)
    cs.clear_pending(THREAD)
    (tmp_path / ".codex-de" / "auth.json").unlink()
    with pytest.raises(cs.SwitchError, match="not logged in"):
        cs.plan_switch(_hook(tmp_path), "de", start_pid=500, table=table)


def test_plan_switch_refuses_a_codex_home_disagreement_and_unregistered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch, ["default", "de"])
    monkeypatch.setattr(
        cs,
        "procargs",
        _fake_procargs({300: ("/x/codex", ["codex"], {"CODEX_HOME": str(tmp_path / ".codex-de")})}),
    )
    with pytest.raises(cs.SwitchError, match="refusing to guess which seat"):
        cs.plan_switch(_hook(tmp_path), "de", start_pid=500, table=_table())
    other = tmp_path / ".codex-other"
    _write(other, [META, USER])
    hook = cs.HookInput(THREAD, str(other / RELATIVE), "/repo", " /switch")
    with pytest.raises(cs.SwitchError, match="not a registered seat"):
        cs.plan_switch(hook, "de", start_pid=500, table=_table())


def test_plan_switch_profile_and_sqlite_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch, ["default", "de"])
    monkeypatch.setattr(
        cs,
        "procargs",
        _fake_procargs(
            {300: ("/x/codex", ["codex", "-p", "rev"], {"CODEX_HOME": str(tmp_path / ".codex")})}
        ),
    )
    (tmp_path / ".codex" / "rev.config.toml").write_text("a = 1\n", encoding="utf-8")
    with pytest.raises(cs.SwitchError, match="profile 'rev' has no"):
        cs.plan_switch(_hook(tmp_path), "de", start_pid=500, table=_table())
    (tmp_path / ".codex-de" / "rev.config.toml").write_text("a = 1\n", encoding="utf-8")
    assert cs.plan_switch(_hook(tmp_path), "de", start_pid=500, table=_table()).record.options == [
        "-C",
        "/repo",
        "-p",
        "rev",
    ]
    conn = sqlite3.connect(tmp_path / ".codex-de" / "state_5.sqlite")
    conn.execute("create table threads (id text primary key, rollout_path text not null)")
    conn.execute("insert into threads values (?, ?)", (THREAD, "/elsewhere.jsonl"))
    conn.commit()
    conn.close()
    with pytest.raises(cs.SwitchError, match="different rollout"):
        cs.plan_switch(_hook(tmp_path), "de", start_pid=500, table=_table())


def test_run_switch_blocks_a_refusal_and_stays_silent_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(tmp_path, monkeypatch, ["default", "de"])
    monkeypatch.setattr("command_center.terminal.ps_table", lambda: _table())
    payload = tmp_path / "hook.json"
    payload.write_text(
        json.dumps(
            {
                "session_id": THREAD,
                "transcript_path": _hook(tmp_path).transcript_path,
                "cwd": "/repo",
                "prompt": " /switch nope",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(input=str(payload), target="", dry_run=False, pid=500)
    assert cs.run_switch(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "unknown seat 'nope'" in out["reason"]
    payload.write_text(
        json.dumps({"session_id": THREAD, "prompt": "fix the tests"}), encoding="utf-8"
    )
    assert cs.run_switch(args) == 0
    assert capsys.readouterr().out == ""


def test_run_switch_dry_run_and_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(tmp_path, monkeypatch, ["default", "de"])
    monkeypatch.setattr("command_center.terminal.ps_table", lambda: _table())
    payload = tmp_path / "hook.json"
    payload.write_text(
        json.dumps(
            {
                "session_id": THREAD,
                "transcript_path": _hook(tmp_path).transcript_path,
                "cwd": "/repo",
                "prompt": " /switch",
            }
        ),
        encoding="utf-8",
    )
    assert cs.run_switch(SimpleNamespace(input=str(payload), target="", dry_run=True, pid=500)) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert reason.startswith("DRY RUN") and f"resume {THREAD}" in reason
    assert "carrying AI_PUSH_LOCAL_ONLY" in reason and "=1" not in reason  # names, never values
    # no tab → refused, nothing spawned, no record
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    spawned: list[list[str]] = []

    def _spawn(argv: list[str]) -> bool:
        spawned.append(argv)
        return True

    monkeypatch.setattr("command_center.spawn.spawn_ccc", _spawn)
    assert (
        cs.run_switch(SimpleNamespace(input=str(payload), target="", dry_run=False, pid=500)) == 0
    )
    assert "no iTerm tab or tmux pane" in json.loads(capsys.readouterr().out)["reason"]
    assert spawned == [] and not list(cs.record_dir().glob("*.json"))
    # a tab → record written, relauncher spawned, pending marker set
    monkeypatch.setenv("ITERM_SESSION_ID", "w1t6p0:ABC")
    assert (
        cs.run_switch(SimpleNamespace(input=str(payload), target="", dry_run=False, pid=500)) == 0
    )
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert reason.startswith("⇄ switching this thread to seat de")
    argv = spawned[0]
    assert argv[0] == "codex-switch-now"
    token = argv[argv.index("--token") + 1]
    assert cs.read_record(token).thread_id == THREAD
    assert argv[argv.index("--pid") + 1] == "300"
    assert argv[argv.index("--hook-pid") + 1] == str(os.getppid())
    assert argv[argv.index("--iterm") + 1] == "w1t6p0:ABC"
    assert cs.pending_token(THREAD) == token
    # a second /switch while that one is in flight is refused
    assert (
        cs.run_switch(SimpleNamespace(input=str(payload), target="", dry_run=False, pid=500)) == 0
    )
    assert "already in flight" in json.loads(capsys.readouterr().out)["reason"]
    assert len(spawned) == 1


def test_tty_free_of_codex_matches_the_program_not_a_mention() -> None:
    busy = _table()
    assert cs.tty_free_of_codex("ttys006", busy) is False  # codex still on it
    for pid in (300, 350, 400, 500):
        del busy[pid]
    assert cs.tty_free_of_codex("ttys006", busy) is False  # tp in the foreground
    del busy[200]
    busy[100] = PsRow(ppid=1, tty="ttys006", stat="Ss+", command="-zsh")
    assert cs.tty_free_of_codex("/dev/ttys006", busy) is True
    assert cs.tty_free_of_codex("", busy) is False


class _FakeTerminal:
    """The slice of ``terminal`` the relauncher touches, scripted per test."""

    def __init__(self, table: dict[int, PsRow]) -> None:
        self.table = table
        self.typed: list[str] = []
        self.gone_after_quit = True

    def ps_table(self) -> dict[int, PsRow]:
        return dict(self.table)

    def iterm_session_tty(self, _iterm: str) -> str:
        return "/dev/ttys006"

    def pid_tty(self, pid: int, table: dict[int, PsRow]) -> str:
        return "/dev/ttys006" if pid in table else ""

    def pid_start(self, _pid: int) -> str:
        return "start"

    def type_into_iterm_session(self, _iterm: str, text: str, *, newline: bool = True) -> bool:
        self.typed.append(text if newline else text + "<no-return>")
        if text == "" and self.typed[-2:-1] == ["/quit<no-return>"] and self.gone_after_quit:
            for pid in (300, 350, 400, 500, 200):
                self.table.pop(pid, None)
            self.table[100] = PsRow(ppid=1, tty="ttys006", stat="Ss+", command="-zsh")
        return True

    def tmux_send_keys(self, _pane: str, text: str, *, newline: bool = True) -> bool:
        self.typed.append(text if newline else text + "<no-return>")
        return True

    def tmux_pane_info(self, _pane: str) -> tuple[int, str] | None:
        return None

    def pid_descends_from(self, *_: object) -> bool:
        return True


def _fast(monkeypatch: pytest.MonkeyPatch, fake: _FakeTerminal) -> None:
    monkeypatch.setattr("command_center.terminal.ps_table", fake.ps_table)
    monkeypatch.setattr("command_center.terminal.iterm_session_tty", fake.iterm_session_tty)
    monkeypatch.setattr("command_center.terminal.pid_tty", fake.pid_tty)
    monkeypatch.setattr("command_center.terminal.pid_start", fake.pid_start)
    monkeypatch.setattr(
        "command_center.terminal.type_into_iterm_session", fake.type_into_iterm_session
    )
    monkeypatch.setattr("command_center.terminal.tmux_send_keys", fake.tmux_send_keys)
    monkeypatch.setattr("command_center.terminal.tmux_pane_info", fake.tmux_pane_info)
    monkeypatch.setattr("command_center.terminal.pid_descends_from", fake.pid_descends_from)
    monkeypatch.setattr(cs, "_pid_gone", lambda pid: pid not in fake.table)
    monkeypatch.setattr(cs, "READY_SETTLE_SEC", 0.0)
    monkeypatch.setattr(cs, "QUIT_WAIT_SEC", 0.3)
    monkeypatch.setattr(cs, "READY_WAIT_SEC", 0.3)
    monkeypatch.setattr(cs, "HOOK_EXIT_WAIT_SEC", 0.3)
    monkeypatch.setattr(cs, "POLL_SEC", 0.01)
    monkeypatch.setattr(cs.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cs.config, "load_config", lambda: SimpleNamespace(notify=[]))


def _now_args(token: str) -> SimpleNamespace:
    return SimpleNamespace(token=token, pid=300, hook_pid=0, iterm="w1t6p0:ABC", tmux_pane="")


def test_run_switch_now_quits_ports_and_types_the_trampoline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(tmp_path)
    cs.write_record(record)
    cs.mark_pending(THREAD, record.token)
    _write(Path(record.source_home), [META, _limits(100, "x"), USER])
    Path(record.target_home).mkdir()
    fake = _FakeTerminal(_table())
    _fast(monkeypatch, fake)
    assert cs.run_switch_now(_now_args(record.token)) == 0
    assert fake.typed == ["/quit<no-return>", "", "ccc codex-switch-exec 0123456789abcdef"]
    assert (Path(record.target_home) / RELATIVE).is_file()
    assert cs.pending_token(THREAD) == ""


def test_run_switch_now_aborts_when_codex_ignores_quit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(tmp_path)
    cs.write_record(record)
    cs.mark_pending(THREAD, record.token)
    fake = _FakeTerminal(_table())
    fake.gone_after_quit = False
    _fast(monkeypatch, fake)
    assert cs.run_switch_now(_now_args(record.token)) == 1
    assert fake.typed == ["/quit<no-return>", ""]  # no signal, nothing else: Codex stays alive
    assert 300 in fake.table
    assert cs.pending_token(THREAD) == ""


def test_run_switch_now_falls_back_to_the_source_seat_after_a_failed_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(tmp_path)
    cs.write_record(record)
    _write(Path(record.source_home), [META, USER])
    _write(Path(record.target_home), [META, USER, USER])  # diverged: the target is ahead
    fake = _FakeTerminal(_table())
    _fast(monkeypatch, fake)
    assert cs.run_switch_now(_now_args(record.token)) == 1
    assert fake.typed == ["/quit<no-return>", "", "ccc codex-switch-exec 0123456789abcdef -S"]
    assert list((Path(record.target_home) / RELATIVE).parent.glob("*.switch-diverged-*"))


def test_run_switch_now_refuses_an_unbound_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record(tmp_path)
    cs.write_record(record)
    fake = _FakeTerminal(_table())
    _fast(monkeypatch, fake)
    args = _now_args(record.token)
    args.pid = 200  # tp, not codex
    assert cs.run_switch_now(args) == 1
    assert fake.typed == []
    assert (
        cs.run_switch_now(
            SimpleNamespace(token="nope", pid=300, hook_pid=0, iterm="x", tmux_pane="")
        )
        == 1
    )


# --------------------------------------------------------------------------- #
# the weekly seat rota (codex_seat_rota): the oracle's verdict, and the override
# --------------------------------------------------------------------------- #
def _configure_rota(tmp_path: Path, entry: str, me: str = "bob") -> None:
    """Register the three fixture seats with ccc AND put one of them on a rota.

    Written into the real ``config.toml`` (under the autouse tmp ``CLAUDE_HOME``) so
    these two tests run against the REAL seat oracle rather than a patched ranking —
    the whole point is that ``/switch`` inherits the rota without knowing about it.
    """
    from command_center import config as _config

    home = _config.app_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f'codex_home_private = "{tmp_path / ".codex-private"}"\n'
        f'codex_homes_extra = ["de={tmp_path / ".codex-de"}"]\n'
        f'codex_seat_rota = ["{entry}"]\n'
        f'codex_seat_rota_me = "{me}"\n',
        encoding="utf-8",
    )
    _config.invalidate_config_cache()


def _rota_entry(label: str, names: str) -> str:
    """A rota entry whose CURRENT week belongs to the first name (live clock, any day)."""
    import datetime as _dt
    import zoneinfo as _zi

    today = _dt.datetime.now(_zi.ZoneInfo("Europe/Zurich")).date()
    monday = today - _dt.timedelta(days=today.weekday())
    return f"{label}={monday.isoformat()}@Europe/Zurich:{names}"


def test_plan_switch_skips_a_seat_on_somebody_elses_week(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The automatic pick never lands on a seat whose week belongs to a colleague."""
    real_ranked = cs.ranked_labels
    _prepare(tmp_path, monkeypatch, [])
    monkeypatch.setattr(cs, "ranked_labels", real_ranked)  # the REAL oracle decides
    _configure_rota(tmp_path, _rota_entry("private", "alice,bob"))
    assert cs.ranked_labels()[0] == ["default", "de"]  # private is off-week
    plan = cs.plan_switch(_hook(tmp_path), "", start_pid=500, table=_table())
    assert plan.target.label == "de"
    assert plan.target_note == ""


def test_forced_switch_note_names_the_rota_not_a_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`<seat>!` stays the human override — and records WHICH block it overrode."""
    real_ranked = cs.ranked_labels
    _prepare(tmp_path, monkeypatch, [])
    monkeypatch.setattr(cs, "ranked_labels", real_ranked)
    _configure_rota(tmp_path, _rota_entry("private", "alice,bob"))
    with pytest.raises(cs.SwitchError, match="not available per ccc's seat oracle"):
        cs.plan_switch(_hook(tmp_path), "gl", start_pid=500, table=_table())
    plan = cs.plan_switch(_hook(tmp_path), "gl", force=True, start_pid=500, table=_table())
    assert plan.target.label == "private"
    assert plan.target_note.startswith("forced past ccc's seat oracle on 'private' (rota: ")
    assert " used by alice)" in plan.target_note


def test_seat_blocker_reads_the_rows_own_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold keeps saying "hold", an unblocked seat says nothing — no rota wording leak."""
    from command_center import quota

    _prepare(tmp_path, monkeypatch, [])
    _configure_rota(tmp_path, _rota_entry("private", "alice,bob"))
    quota.record_block(
        "codex:de",
        blocked_until=int(time.time()) + 3600,
        kind=quota.KIND_HOLD,
        reason="de reserved",
    )
    assert cs.seat_blocker("de") == "hold: de reserved"
    assert cs.seat_blocker("default") == ""  # eligible → no reason to name
