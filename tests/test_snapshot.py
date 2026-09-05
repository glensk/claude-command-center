"""Unit tests for ``ccc snapshot`` / ``ccc restore-snapshot``.

Everything below iTerm2 is pure, so nothing here touches macOS: no API socket, no
``ps``/``lsof``/``sysctl`` call, no ``iterm2`` import. The layout walk arrives as RAW
dicts, the process facts as fixture ``ps`` text, ``argv`` as a stub callable, and the
split executor is exercised against fake session objects that record their splits.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from command_center import snapshot
from command_center.models import LiveSession

# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #
#: A realistic `ps -axo pid=,ppid=,tty=,stat=,command=` dump.
#:   ttys000 — login shell, `man git` + its `less` child in the foreground
#:   ttys001 — login shell running a live `claude`
#:   ttys002 — a bare shell, nothing in the foreground
#:   ttys003 — the pane running `ccc snapshot` itself (uv → python → ccc → ps)
#:   ttys004 — the ccc TUI (a `#!`-script: python3 → ccc) with a `git` child on top
_PS = """\
    1     0 ??       Ss   /sbin/launchd
 2057     1 ttys000  Ss   /usr/bin/login -fqpl user
 2058  2057 ttys000  S    -zsh
 2100  2058 ttys000  S+   man git
 2101  2100 ttys000  S+   /usr/bin/less -is
 3057     1 ttys001  Ss   /usr/bin/login -fqpl user
 3058  3057 ttys001  S    -zsh
 3100  3058 ttys001  S+   claude --dangerously-skip-permissions
 4057     1 ttys002  Ss   /usr/bin/login -fqpl user
 4058  4057 ttys002  S    -zsh
 5057     1 ttys003  Ss   /usr/bin/login -fqpl user
 5058  5057 ttys003  S    -zsh
 5100  5058 ttys003  S+   uv run ccc snapshot
 5101  5100 ttys003  S+   /opt/py/bin/python3 /home/user/.local/bin/ccc snapshot
 5102  5101 ttys003  R+   ps -axo pid=,ppid=,tty=,stat=,command=
 6057     1 ttys004  Ss   /usr/bin/login -fqpl user
 6058  6057 ttys004  S    -zsh
 6100  6058 ttys004  S+   /opt/py/bin/python3 /home/user/.local/bin/ccc
 6101  6100 ttys004  S+   /usr/bin/git status
"""

_CWDS = {
    2058: "/repo/docs",
    2100: "/repo/docs",
    2101: "/repo/docs",
    3058: "/repo/app",
    3100: "/repo/app",
    4058: "/home/user",
    5058: "/repo/tools",
    5101: "/repo/tools",
    6058: "/repo/ui",
    6100: "/repo/ui",
    6101: "/repo/ui",
}

_ARGV = {
    2100: ["man", "git"],
    2101: ["/usr/bin/less", "-is"],
    5101: ["ccc", "snapshot"],
    6100: ["/opt/py/bin/python3", "/home/user/.local/bin/ccc"],
    6101: ["/usr/bin/git", "status"],
}


def _pane(uuid: str, tty: str, job_pid: int | None, name: str = "", **extra: object) -> dict:
    """One RAW pane dict, as the iTerm collector emits it."""
    raw: dict = {
        "uuid": uuid,
        "tty": tty,
        "job_pid": job_pid,
        "job_name": "",
        "command_line": "",
        "path": "",
        "name": name,
    }
    raw.update(extra)
    return raw


_DEFAULT_FRAME = [0, 0, 1200, 800]


def _window(*trees: dict, frame: object = _DEFAULT_FRAME, selected: int = 0) -> dict:
    """One RAW window whose tabs are the given trees (``frame=None`` = a frameless one)."""
    return {
        "frame": frame,
        "selected_tab": selected,
        "tabs": [{"tree": tree} for tree in trees],
    }


def _live(session_id: str, pid: int, cwd: str, config_dir: str = "") -> LiveSession:
    return LiveSession(pid=pid, session_id=session_id, cwd=cwd, alive=True, config_dir=config_dir)


def _build(
    raw_windows: list[dict],
    live: list[LiveSession] | None = None,
    store_rows: dict[str, str] | None = None,
    args_of: snapshot.ProcArgs = _ARGV.get,
    cwds: dict[int, str] | None = None,
    own_pid: int = 5101,
    allowlist: list[str] | None = None,
) -> dict:
    """``build_snapshot`` over the shared fixtures, with per-test overrides."""
    return snapshot.build_snapshot(
        raw_windows,
        live or [],
        store_rows or {},
        snapshot.parse_ps(_PS),
        args_of,
        dict(_CWDS) if cwds is None else cwds,
        own_pid,
        allowlist=allowlist,
        created_at="2026-08-30T10:11:12+02:00",
    )


def _panes(data: dict) -> list[snapshot.SnapPane]:
    windows = snapshot.windows_from_json(data)
    return [p for w in windows for t in w.tabs for p in snapshot.iter_panes(t.tree)]


# --------------------------------------------------------------------------- #
# KERN_PROCARGS2 parsing
# --------------------------------------------------------------------------- #
def _procargs2(exec_path: str, argv: list[str], padding: int = 3) -> bytes:
    """Build a KERN_PROCARGS2 blob the way the kernel lays it out."""
    blob = len(argv).to_bytes(4, "little")
    blob += exec_path.encode() + b"\0" + b"\0" * padding
    for arg in argv:
        blob += arg.encode() + b"\0"
    return blob


def test_parse_procargs2_reads_exact_argv() -> None:
    raw = _procargs2("/usr/bin/vim", ["vim", "notes.md"])
    assert snapshot.parse_procargs2(raw) == ["vim", "notes.md"]


def test_parse_procargs2_keeps_spaces_and_quotes_inside_one_arg() -> None:
    """Argv elements are NUL-separated, so spaces/quotes survive intact."""
    argv = ["vim", "/repo/my notes/a file.md", 'say "hi"']
    assert snapshot.parse_procargs2(_procargs2("/usr/bin/vim", argv)) == argv


def test_parse_procargs2_tolerates_garbage() -> None:
    assert snapshot.parse_procargs2(b"") == []
    assert snapshot.parse_procargs2(b"\x00\x00\x00\x00abc") == []  # argc 0
    assert snapshot.parse_procargs2(b"\x02\x00\x00\x00nonul") == []  # no exec_path NUL


def test_parse_procargs2_stops_at_argc() -> None:
    """Trailing environment strings after argv are NOT argv."""
    raw = _procargs2("/bin/ls", ["ls", "-l"]) + b"PATH=/usr/bin\0HOME=/home/user\0"
    assert snapshot.parse_procargs2(raw) == ["ls", "-l"]


def test_parse_procargs2_replaces_undecodable_bytes() -> None:
    raw = b"\x01\x00\x00\x00" + b"/bin/x\0\0" + b"\xff\xfe\0"
    assert snapshot.parse_procargs2(raw) == ["��"]


# --------------------------------------------------------------------------- #
# ps / lsof / tty parsing
# --------------------------------------------------------------------------- #
def test_normalize_tty() -> None:
    assert snapshot.normalize_tty("ttys001") == "/dev/ttys001"
    assert snapshot.normalize_tty("/dev/ttys001") == "/dev/ttys001"
    assert snapshot.normalize_tty("??") == ""
    assert snapshot.normalize_tty(None) == ""


def test_parse_ps_keeps_the_command_string_whole() -> None:
    rows = snapshot.parse_ps(_PS)
    assert rows[2100] == snapshot.PsRow(ppid=2058, tty="/dev/ttys000", stat="S+", command="man git")
    assert rows[1].tty == ""  # "??" -> no terminal
    assert rows[5102].command.startswith("ps -axo")


def test_parse_lsof_cwds() -> None:
    text = "p2058\nn/repo/docs\np2100\nn/repo/my notes\n"
    assert snapshot.parse_lsof_cwds(text) == {2058: "/repo/docs", 2100: "/repo/my notes"}


def test_argv0_name_strips_login_dash_and_path() -> None:
    assert snapshot.argv0_name("-zsh") == "zsh"
    assert snapshot.argv0_name("/usr/bin/less -is") == "less"
    assert snapshot.argv0_name("") == ""
    assert snapshot.is_shell_command("-zsh") is True
    assert snapshot.is_shell_command("man git") is False
    # argv0_name stays interpreter-blind — effective_name is the allowlist-matching name.
    assert snapshot.argv0_name("python3.12 /x/bin/ccc") == "python3.12"


def test_effective_name_sees_through_an_interpreter_wrapper() -> None:
    """A `#!`-script is exec'd as `<interpreter> <script>`; the allowlist means the script."""
    assert snapshot.effective_name("python3.12 /x/bin/ccc".split()) == "ccc"
    assert snapshot.effective_name(["/usr/bin/python3", "/home/u/.local/bin/ccc"]) == "ccc"
    assert snapshot.effective_name(["node", "/opt/js/serve.js", "--port", "80"]) == "serve.js"
    assert snapshot.effective_name(["perl", "/usr/bin/rename"]) == "rename"


