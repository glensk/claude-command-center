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


# The 2026-10-27 shapes (tp#1317): every Claude pane below is LIVE, but Claude Code's
# registry yields nothing for it — the capture must still record it as a claude pane.
_SID_NEW = "3249fd97-06b2-4c9c-a47a-5934c4afad8d"
_SID_RES = "e3b6fd23-1a46-45f2-9cdc-d4048e926d71"
_SID_BARE = "7b659c27-d812-4e21-93b0-fe78346f9f0d"
_PS_1317 = """\
    1     0 ??       Ss   /sbin/launchd
 7057     1 ttys007  Ss   /usr/bin/login -fqpl user
 7058  7057 ttys007  S    -zsh
 7100  7058 ttys007  S+   claude --model claude-opus-5-5 --session-id 3249fd97 -- go
 8057     1 ttys008  Ss   /usr/bin/login -fqpl user
 8058  8057 ttys008  S    -zsh
 8100  8058 ttys008  S+   claude --dangerously-skip-permissions --resume e3b6fd23
 9057     1 ttys009  Ss   /usr/bin/login -fqpl user
 9058  9057 ttys009  S    -zsh
 9100  9058 ttys009  S+   claude --dangerously-skip-permissions
"""
_ARGV_1317 = {
    7100: ["claude", "--model", "claude-opus-5-5", "--session-id", _SID_NEW, "--", "go"],
    8100: ["claude", "--dangerously-skip-permissions", "--resume", _SID_RES],
    9100: ["claude", "--dangerously-skip-permissions"],
}
_CWDS_1317 = {7100: "/repo/dot", 8100: "/repo/tp", 9100: "/repo/tp", 9058: "/home/user"}


def _build_1317(
    raw_panes: list[dict],
    live: list[LiveSession] | None = None,
    store_info: list[snapshot.StoreRow] | None = None,
    argv: dict[int, list[str]] | None = None,
) -> list[snapshot.SnapPane]:
    """Capture *raw_panes* (one tab each) over the tp#1317 process fixture."""
    data = snapshot.build_snapshot(
        [_window(*({"pane": p} for p in raw_panes))],
        live or [],
        {},
        snapshot.parse_ps(_PS_1317),
        (argv or _ARGV_1317).get,
        dict(_CWDS_1317),
        own_pid=0,  # no own subtree (pid 1's would be every process)
        created_at="2026-10-27T10:10:35+01:00",
        store_info=store_info,
    )
    return _panes(data)


def test_claude_argv_session_id_parses_every_launch_shape() -> None:
    sid = _SID_NEW
    assert snapshot.claude_argv_session_id(["claude", "--session-id", sid, "--", "x"]) == sid
    assert snapshot.claude_argv_session_id(["claude", f"--session-id={sid}"]) == sid
    assert snapshot.claude_argv_session_id(["claude", "--resume", sid]) == sid
    assert snapshot.claude_argv_session_id(["claude", "-r", sid]) == sid
    assert snapshot.claude_argv_session_id(["/x/claude/versions/2.1.311", "-r", sid]) == sid
    # no id / the prompt after `--` / the picker / a fork / not claude at all
    assert snapshot.claude_argv_session_id(["claude", "--dangerously-skip-permissions"]) == ""
    assert snapshot.claude_argv_session_id(["claude", "--", "--resume", sid]) == ""
    assert snapshot.claude_argv_session_id(["claude", "--resume", "--model", "x"]) == ""
    assert snapshot.claude_argv_session_id(["claude", "--resume", sid, "--fork-session"]) == ""
    assert snapshot.claude_argv_session_id(["vim", "--resume", sid]) == ""


def test_registry_miss_still_records_session_id_argv_as_claude() -> None:
    """A `ccc new-job` launch (`--session-id`) with NO registry entry is a claude pane,
    on the account its store row names (the 10:10 capture recorded it as a command)."""
    row = snapshot.StoreRow(_SID_NEW, config_dir="/acct/private", no_codex=True)
    (pane,) = _build_1317([_pane("U7", "/dev/ttys007", 7100)], store_info=[row])
    assert (pane.kind, pane.session_id, pane.cwd) == ("claude", _SID_NEW, "/repo/dot")
    assert (pane.config_dir, pane.no_codex, pane.matched) == ("/acct/private", True, "argv")


def test_registry_miss_still_records_resume_argv_as_claude() -> None:
    (pane,) = _build_1317([_pane("U8", "/dev/ttys008", 8100)])
    assert (pane.kind, pane.session_id, pane.config_dir) == ("claude", _SID_RES, "")