def test_effective_name_keeps_the_interpreter_when_it_runs_no_script() -> None:
    assert snapshot.effective_name("python3 -m http.server".split()) == "python3"
    assert snapshot.effective_name(["python3"]) == "python3"


def test_effective_name_is_argv0_for_everything_else() -> None:
    assert snapshot.effective_name("vim x".split()) == "vim"
    assert snapshot.effective_name(["-zsh"]) == "zsh"
    assert snapshot.effective_name([]) == ""


# --------------------------------------------------------------------------- #
# Classifier — Claude panes
# --------------------------------------------------------------------------- #
def test_claude_pane_matches_by_pane_uuid_first() -> None:
    """The recorded ``iterm_session_id`` tail is the primary key (tty is the fallback)."""
    live = [_live("sess-1", pid=9999, cwd="/repo/app", config_dir="/acct/work")]
    data = _build(
        [_window({"pane": _pane("AAAA-1111", "/dev/ttys000", 2100)})],
        live=live,
        store_rows={"sess-1": "w0t0p0:aaaa-1111"},
    )
    pane = _panes(data)[0]
    assert (pane.kind, pane.session_id, pane.cwd) == ("claude", "sess-1", "/repo/app")
    assert pane.config_dir == "/acct/work"


def test_claude_pane_matches_by_pid_tty_when_uuid_is_unknown() -> None:
    """No store row (or a stale one) still resolves via the live pid's tty."""
    live = [_live("sess-2", pid=3100, cwd="/repo/app")]
    data = _build([_window({"pane": _pane("ZZZZ", "/dev/ttys001", 3100)})], live=live)
    assert [(p.kind, p.session_id) for p in _panes(data)] == [("claude", "sess-2")]


def test_dead_live_entries_are_not_matched() -> None:
    dead = LiveSession(pid=3100, session_id="gone", cwd="/repo/app", alive=False)
    data = _build([_window({"pane": _pane("ZZZZ", "/dev/ttys001", 3100)})], live=[dead])
    assert _panes(data)[0].kind != "claude"


# --------------------------------------------------------------------------- #
# Classifier — other panes
# --------------------------------------------------------------------------- #
def test_ancestor_rule_prefers_man_over_its_less_child() -> None:
    """`man git` spawns the pager that owns the tty — restore `man git`, not `less`."""
    data = _build([_window({"pane": _pane("P1", "/dev/ttys000", 2101)})])
    pane = _panes(data)[0]
    assert (pane.kind, pane.argv, pane.cwd) == ("command", ["man", "git"], "/repo/docs")


def test_ancestor_rule_matches_a_script_through_its_interpreter() -> None:
    """The ccc TUI is `python3 …/bin/ccc` — the allowlist entry `ccc` must still hit it."""
    rows = snapshot.parse_ps(_PS)
    assert snapshot.allowlisted_ancestor(rows, 6101, 6058, ["ccc"]) == 6100
    assert snapshot.allowlisted_ancestor(rows, 6101, 6058, ["vim"]) == 6101  # nothing matches


def test_capture_lifts_a_child_back_to_the_interpreted_script_and_keeps_full_argv() -> None:
    data = _build([_window({"pane": _pane("P1", "/dev/ttys004", 6101)})], allowlist=["ccc"])
    pane = _panes(data)[0]
    assert pane.kind == "command"
    assert pane.argv == ["/opt/py/bin/python3", "/home/user/.local/bin/ccc"]
    assert pane.cwd == "/repo/ui"


def test_foreground_falls_back_to_the_deepest_plus_process() -> None:
    """With no usable ``jobPid``, the deepest foreground-flagged pid on the tty wins."""
    data = _build([_window({"pane": _pane("P1", "/dev/ttys000", None)})])
    pane = _panes(data)[0]
    # deepest '+' is `less` (2101) -> the ancestor rule lifts it back to `man git`
    assert pane.argv == ["man", "git"]


def test_foreground_ignores_a_stale_job_pid() -> None:
    """A ``jobPid`` naming a process ``ps`` no longer sees falls back to the tty scan."""
    data = _build([_window({"pane": _pane("P1", "/dev/ttys000", 999999)})])
    assert _panes(data)[0].argv == ["man", "git"]


def test_shell_only_pane_records_the_shell_cwd() -> None:
    data = _build([_window({"pane": _pane("P1", "/dev/ttys002", 4058)})])
    pane = _panes(data)[0]
    assert (pane.kind, pane.cwd, pane.argv) == ("shell", "/home/user", [])


def test_own_subtree_is_never_recorded_as_the_pane_program() -> None:
    """The pane running `ccc snapshot` snapshots as a plain shell at its cwd."""
    data = _build([_window({"pane": _pane("P1", "/dev/ttys003", 5101)})])
    pane = _panes(data)[0]
    assert (pane.kind, pane.cwd) == ("shell", "/repo/tools")


def test_own_subtree_covers_pid_children_and_wrapper_ancestors() -> None:
    rows = snapshot.parse_ps(_PS)
    assert snapshot.own_subtree(rows, 5101) == {5100, 5101, 5102}  # not 5058 (the shell)


def test_pane_without_readable_argv_degrades_to_shell_plus_note() -> None:
    """No KERN_PROCARGS2 answer ⇒ a shell + a breadcrumb, never a reconstructed command."""
    data = _build(
        [_window({"pane": _pane("P1", "/dev/ttys000", 2100, command_line="man git")})],
        args_of=lambda _pid: None,
    )
    pane = _panes(data)[0]
    assert (pane.kind, pane.display, pane.cwd) == ("shell", "man git", "/repo/docs")


def test_classifier_uses_the_pane_path_when_lsof_knew_nothing() -> None:
    raw = _pane("P1", "/dev/ttys002", 4058, path="/fallback/cwd")
    data = _build([_window({"pane": raw})], cwds={})
    assert _panes(data)[0].cwd == "/fallback/cwd"


# --------------------------------------------------------------------------- #
# Snapshot document / schema
# --------------------------------------------------------------------------- #
def test_split_tree_order_survives_the_json_round_trip() -> None:
    """Child order comes from ``tab.root``'s splitter tree, never from ``tab.sessions``."""
    tree = {
        "split": "vertical",
        "children": [
            {"pane": _pane("A", "/dev/ttys002", 4058)},
            {
                "split": "horizontal",
                "children": [
                    {"pane": _pane("B", "/dev/ttys000", 2100)},
                    {"pane": _pane("C", "/dev/ttys001", 3100)},
                ],
            },
        ],
    }
    data = _build([_window(tree, frame=[10, 20, 30, 40], selected=0)])
    assert data["schema_version"] == 1
    assert data["created_at"] == "2026-08-30T10:11:12+02:00"
    assert data["windows"][0]["frame"] == [10, 20, 30, 40]
    node = data["windows"][0]["tabs"][0]["tree"]
    assert node["split"] == "vertical"
    assert node["children"][1]["split"] == "horizontal"
    # A round trip through JSON text must preserve the whole tree verbatim.
    again = json.loads(json.dumps(data))
    assert snapshot.snapshot_counts(again) == (1, 1, 3, 0)
    assert [p.kind for p in _panes(again)] == ["shell", "command", "shell"]


def test_pane_json_omits_empty_fields() -> None:
    pane = snapshot.SnapPane(kind="shell", cwd="/repo")
    assert snapshot.pane_to_json(pane) == {"kind": "shell", "cwd": "/repo"}


def test_single_child_splitter_collapses_to_a_bare_pane() -> None:
    """iTerm's root is always a Splitter; a one-pane tab must not nest pointlessly."""
    tree = {"split": "horizontal", "children": [{"pane": _pane("A", "/dev/ttys002", 4058)}]}
    data = _build([_window(tree)])
    assert "pane" in data["windows"][0]["tabs"][0]["tree"]


def test_selected_tab_and_multi_window_counts() -> None:
    data = _build(
        [
            _window({"pane": _pane("A", "/dev/ttys002", 4058)}, selected=0),
            _window(
                {"pane": _pane("B", "/dev/ttys000", 2100)},
                {"pane": _pane("C", "/dev/ttys001", 3100)},
                frame=None,
                selected=1,
            ),
        ]
    )
    assert snapshot.snapshot_counts(data) == (2, 3, 3, 0)
    assert data["windows"][1]["selected_tab"] == 1
    assert data["windows"][1]["frame"] is None


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def _doc() -> dict:
    return _build([_window({"pane": _pane("A", "/dev/ttys002", 4058)})])