def test_argv_id_that_is_live_takes_the_registry_account() -> None:
    live = [_live(_SID_RES, pid=4242, cwd="/repo/tp", config_dir="/acct/work")]
    (pane,) = _build_1317([_pane("U8", "/dev/ttys008", 8100)], live=live)
    assert (pane.kind, pane.config_dir, pane.matched) == ("claude", "/acct/work", "argv")


def test_bare_claude_takes_its_id_from_the_open_row_with_its_pid_and_pane() -> None:
    rows = [
        snapshot.StoreRow(
            _SID_BARE, config_dir="/acct/work", iterm_uuid="U9", last_seen_pid=9100, updated_at=5
        ),
        # a finished session that once had the same pid in the same tab is never chosen
        snapshot.StoreRow("old", iterm_uuid="U9", last_seen_pid=9100, updated_at=9, closed=True),
    ]
    (pane,) = _build_1317([_pane("u9", "/dev/ttys009", 9100)], store_info=rows)
    assert (pane.kind, pane.session_id, pane.config_dir) == ("claude", _SID_BARE, "/acct/work")
    assert pane.matched == "store"


def test_bare_claude_needs_both_the_pid_and_the_pane_to_agree() -> None:
    """A reused pid in ANOTHER tab, or this tab's row of another pid, names nothing."""
    rows = [
        snapshot.StoreRow("other-tab", iterm_uuid="UX", last_seen_pid=9100),
        snapshot.StoreRow("other-pid", iterm_uuid="U9", last_seen_pid=4242),
    ]
    (pane,) = _build_1317([_pane("U9", "/dev/ttys009", 9100)], store_info=rows)
    assert pane.kind == "command"


def test_argv_id_never_borrows_another_rows_account() -> None:
    rows = [
        snapshot.StoreRow("stale", config_dir="/acct/other", iterm_uuid="U8", last_seen_pid=8100)
    ]
    (pane,) = _build_1317([_pane("U8", "/dev/ttys008", 8100)], store_info=rows)
    assert (pane.session_id, pane.config_dir, pane.matched) == (_SID_RES, "", "argv")


def test_a_stale_registry_pid_reused_by_another_program_is_not_claude() -> None:
    """The registry file outlived its process and the pid now runs vim."""
    live = [_live("stale", pid=9100, cwd="/repo/tp")]
    (pane,) = _build_1317(
        [_pane("U9", "/dev/ttys099", 9100)], live=live, argv={9100: ["vim", "notes.md"]}
    )
    assert pane.kind == "command"


def test_registry_pid_join_refuses_a_claude_whose_argv_names_another_session() -> None:
    live = [_live("someone-else", pid=8100, cwd="/repo/tp")]
    (pane,) = _build_1317([_pane("U8", "/dev/ttys099", 8100)], live=live)
    assert (pane.session_id, pane.matched) == (_SID_RES, "argv")


def test_live_match_takes_no_codex_from_the_store_row() -> None:
    """The registry has no Codex opt-out; the restored pane must keep its pin."""
    live = [_live(_SID_RES, pid=8100, cwd="/repo/tp")]
    rows = [snapshot.StoreRow(_SID_RES, no_codex=True)]
    (pane,) = _build_1317([_pane("U8", "/dev/ttys008", 8100)], live=live, store_info=rows)
    assert (pane.kind, pane.matched, pane.no_codex) == ("claude", "tty", True)


def test_bare_claude_with_no_recoverable_id_stays_a_command() -> None:
    (pane,) = _build_1317([_pane("U9", "/dev/ttys009", 9100)])
    assert (pane.kind, pane.argv) == ("command", ["claude", "--dangerously-skip-permissions"])


def test_registry_pid_on_the_foreground_chain_matches_when_the_tty_join_misses() -> None:
    """The registry pid IS the pane's (bare) claude even though its ps tty is not the pane's."""
    live = [_live("sess-pid", pid=9100, cwd="/repo/tp", config_dir="/acct/work")]
    (pane,) = _build_1317([_pane("U9", "/dev/ttys099", 9100)], live=live)
    assert (pane.kind, pane.session_id, pane.matched) == ("claude", "sess-pid", "pid")


def test_matched_is_persisted_for_diagnosis_and_read_back() -> None:
    (pane,) = _build_1317([_pane("U8", "/dev/ttys008", 8100)])
    doc = snapshot.windows_to_json(
        [snapshot.SnapWindow(tabs=[snapshot.SnapTab(pane)])], "2026-10-27T10:10:35+01:00"
    )
    assert snapshot.windows_from_json(doc)[0].tabs[0].tree == pane
    assert "via argv" in snapshot.pane_summary(pane)


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