def test_write_snapshot_is_atomic_0600_in_a_0700_dir() -> None:
    folder = snapshot.snapshots_dir()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    path = snapshot.write_snapshot(_doc())
    assert path.parent == folder
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(folder.glob("*.tmp"))  # the mkstemp temp was replaced, not left behind
    assert snapshot.load_snapshot(path)["schema_version"] == 1


def test_write_snapshot_suffixes_same_second_collisions(tmp_path: Path) -> None:
    from datetime import datetime

    when = datetime(2026, 8, 30, 10, 11, 12)
    names = [snapshot.write_snapshot(_doc(), tmp_path, when).name for _ in range(3)]
    assert names == ["20260830-101112.json", "20260830-101112-2.json", "20260830-101112-3.json"]


def test_latest_snapshot_orders_collision_suffixes_after_their_base(tmp_path: Path) -> None:
    """Plain name sort gets this backwards ('-' < '.'), so the key parses the sequence."""
    from datetime import datetime

    snapshot.write_snapshot(_doc(), tmp_path, datetime(2026, 8, 30, 10, 11, 12))
    second = snapshot.write_snapshot(_doc(), tmp_path, datetime(2026, 8, 30, 10, 11, 12))
    assert snapshot.latest_snapshot(tmp_path) == second
    later = snapshot.write_snapshot(_doc(), tmp_path, datetime(2026, 8, 30, 11, 0, 0))
    assert snapshot.latest_snapshot(tmp_path) == later
    assert len(snapshot.list_snapshots(tmp_path)) == 3


def test_resolve_snapshot_by_name_with_and_without_json(tmp_path: Path) -> None:
    from datetime import datetime

    path = snapshot.write_snapshot(_doc(), tmp_path, datetime(2026, 8, 30, 10, 11, 12))
    assert snapshot.resolve_snapshot("20260830-101112", tmp_path) == path
    assert snapshot.resolve_snapshot("20260830-101112.json", tmp_path) == path
    assert snapshot.resolve_snapshot("", tmp_path) == path  # empty => latest
    assert snapshot.resolve_snapshot(str(path), tmp_path) == path  # a path is used as-is
    assert snapshot.resolve_snapshot("nope", tmp_path) is None
    assert snapshot.resolve_snapshot("", tmp_path / "empty") is None


def test_load_snapshot_rejects_junk(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable snapshot"):
        snapshot.load_snapshot(bad)
    bad.write_text('{"schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a ccc snapshot"):
        snapshot.load_snapshot(bad)
    bad.write_text('{"schema_version": 99, "windows": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported schema_version"):
        snapshot.load_snapshot(bad)


# --------------------------------------------------------------------------- #
# Restore planning
# --------------------------------------------------------------------------- #
def _NO_BLOCKERS(_sid: str, _cwd: str, _cfg: str) -> list[str]:  # noqa: N802
    """A ``core.resume_blockers`` stub that never blocks."""
    return []


def _snapshot_of(*panes: snapshot.SnapPane) -> dict:
    """A one-window, one-tab snapshot document holding *panes* side by side."""
    tree: snapshot.SnapNode | snapshot.SnapPane = (
        panes[0] if len(panes) == 1 else snapshot.SnapNode("vertical", list(panes))
    )
    windows = [snapshot.SnapWindow(frame=(0, 0, 10, 10), tabs=[snapshot.SnapTab(tree)])]
    return snapshot.windows_to_json(windows, "2026-08-30T10:11:12+02:00")


def test_plan_skips_a_claude_session_that_is_already_live(tmp_path: Path) -> None:
    pane = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="live-1")
    plan = snapshot.build_plan(_snapshot_of(pane), {"live-1"}, _NO_BLOCKERS, [])
    action = snapshot.plan_actions(plan)[0]
    assert (action.command, action.skipped, action.error) == (None, True, False)
    assert "already running" in action.note


def test_plan_marks_a_blocked_resume_as_an_error_pane(tmp_path: Path) -> None:
    pane = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="blocked")
    plan = snapshot.build_plan(
        _snapshot_of(pane), set(), lambda _s, _c, _d: ["no recorded conversation"], []
    )
    action = snapshot.plan_actions(plan)[0]
    assert action.error is True
    assert action.command == f"cd {tmp_path}"  # only a cd — never `claude --resume`
    assert "no recorded conversation" in action.note


def test_plan_resumes_a_clean_claude_session_on_its_own_account(tmp_path: Path) -> None:
    pane = snapshot.SnapPane(
        kind="claude", cwd=str(tmp_path), session_id="sess-9", config_dir=str(tmp_path / "work")
    )
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, [])
    command = snapshot.plan_actions(plan)[0].command or ""
    # accounts.launch_env_prefix pins the seat INTO the command string (see accounts.py).
    assert command.startswith("unset CLAUDE_SECURESTORAGE_CONFIG_DIR; export CLAUDE_CONFIG_DIR=")
    assert command.endswith(f"cd {tmp_path} && claude --resume sess-9")


def test_plan_default_account_unsets_the_config_var(tmp_path: Path) -> None:
    pane = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="sess-9")
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, [])
    command = snapshot.plan_actions(plan)[0].command or ""
    assert command.startswith("unset CLAUDE_SECURESTORAGE_CONFIG_DIR CLAUDE_CONFIG_DIR; ")


def test_plan_reruns_an_allowlisted_command_with_per_element_quoting(tmp_path: Path) -> None:
    cwd = tmp_path / "my notes"
    cwd.mkdir()
    pane = snapshot.SnapPane(
        kind="command", cwd=str(cwd), argv=["/usr/bin/vim", 'a "quoted" file.md']
    )
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, ["vim"])
    command = snapshot.plan_actions(plan)[0].command or ""
    assert command == f"cd '{cwd}' && /usr/bin/vim 'a \"quoted\" file.md'"


def test_plan_reruns_an_interpreted_script_by_its_own_name(tmp_path: Path) -> None:
    """`ccc` on the allowlist relaunches the TUI, and the FULL argv is what gets run."""
    argv = ["/usr/bin/python3", "/Users/u/.local/bin/ccc"]
    pane = snapshot.SnapPane(kind="command", cwd=str(tmp_path), argv=argv)
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, ["ccc"])
    action = snapshot.plan_actions(plan)[0]
    assert action.command == f"cd {tmp_path} && /usr/bin/python3 /Users/u/.local/bin/ccc"
    assert action.note == ""


def test_plan_does_not_unwrap_an_interpreter_with_no_script(tmp_path: Path) -> None:
    """`python3 -m http.server` is python3, not an allowlisted `http.server`."""
    pane = snapshot.SnapPane(kind="command", cwd=str(tmp_path), argv=["python3", "-m", "vim"])
    action = snapshot.plan_actions(
        snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, ["vim"])
    )[0]
    assert "printf" in (action.command or "")


def test_plan_prints_a_note_for_a_non_allowlisted_command(tmp_path: Path) -> None:
    pane = snapshot.SnapPane(kind="command", cwd=str(tmp_path), argv=["rm", "-rf", "/"])
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, ["vim"])
    action = snapshot.plan_actions(plan)[0]
    command = action.command or ""
    assert command.startswith(f"cd {tmp_path} && printf '%s\\n' ")
    assert "rm -rf /" not in command.split("printf", 1)[0]  # never on the executed side
    assert command.endswith("'[ccc-restore] was running: rm -rf /'")  # inside ONE quoted arg
    assert "not on snapshot_restore_commands" in action.note


def test_plan_shell_pane_just_cds(tmp_path: Path) -> None:
    pane = snapshot.SnapPane(kind="shell", cwd=str(tmp_path))
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, [])
    assert snapshot.plan_actions(plan)[0].command == f"cd {tmp_path}"


def test_plan_shell_pane_keeps_the_display_breadcrumb(tmp_path: Path) -> None:
    pane = snapshot.SnapPane(kind="shell", cwd=str(tmp_path), display="some-daemon --serve")
    action = snapshot.plan_actions(
        snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, [])
    )[0]
    assert (action.command or "").endswith("'[ccc-restore] was running: some-daemon --serve'")


def test_plan_degrades_when_the_cwd_is_gone(tmp_path: Path) -> None:
    """A vanished directory is a degrade (note), not an error (exit code stays 0)."""
    pane = snapshot.SnapPane(kind="command", cwd=str(tmp_path / "gone"), argv=["vim", "x"])
    action = snapshot.plan_actions(
        snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, ["vim"])
    )[0]
    assert (action.command, action.error) == (None, False)
    assert "no longer exists" in action.note


def test_plan_degrades_when_no_cwd_was_recorded() -> None:
    pane = snapshot.SnapPane(kind="shell", cwd="")
    action = snapshot.plan_actions(
        snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, [])
    )[0]
    assert (action.command, action.note, action.error) == (None, "no cwd recorded", False)


def test_plan_preserves_the_split_topology(tmp_path: Path) -> None:
    left = snapshot.SnapPane(kind="shell", cwd=str(tmp_path))
    right = snapshot.SnapPane(kind="shell", cwd=str(tmp_path), title="right")
    plan = snapshot.build_plan(_snapshot_of(left, right), set(), _NO_BLOCKERS, [])
    tree = plan[0].tabs[0].tree
    assert isinstance(tree, snapshot.PlanNode)
    assert tree.split == "vertical" and len(tree.children) == 2
    assert [a.title for a in snapshot.plan_actions(plan)] == ["", "right"]


# --------------------------------------------------------------------------- #
# The split executor (fake iTerm sessions)
# --------------------------------------------------------------------------- #
class FakeSession:
    """A stand-in iTerm session that records its splits, sent text and renames."""

    def __init__(self, name: str, log: list[str], fail_send: bool = False) -> None:
        self.name = name
        self.log = log
        self.sent: list[str] = []
        self.renamed: str | None = None
        self.fail_send = fail_send
        self._splits = 0

    async def async_split_pane(self, vertical: bool = False) -> FakeSession:
        self._splits += 1
        child = FakeSession(f"{self.name}.{'v' if vertical else 'h'}{self._splits}", self.log)
        self.log.append(f"{self.name} -{'V' if vertical else 'H'}-> {child.name}")
        return child

    async def async_send_text(self, text: str) -> None:
        if self.fail_send:
            raise RuntimeError("pane died")
        self.sent.append(text)

    async def async_get_variable(self, name: str) -> str:
        return "zsh"  # an idle shell: _await_shell settles at once

    async def async_set_name(self, name: str) -> None:
        self.renamed = name


async def _split(session: object, vertical: bool) -> object:
    return await session.async_split_pane(vertical=vertical)  # type: ignore[attr-defined]


def _leaf(tag: str) -> snapshot.PaneAction:
    return snapshot.PaneAction(command=f"echo {tag}", kind="shell", title=tag, cwd="/x")


def test_layout_tree_chains_splits_so_children_keep_their_order() -> None:
    """Each new pane is split off the PREVIOUS one — that is what preserves order."""
    log: list[str] = []
    root = FakeSession("root", log)
    node = snapshot.PlanNode("vertical", [_leaf("a"), _leaf("b"), _leaf("c")])
    leaves = asyncio.run(snapshot.layout_tree(node, root, _split))
    assert [a.title for a, _ in leaves] == ["a", "b", "c"]
    assert [s.name for _, s in leaves] == ["root", "root.v1", "root.v1.v1"]
    assert log == ["root -V-> root.v1", "root.v1 -V-> root.v1.v1"]