def _legacy(tmp_path: Path, argv: list[str]) -> snapshot.SnapPane:
    """A pre-tp#1317 snapshot pane: a live Claude session recorded as a plain command."""
    return snapshot.SnapPane(kind="command", cwd=str(tmp_path), argv=argv, display=" ".join(argv))


def test_plan_upgrades_a_legacy_session_id_command_pane_to_a_resume(tmp_path: Path) -> None:
    sid = "3249fd97-06b2-4c9c-a47a-5934c4afad8d"
    pane = _legacy(tmp_path, ["claude", "--model", "m", "--session-id", sid, "--", "go"])
    store = {sid: snapshot.StoreRow(sid, config_dir=str(tmp_path / "work"))}
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, [], store=store)
    action = snapshot.plan_actions(plan)[0]
    assert action.kind == "claude"
    assert (action.command or "").startswith(
        "unset CLAUDE_SECURESTORAGE_CONFIG_DIR; export CLAUDE_CONFIG_DIR="
    )
    assert (action.command or "").endswith(f"cd {tmp_path} && claude --resume {sid}")
    assert "printf" not in (action.command or "")


def test_plan_legacy_upgrade_skips_a_live_session_and_keeps_blockers(tmp_path: Path) -> None:
    sid = "e3b6fd23-1a46-45f2-9cdc-d4048e926d71"
    pane = _legacy(tmp_path, ["claude", "--dangerously-skip-permissions", "--resume", sid])
    live = snapshot.plan_actions(snapshot.build_plan(_snapshot_of(pane), {sid}, _NO_BLOCKERS, []))
    assert live[0].skipped is True
    seen: list[tuple[str, str]] = []

    def blockers(session_id: str, _cwd: str, config_dir: str) -> list[str]:
        seen.append((session_id, config_dir))
        return ["no recorded Claude account"]

    blocked = snapshot.plan_actions(snapshot.build_plan(_snapshot_of(pane), set(), blockers, []))
    assert seen == [(sid, "")]  # no store row: the account stays blank for the pre-flight
    assert (blocked[0].error, blocked[0].command) == (True, f"cd {tmp_path}")


def test_plan_legacy_claude_without_an_id_stays_a_note(tmp_path: Path) -> None:
    pane = _legacy(tmp_path, ["claude", "--dangerously-skip-permissions"])
    action = snapshot.plan_actions(
        snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, [])
    )[0]
    assert action.kind == "command"
    assert snapshot.RESTORE_NOTE_PREFIX in (action.command or "")


def test_plan_never_reruns_a_claude_argv_even_when_allowlisted(tmp_path: Path) -> None:
    pane = _legacy(tmp_path, ["claude", "--dangerously-skip-permissions"])
    plan = snapshot.build_plan(_snapshot_of(pane), set(), _NO_BLOCKERS, ["claude"])
    assert snapshot.RESTORE_NOTE_PREFIX in (snapshot.plan_actions(plan)[0].command or "")