def test_layout_tree_recurses_into_nested_splitters() -> None:
    log: list[str] = []
    root = FakeSession("root", log)
    node = snapshot.PlanNode(
        "vertical",
        [_leaf("a"), snapshot.PlanNode("horizontal", [_leaf("b"), _leaf("c")])],
    )
    leaves = asyncio.run(snapshot.layout_tree(node, root, _split))
    assert [a.title for a, _ in leaves] == ["a", "b", "c"]
    assert log == ["root -V-> root.v1", "root.v1 -H-> root.v1.h1"]


def test_layout_tree_of_a_single_pane_never_splits() -> None:
    log: list[str] = []
    root = FakeSession("root", log)
    leaves = asyncio.run(snapshot.layout_tree(_leaf("solo"), root, _split))
    assert log == [] and [s.name for _, s in leaves] == ["root"]


def test_deliver_types_commands_and_renames_only_non_claude_panes() -> None:
    log: list[str] = []
    shell, claude = FakeSession("s", log), FakeSession("c", log)
    actions = [
        snapshot.PaneAction(command="cd /x", kind="shell", title="notes"),
        snapshot.PaneAction(command="claude --resume z", kind="claude", title="app"),
    ]
    failures = asyncio.run(snapshot._deliver([(actions[0], shell), (actions[1], claude)]))
    assert failures == []
    assert shell.sent == ["cd /x\n"] and shell.renamed == "notes"
    assert claude.sent == ["claude --resume z\n"] and claude.renamed is None


def test_deliver_collects_per_pane_failures_without_aborting() -> None:
    log: list[str] = []
    broken = FakeSession("broken", log, fail_send=True)
    good = FakeSession("good", log)
    actions = [
        snapshot.PaneAction(command="cd /a", kind="shell", cwd="/a"),
        snapshot.PaneAction(command="cd /b", kind="shell", cwd="/b"),
    ]
    failures = asyncio.run(snapshot._deliver([(actions[0], broken), (actions[1], good)]))
    assert len(failures) == 1 and "/a" in failures[0]
    assert good.sent == ["cd /b\n"]  # the later pane still got its command


class SettlingSession(FakeSession):
    """A pane whose ``jobName`` walks a scripted startup: login → rc children → shell."""

    def __init__(self, name: str, log: list[str], jobs: list[str]) -> None:
        super().__init__(name, log)
        self.jobs = list(jobs)
        self.polls: list[str] = []

    async def async_get_variable(self, name: str) -> str:
        job = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
        self.polls.append(job)
        return job


@pytest.fixture(autouse=True)
def _fast_shell_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "_SHELL_POLL_SECONDS", 0.0)


def test_is_shell_job_recognises_login_shells_and_paths() -> None:
    assert snapshot.is_shell_job("zsh") and snapshot.is_shell_job("-zsh")
    assert snapshot.is_shell_job("/bin/bash") and snapshot.is_shell_job("fish")
    assert not snapshot.is_shell_job("login") and not snapshot.is_shell_job("python3.14")
    assert not snapshot.is_shell_job("") and not snapshot.is_shell_job(None)


def test_await_shell_waits_for_the_shell_to_stay_idle_across_polls() -> None:
    log: list[str] = []
    pane = SettlingSession("p", log, ["login", "zsh", "starship", "zsh", "lsd", "zsh"])
    assert asyncio.run(snapshot._await_shell(pane)) is True
    # a lone "zsh" between two startup children never counts: 3 consecutive polls needed
    assert pane.polls == ["login", "zsh", "starship", "zsh", "lsd", "zsh", "zsh", "zsh"]


def test_await_shell_gives_up_after_the_timeout_and_reports_it() -> None:
    log: list[str] = []
    stuck = SettlingSession("p", log, ["python3"])
    assert asyncio.run(snapshot._await_shell(stuck, timeout=0.0)) is False


def test_await_shell_types_anyway_when_the_pane_has_no_variables() -> None:
    class Mute:
        async def async_get_variable(self, name: str) -> str:
            raise RuntimeError("no such variable")

    assert asyncio.run(snapshot._await_shell(Mute())) is False


def test_deliver_waits_for_every_pane_before_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[str] = []
    slow = SettlingSession("slow", log, ["login", "obsidian_settings.sh", "zsh"])
    fast = SettlingSession("fast", log, ["zsh"])
    actions = [
        snapshot.PaneAction(command="cd /slow", kind="shell", cwd="/slow"),
        snapshot.PaneAction(command="cd /fast", kind="shell", cwd="/fast"),
    ]
    failures = asyncio.run(snapshot._deliver([(actions[0], slow), (actions[1], fast)]))
    assert failures == []
    assert slow.polls[:3] == ["login", "obsidian_settings.sh", "zsh"]  # waited through startup
    assert slow.sent == ["cd /slow\n"] and fast.sent == ["cd /fast\n"]


# --------------------------------------------------------------------------- #
# Phase 1 against fake iTerm windows/tabs (the just-created-object gotcha)
# --------------------------------------------------------------------------- #
class FakeTab:
    """A tab exactly as the Python API hands it back right after creation.

    ``current_session`` is ``None`` — ``active_session_id`` only arrives with a later
    layout notification — while ``sessions`` already lists the one real session.
    """

    def __init__(self, session: FakeSession | None) -> None:
        self.sessions = [session] if session else []
        self.current_session = None
        self.selected = False

    async def async_select(self) -> None:
        self.selected = True


class FakeWindow:
    """A fresh window: ``current_tab`` is ``None`` too (``selected_tab_id`` unset)."""

    def __init__(self, log: list[str], sessions: list[FakeSession | None]) -> None:
        self.log = log
        self.tabs = [FakeTab(sessions[0])]
        self.current_tab = None
        self._pending = list(sessions[1:])
        self.frame: object = None

    async def async_create_tab(self) -> FakeTab:
        self.log.append("create_tab")
        tab = FakeTab(self._pending.pop(0))
        self.tabs.append(tab)
        return tab

    async def async_set_frame(self, frame: object) -> None:
        self.frame = frame