def test_restore_dry_run_upgrades_an_on_disk_legacy_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`ccc restore-snapshot -n` over a pre-tp#1317 file plans a resume on the account
    of the store row, and leaves the file byte-for-byte untouched."""
    import argparse

    from command_center import core, store
    from command_center.adapters import ClaudeAdapter

    sid = "3249fd97-06b2-4c9c-a47a-5934c4afad8d"
    pane = _legacy(tmp_path, ["claude", "--model", "m", "--session-id", sid, "--", "go"])
    path = snapshot.write_snapshot(_snapshot_of(pane))
    before = path.read_bytes()

    class _Row:
        session_id, config_dir, no_codex = sid, str(tmp_path / "work"), True

    class _Store:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def list_sessions(self, _archived: bool = False) -> list[_Row]:
            return [_Row()]

    monkeypatch.setattr(store, "Store", _Store)
    monkeypatch.setattr(ClaudeAdapter, "discover", lambda _self: [])
    monkeypatch.setattr(core, "resume_blockers", lambda *_a: [])
    args = argparse.Namespace(name=path.name, dry_run=True, yes=False)
    assert snapshot.run_restore(args) == 0
    out = capsys.readouterr().out
    assert f"claude --resume {sid}" in out and "CCC_NO_CODEX" in out
    assert snapshot.RESTORE_NOTE_PREFIX not in out
    assert "1 claude session(s)" in out
    assert path.read_bytes() == before


def test_restore_dry_run_takes_a_storeless_account_from_its_own_registry_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import argparse

    from command_center import core, store
    from command_center.adapters import ClaudeAdapter

    sid = "e3b6fd23-1a46-45f2-9cdc-d4048e926d71"
    pane = _legacy(tmp_path, ["claude", "--resume", sid])
    path = snapshot.write_snapshot(_snapshot_of(pane))
    work = str(tmp_path / "work")
    entries = [
        LiveSession(pid=1, session_id=sid, cwd=str(tmp_path), alive=False, config_dir=work),
        # a D9 conflict never lends its (blank) account; another id never lends its own
        LiveSession(pid=2, session_id="other", cwd="/", alive=False, config_dir="/acct/x"),
    ]

    class _Store:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def list_sessions(self, _archived: bool = False) -> list[object]:
            return []

    monkeypatch.setattr(store, "Store", _Store)
    monkeypatch.setattr(ClaudeAdapter, "discover", lambda _self: entries)
    seen: list[str] = []

    def blockers(_sid: str, _cwd: str, config_dir: str) -> list[str]:
        seen.append(config_dir)
        return []

    monkeypatch.setattr(core, "resume_blockers", blockers)
    args = argparse.Namespace(name=path.name, dry_run=True, yes=False)
    assert snapshot.run_restore(args) == 0
    assert seen == [work]
    assert f"claude --resume {sid}" in capsys.readouterr().out


def test_snapshot_counts_include_legacy_claude_panes(tmp_path: Path) -> None:
    sid = "3249fd97-06b2-4c9c-a47a-5934c4afad8d"
    doc = _snapshot_of(
        _legacy(tmp_path, ["claude", "--session-id", sid]),
        _legacy(tmp_path, ["claude"]),
    )
    assert snapshot.snapshot_counts(doc) == (1, 1, 2, 1)


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
        self.injected: list[bytes] = []
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
        # An idle shell until text is typed (_await_shell settles at once); then the
        # typed program owns the foreground (_await_program returns at once).
        return "prog" if self.sent else "zsh"

    async def async_set_name(self, name: str) -> None:
        self.renamed = name

    async def async_inject(self, data: bytes) -> None:
        self.injected.append(data)


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


def test_deliver_types_commands_and_falls_back_to_pane_names_for_every_kind() -> None:
    """A snapshot without tab titles renames each pane — Claude panes too (tp#1317):
    they launch with CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1, so nothing overwrites it."""
    log: list[str] = []
    shell, claude = FakeSession("s", log), FakeSession("c", log)
    actions = [
        snapshot.PaneAction(command="cd /x", kind="shell", title="notes"),
        snapshot.PaneAction(command="claude --resume z", kind="claude", title="app"),
    ]
    failures = asyncio.run(snapshot._deliver([(actions[0], shell), (actions[1], claude)]))
    assert failures == []
    assert shell.sent == ["cd /x\n"] and shell.renamed == "notes"
    assert claude.sent == ["claude --resume z\n"] and claude.renamed == "app"


class TitledTab:
    """A live tab that records ``async_set_title``."""

    def __init__(self) -> None:
        self.title: str | None = None

    async def async_set_title(self, title: str) -> None:
        self.title = title


def test_deliver_restores_a_tab_title_the_way_it_was_set() -> None:
    log: list[str] = []
    auto, override = FakeSession("a", log), FakeSession("o", log)
    tab = TitledTab()
    actions = [
        snapshot.PaneAction(
            command="claude --resume z", kind="claude", title="solarized", tab_title="🟤 dot"
        ),
        snapshot.PaneAction(
            command="cd /x",
            kind="shell",
            title="tp",
            tab_title="💠 a\\b",
            tab_title_override=True,
            tab_title_format="💠 a\\\\b",
            tab=tab,
        ),
    ]
    assert asyncio.run(snapshot._deliver([(actions[0], auto), (actions[1], override)])) == []
    # OSC 1 into the pane (never the profile-derived pane name), like iterm-set-tabtitle.py
    assert auto.injected == ["\033]1;🟤 dot\007".encode()] and auto.renamed is None
    # a tab-level override goes back on the tab, backslash-escaped for interpolation
    assert tab.title == "💠 a\\\\b" and not override.injected and override.renamed is None


def test_deliver_titles_a_claude_pane_only_after_its_program_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected before `cd … && claude` ran, the shell's own title hook would win."""
    monkeypatch.setattr(snapshot, "_TITLE_WAIT_TIMEOUT", 1.0)
    log: list[str] = []
    calls: list[str] = []

    class Pane(FakeSession):
        async def async_get_variable(self, name: str) -> str:
            job = "prog" if self.sent else "zsh"
            calls.append(job)
            return job

        async def async_inject(self, data: bytes) -> None:
            calls.append("inject")
            await super().async_inject(data)

    pane = Pane("p", log)
    action = snapshot.PaneAction(command="claude --resume z", kind="claude", tab_title="T")
    assert asyncio.run(snapshot._deliver([(action, pane)])) == []
    assert calls[-2:] == ["prog", "inject"]


def test_a_failed_title_is_a_warning_not_a_failed_restore(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Broken(FakeSession):
        async def async_inject(self, data: bytes) -> None:
            raise RuntimeError("gone")

    action = snapshot.PaneAction(command="cd /x", kind="shell", cwd="/x", tab_title="T")
    assert asyncio.run(snapshot._deliver([(action, Broken("b", []))])) == []
    assert "warning: title of the shell pane in /x not restored: gone" in capsys.readouterr().err


def test_an_interpolated_tab_override_round_trips_as_its_expression(tmp_path: Path) -> None:
    raw = _window({"pane": _pane("P1", "/dev/ttys002", None)})
    raw["tabs"][0].update(
        title="shown now", title_override="shown now", title_format="\\(session.name)"
    )
    (tab,) = snapshot.windows_from_json(_build([raw]))[0].tabs
    assert (tab.title, tab.title_override, tab.title_format) == (
        "shown now",
        True,
        "\\(session.name)",
    )
    tab.tree = snapshot.SnapPane(kind="shell", cwd=str(tmp_path))
    doc = snapshot.windows_to_json([snapshot.SnapWindow(tabs=[tab])], "2026-10-27T10:10:35+01:00")
    (action,) = snapshot.plan_actions(snapshot.build_plan(doc, set(), _NO_BLOCKERS, []))
    assert action.tab_title_format == "\\(session.name)"  # set back as-is, not escaped


def test_osc_title_strips_control_characters() -> None:
    assert snapshot.osc_title("a\x07b\nc\x1b\x9cd") == b"\x1b]1;abcd\x07"


def test_capture_records_the_tab_title_and_prefers_it_in_dry_run() -> None:
    raw = _window({"pane": _pane("P1", "/dev/ttys002", None, name="solarized")})
    raw["tabs"][0].update(title="🟤 dotfiles", title_override="")
    raw2 = _window({"pane": _pane("P2", "/dev/ttys002", None)})
    raw2["tabs"][0].update(title="💠 mac work", title_override="💠 mac work")
    data = _build([raw, raw2])
    tabs = [t for w in snapshot.windows_from_json(data) for t in w.tabs]
    assert [(t.title, t.title_override) for t in tabs] == [
        ("🟤 dotfiles", False),
        ("💠 mac work", True),
    ]
    lines = "\n".join(snapshot.describe_snapshot(data))
    assert '"🟤 dotfiles"' in lines and '"💠 mac work"  [tab override]' in lines


def test_old_snapshot_tabs_without_titles_still_load_and_plan_pane_names(
    tmp_path: Path,
) -> None:
    pane = snapshot.SnapPane(kind="shell", cwd=str(tmp_path), title="notes")
    doc = _snapshot_of(pane)
    assert "title" not in doc["windows"][0]["tabs"][0]  # serialisation unchanged
    action = snapshot.plan_actions(snapshot.build_plan(doc, set(), _NO_BLOCKERS, []))[0]
    assert (action.tab_title, action.title) == ("", "notes")


def test_plan_puts_the_tab_title_on_the_first_pane_only(tmp_path: Path) -> None:
    left = snapshot.SnapPane(kind="shell", cwd=str(tmp_path), title="l")
    right = snapshot.SnapPane(kind="shell", cwd=str(tmp_path), title="r")
    tab = snapshot.SnapTab(
        snapshot.SnapNode("vertical", [left, right]), title="T", title_override=True
    )
    doc = snapshot.windows_to_json([snapshot.SnapWindow(tabs=[tab])], "2026-10-27T10:10:35+01:00")
    plan = snapshot.build_plan(doc, set(), _NO_BLOCKERS, [])
    first, second = snapshot.plan_actions(plan)
    assert (first.tab_title, first.tab_title_override, second.tab_title) == ("T", True, "")
    assert '"T"  [tab override]' in "\n".join(snapshot.describe_plan(plan))


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
    monkeypatch.setattr(snapshot, "_TITLE_WAIT_TIMEOUT", 0.0)


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