def _fake_iterm(monkeypatch: pytest.MonkeyPatch, log: list[str], *windows: FakeWindow) -> None:
    """Route ``iterm2.Window.async_create`` / ``async_get_app`` to the fakes, logging both."""
    import iterm2  # pylint: disable=import-outside-toplevel

    queue = list(windows)

    async def create(connection: object) -> FakeWindow:
        log.append("create_window")
        return queue.pop(0)

    async def get_app(connection: object) -> object:
        log.append("get_app")
        return object()

    monkeypatch.setattr(iterm2.Window, "async_create", staticmethod(create))
    monkeypatch.setattr(iterm2, "async_get_app", get_app)


def test_build_window_addresses_fresh_tabs_through_sessions_not_current_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: every pane of a restore was skipped because ``current_tab`` and
    ``current_session`` are ``None`` on objects the API has just created."""
    log: list[str] = []
    first, second = FakeSession("t0", log), FakeSession("t1", log)
    window = FakeWindow(log, [first, second])
    _fake_iterm(monkeypatch, log, window)
    plan = snapshot.WindowPlan(
        tabs=[
            snapshot.TabPlan(_leaf("a")),
            snapshot.TabPlan(snapshot.PlanNode("horizontal", [_leaf("b"), _leaf("c")])),
        ]
    )
    built, leaves, lost = asyncio.run(snapshot._build_window(object(), plan))
    assert built is window and lost == []
    assert [a.title for a, _ in leaves] == ["a", "b", "c"]
    assert [s.name for _, s in leaves] == ["t0", "t1", "t1.h1"]
    assert log == ["create_window", "create_tab", "t1 -H-> t1.h1"]


def test_build_window_reports_a_sessionless_tab_per_pane_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    window = FakeWindow(log, [FakeSession("t0", log), None, FakeSession("t2", log)])
    _fake_iterm(monkeypatch, log, window)
    live = snapshot.PaneAction(command=None, kind="claude", cwd="/live", skipped=True)
    plan = snapshot.WindowPlan(
        tabs=[
            snapshot.TabPlan(_leaf("a")),
            snapshot.TabPlan(snapshot.PlanNode("vertical", [_leaf("b"), live, _leaf("c")])),
            snapshot.TabPlan(_leaf("d")),
        ]
    )
    _, leaves, lost = asyncio.run(snapshot._build_window(object(), plan))
    assert [a.title for a, _ in leaves] == ["a", "d"]  # the later tab is still built
    assert len(lost) == 2 and all("no session for tab 2" in text for text in lost)
    assert all(text.startswith("shell pane in /x") for text in lost)  # skipped pane not counted


def test_sole_session_falls_back_to_current_session_only_for_odd_tabs() -> None:
    class Odd:
        sessions = ["s1", "s2"]
        current_session = "s2"

    class Fresh:
        sessions = ["only"]
        current_session = None

    class Empty:
        sessions: list[object] = []
        current_session = None

    assert snapshot._sole_session(Fresh()) == "only"
    assert snapshot._sole_session(Odd()) == "s2"
    assert snapshot._sole_session(Empty()) is None
    assert snapshot._sole_session(None) is None


def test_execute_plan_installs_the_app_delegate_then_delivers_every_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-y`` skips the guard (the only other ``async_get_app`` caller) — the executor must
    install the delegate itself or ``async_create_tab`` asserts; and every planned pane
    of every window gets its command."""
    log: list[str] = []
    w1 = FakeWindow(log, [FakeSession("w1t0", log), FakeSession("w1t1", log)])
    w2 = FakeWindow(log, [FakeSession("w2t0", log)])
    _fake_iterm(monkeypatch, log, w1, w2)
    plan = [
        snapshot.WindowPlan(
            selected_tab=1, tabs=[snapshot.TabPlan(_leaf("a")), snapshot.TabPlan(_leaf("b"))]
        ),
        snapshot.WindowPlan(tabs=[snapshot.TabPlan(_leaf("c"))]),
    ]
    failures = asyncio.run(snapshot.execute_plan(object(), plan))
    assert failures == []
    assert log[:2] == ["get_app", "create_window"]  # delegate first, tabs after
    typed = [t.sessions[0].sent for w in (w1, w2) for t in w.tabs]
    assert typed == [["echo a\n"], ["echo b\n"], ["echo c\n"]]
    assert w1.tabs[1].selected and not w1.tabs[0].selected
    assert snapshot.report_restore(snapshot.plan_actions(plan), failures) == 0


def test_execute_plan_counts_lost_panes_as_failures_in_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log: list[str] = []
    window = FakeWindow(log, [FakeSession("t0", log), None])
    _fake_iterm(monkeypatch, log, window)
    plan = [snapshot.WindowPlan(tabs=[snapshot.TabPlan(_leaf("a")), snapshot.TabPlan(_leaf("b"))])]
    failures = asyncio.run(snapshot.execute_plan(object(), plan))
    assert len(failures) == 1 and failures[0].startswith("window 1: shell pane in /x")
    assert snapshot.report_restore(snapshot.plan_actions(plan), failures) == 1
    assert "restored 1 pane(s), skipped 0, failed 1" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Guards + exit-code aggregation
# --------------------------------------------------------------------------- #
def test_other_pane_count_excludes_the_pane_running_the_command() -> None:
    raw = [
        _window({"pane": _pane("AAAA", "/dev/ttys000", None)}),
        _window(
            {
                "split": "vertical",
                "children": [
                    {"pane": _pane("BBBB", "/dev/ttys001", None)},
                    {"pane": _pane("CCCC", "/dev/ttys002", None)},
                ],
            }
        ),
    ]
    assert snapshot.other_pane_count(raw, "w0t0p0:aaaa") == 2  # case-insensitive uuid tail
    assert snapshot.other_pane_count(raw, "") == 3  # unknown own pane -> count them all
    assert snapshot.other_pane_count([], "w0t0p0:aaaa") == 0


def test_report_restore_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    clean = [
        snapshot.PaneAction(command="cd /a", kind="shell"),
        snapshot.PaneAction(command=None, skipped=True, kind="claude"),
    ]
    assert snapshot.report_restore(clean, []) == 0
    assert "restored 1 pane(s), skipped 1, failed 0" in capsys.readouterr().out

    errored = [*clean, snapshot.PaneAction(command="cd /b", error=True, note="boom", cwd="/b")]
    assert snapshot.report_restore(errored, []) == 1
    assert "boom" in capsys.readouterr().err

    assert snapshot.report_restore(clean, ["window 1: nope"]) == 1
    assert "window 1: nope" in capsys.readouterr().err


def test_restore_commands_falls_back_to_the_shipped_default() -> None:
    from command_center.config import Config

    assert "vim" in snapshot.restore_commands(Config())
    assert snapshot.restore_commands(Config(snapshot_restore_commands=[])) == list(
        snapshot.DEFAULT_RESTORE_COMMANDS
    )
    assert snapshot.restore_commands(Config(snapshot_restore_commands=["htop", " "])) == ["htop"]


def test_snapshot_restore_commands_is_a_real_config_key() -> None:
    from command_center.config import DEFAULTS, Config

    assert DEFAULTS["snapshot_restore_commands"] == Config().snapshot_restore_commands


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def test_both_commands_refuse_under_the_tmux_launcher(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from command_center import config as config_module

    real = config_module.load_config

    def _tmux() -> object:
        cfg = real()
        cfg.launcher = "tmux"
        return cfg

    monkeypatch.setattr(config_module, "load_config", _tmux)
    assert snapshot.run_snapshot(argparse.Namespace(list=False, dry_run=False)) == 1
    assert "iTerm2-only" in capsys.readouterr().err
    args = argparse.Namespace(name=None, dry_run=True, yes=False)
    assert snapshot.run_restore(args) == 1
    assert "iTerm2-only" in capsys.readouterr().err


def test_snapshot_list_reports_counts_and_age(capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    path = snapshot.write_snapshot(_doc())
    assert snapshot.run_snapshot(argparse.Namespace(list=True, dry_run=False)) == 0
    out = capsys.readouterr().out
    assert path.name in out and "1 window(s), 1 tab(s), 1 pane(s), 0 claude" in out


def test_snapshot_list_is_empty_before_the_first_capture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    assert snapshot.run_snapshot(argparse.Namespace(list=True, dry_run=False)) == 0
    assert "no snapshots yet" in capsys.readouterr().out


def test_restore_reports_a_missing_snapshot(capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    args = argparse.Namespace(name="does-not-exist", dry_run=True, yes=False)
    assert snapshot.run_restore(args) == 1
    assert "no snapshot does-not-exist" in capsys.readouterr().err


def test_restore_dry_run_prints_the_plan_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("--dry-run must never open an iTerm2 connection")

    monkeypatch.setattr(snapshot, "_connect", _boom)
    pane = snapshot.SnapPane(kind="shell", cwd=str(tmp_path), title="notes")
    path = snapshot.write_snapshot(_snapshot_of(pane))
    args = argparse.Namespace(name=path.name, dry_run=True, yes=False)
    assert snapshot.run_restore(args) == 0
    out = capsys.readouterr().out
    assert f"restoring {path.name}" in out and "ago" in out
    assert f"+ cd {tmp_path}" in out


def test_restore_dry_run_exits_1_when_a_pane_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from command_center import core

    monkeypatch.setattr(core, "resume_blockers", lambda _s, _c, _d: ["nothing to resume"])
    pane = snapshot.SnapPane(kind="claude", cwd=str(tmp_path), session_id="dead")
    path = snapshot.write_snapshot(_snapshot_of(pane))
    args = argparse.Namespace(name=path.name, dry_run=True, yes=False)
    assert snapshot.run_restore(args) == 1
    assert "nothing to resume" in capsys.readouterr().out


def test_cli_registers_both_subcommands() -> None:
    from command_center import cli

    parser = cli.build_parser()
    args = parser.parse_args(["snapshot", "-n"])
    assert args.func is cli.cmd_snapshot and args.dry_run is True
    args = parser.parse_args(["restore-snapshot", "20260830-101112", "-y"])
    assert args.func is cli.cmd_restore_snapshot
    assert (args.name, args.yes, args.dry_run) == ("20260830-101112", True, False)


# --------------------------------------------------------------------------- #
# core.resume_blockers — the shared resume pre-flight
# --------------------------------------------------------------------------- #
def test_resume_blockers_is_clean_for_a_session_with_a_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from command_center import core

    home = tmp_path / "claude-home"
    project = home / "projects" / "-repo-app"
    project.mkdir(parents=True)
    (project / "sess-x.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    assert core.resume_blockers("sess-x", "/repo/app", str(home)) == []


def test_resume_blockers_flags_a_missing_transcript() -> None:
    from command_center import core

    blockers = core.resume_blockers("ghost", "/repo/app", "")
    assert len(blockers) == 1 and "no recorded conversation" in blockers[0]
    assert not blockers[0].startswith("error:")  # the caller adds the prefix


def test_cmd_resume_still_reports_the_blocker_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refactor onto core.resume_blockers must not change `ccc resume`'s stderr."""
    import argparse

    from command_center.cli import cmd_resume
    from command_center.store import Store

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    with Store(tmp_path / "command-center" / "state.db") as store:
        store.ensure("dead-sess", cwd="/repo")
    monkeypatch.setattr(os, "execvp", lambda *_a: pytest.fail("must not exec"))
    assert cmd_resume(argparse.Namespace(session_id="dead-sess")) == 1
    assert "error: no recorded conversation for dead-sess" in capsys.readouterr().err
