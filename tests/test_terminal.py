"""Unit tests for the terminal/iTerm helpers (AppleScript stubbed out).

Also home to the Stop-chain DRAIN predicate (``drain_state`` / ``stop_hook_tokens`` /
``own_branch_pids`` and ``cli._wait_for_stop_drain``): no process is ever forked — every
test hands the predicate a hand-built ``ps`` table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import terminal


def test_close_iterm_session_maps_osascript_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_osascript(script: str) -> str:
        captured["script"] = script
        return "tab\n"

    monkeypatch.setattr(terminal, "_osascript", fake_osascript)
    assert terminal.close_iterm_session("w0t1p0:ABC-123") == "tab"
    # The UUID after the colon is what the AppleScript matches on.
    assert "ABC-123" in captured["script"]


def test_close_iterm_session_session_vs_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal, "_osascript", lambda _s: "session")
    assert terminal.close_iterm_session("w0t1p0:UUID") == "session"

    # Not located / unknown output -> "".
    monkeypatch.setattr(terminal, "_osascript", lambda _s: "")
    assert terminal.close_iterm_session("w0t1p0:UUID") == ""
    monkeypatch.setattr(terminal, "_osascript", lambda _s: "weird")
    assert terminal.close_iterm_session("w0t1p0:UUID") == ""


def test_close_iterm_session_no_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    # osascript missing / failed -> None -> "".
    monkeypatch.setattr(terminal, "_osascript", lambda _s: None)
    assert terminal.close_iterm_session("w0t1p0:UUID") == ""
    # No UUID at all -> "" without even invoking AppleScript.
    assert terminal.close_iterm_session(":") == ""
    assert terminal.close_iterm_session("") == ""


def test_set_session_titles_builds_script(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))
    terminal.set_session_titles({"w0t1p0:ABC-123": '🔴 my"repo'})
    assert len(calls) == 1
    script = calls[0][-1]
    assert "ABC-123" in script  # keyed on the UUID after the colon
    assert '🔴 my\\"repo' in script  # title embedded with the quote escaped


def test_set_session_titles_skips_when_empty_or_no_osascript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))

    # Nothing to set -> no subprocess, even if osascript exists.
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    terminal.set_session_titles({})
    # osascript missing -> no subprocess, even with titles to set.
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: None)
    terminal.set_session_titles({"w0t1p0:UUID": "🔴 repo"})
    assert calls == []


def test_set_session_titles_preserving_builds_marker_aware_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))

    terminal.set_session_titles_preserving({"w0t1p0:ABC-123": "🟧 cscs-api"}, marker="🔴 ")
    assert len(calls) == 1
    script = calls[0][-1]
    assert "ABC-123" in script  # keyed on the UUID after the colon
    assert "🟧 cscs-api" in script  # the desired core is embedded
    # Marker preserved: it measures the marker length and slices the title past it,
    # so a "waiting" tab keeps its 🔴 while only the badge+leaf core is rewritten.
    assert 'set mlen to (count of "🔴 ")' in script
    assert "starts with" in script and "text (mlen + 1) thru -1 of n" in script


def test_set_session_titles_preserving_skips_when_empty_or_no_osascript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(terminal.subprocess, "Popen", lambda cmd, **_kw: calls.append(cmd))

    monkeypatch.setattr(terminal.shutil, "which", lambda _name: "/usr/bin/osascript")
    terminal.set_session_titles_preserving({})
    monkeypatch.setattr(terminal.shutil, "which", lambda _name: None)
    terminal.set_session_titles_preserving({"w0t1p0:UUID": "🟧 repo"})
    assert calls == []


# --------------------------------------------------------------------------- #
# The Stop-chain drain predicate (tp#225)
#
# `close-now` / `switch-now` must decide when to KILL a Claude process. The predicate
# is deliberately tri-state: "cannot tell" is UNKNOWN, and the callers read that as
# "do not kill" — the file locks are protected by the store-side lease, never by this
# observation, so a detection weakness here can only cost a refused close.
# --------------------------------------------------------------------------- #
CLAUDE_PID = 4321


def _row(ppid: int, command: str):
    from command_center.snapshot import PsRow

    return PsRow(ppid, "ttys009", "S", command)


def _tree(**children: str) -> dict[int, object]:
    """A claude at ``CLAUDE_PID`` plus one child per ``pid<N>=<command>`` keyword."""
    table: dict[int, object] = {CLAUDE_PID: _row(1, "claude")}
    for name, command in children.items():
        table[int(name.removeprefix("pid"))] = _row(CLAUDE_PID, command)
    return table


def _write_project_settings(cwd: Path, command: str, name: str = "settings.json") -> None:
    import json

    claude = cwd / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / name).write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}),
        encoding="utf-8",
    )


def test_stop_hook_tokens_reads_the_account_and_project_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path-looking words and their basenames, from settings.json + the project's two files."""
    import json

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/opt/ccc hook stop"}]},
                        {"hooks": [{"type": "command", "command": "/etc/hooks/fmt.sh"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cwd = tmp_path / "repo"
    _write_project_settings(cwd, "/bin/zsh /Users/me/scripts/commit-all.sh --push")
    _write_project_settings(cwd, "node /Users/me/x/relay.mjs", name="settings.local.json")
    tokens = terminal.stop_hook_tokens(str(cwd))
    assert "commit-all.sh" in tokens and "/users/me/scripts/commit-all.sh" in tokens
    assert "relay.mjs" in tokens
    assert "fmt.sh" in tokens  # the account scope IS read — just not ccc's own entries
    assert "/opt/ccc" not in tokens  # ccc's own entry contributes nothing (multi-purpose bin)
    assert "--push" not in tokens and "node" not in tokens  # no slash / shell noise
    # An interpreter a hook is merely wrapped in is never a token: it would match every
    # unrelated zsh/python child of the observed claude.
    assert "/bin/zsh" not in tokens and "zsh" not in tokens


def test_stop_hook_tokens_tolerates_missing_and_invalid_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable or malformed scope contributes nothing and never raises."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "nowhere"))
    assert terminal.stop_hook_tokens("") == set()
    cwd = tmp_path / "repo"
    (cwd / ".claude").mkdir(parents=True)
    (cwd / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    assert terminal.stop_hook_tokens(str(cwd)) == set()
    assert terminal.stop_hook_tokens("/no/such/directory") == set()


def test_stop_hook_tokens_never_carry_cccs_own_binary_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ccc's own Stop entries contribute NOTHING: `ccc` is a multi-purpose binary.

    Its path would match every `ccc statusline` / `ccc aim` / `ccc daemon` the status line
    spawns under the observed claude, so the chain would read RUNNING forever and no close
    would ever happen. ccc's own entries say `hook` anyway, which the over-match covers.
    """
    import json

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/opt/ccc hook stop"}]},
                        {"hooks": [{"type": "command", "command": "/opt/ccc hook release-locks"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert terminal.stop_hook_tokens("") == set()
    # A foreign entry in the SAME list is still read.
    cwd = tmp_path / "repo"
    _write_project_settings(cwd, "/bin/zsh /Users/me/scripts/commit-all.sh")
    tokens = terminal.stop_hook_tokens(str(cwd))
    assert "commit-all.sh" in tokens
    assert not [token for token in tokens if token.endswith("/ccc") or token == "ccc"]


def test_drain_state_ignores_the_status_lines_own_ccc_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (tp#225 round B): `ccc statusline` under the bound claude is NOT a hook."""
    import json

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/opt/ccc hook stop"}]},
                        {"hooks": [{"type": "command", "command": "/opt/ccc hook release-locks"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    tokens = terminal.stop_hook_tokens("")
    table = _tree(pid5001="/path/to/python3 /opt/ccc statusline --capture-usage")
    assert terminal.drain_state(CLAUDE_PID, table, tokens, set()) == (terminal.DRAIN_DRAINED, [])


def test_drain_state_finds_a_hook_command_that_never_says_hook(tmp_path: Path) -> None:
    """The whole point of the token scan: a foreign commit script is not called '*hook*'."""
    cwd = tmp_path / "repo"
    _write_project_settings(cwd, "/bin/zsh /Users/me/scripts/commit-all.sh")
    tokens = terminal.stop_hook_tokens(str(cwd))
    table = _tree(pid5001="/bin/zsh /Users/me/scripts/commit-all.sh")
    state, running = terminal.drain_state(CLAUDE_PID, table, tokens, set())
    assert state == terminal.DRAIN_RUNNING
    assert running == ["/bin/zsh /Users/me/scripts/commit-all.sh"]


def test_drain_state_keeps_a_long_lived_false_positive_running() -> None:
    """The `hook` substring over-match is deliberate: a false positive only costs a refusal."""
    table = _tree(pid5001="node /opt/webhook-relay/server.js")
    state, running = terminal.drain_state(CLAUDE_PID, table, set(), set())
    assert state == terminal.DRAIN_RUNNING and running


def test_drain_state_is_drained_when_only_unrelated_children_remain() -> None:
    """MCP servers and editors are not hooks — a quiet chain is DRAINED, not RUNNING."""
    table = _tree(pid5001="node /opt/mcp/server.js", pid5002="/usr/bin/vim")
    assert terminal.drain_state(CLAUDE_PID, table, set(), set()) == (terminal.DRAIN_DRAINED, [])


def test_drain_state_unknown_on_a_failed_observation() -> None:
    """`snapshot.read_ps` returns {} on ANY failure — an empty table is never proof of quiet."""
    assert terminal.drain_state(CLAUDE_PID, {}, set(), set()) == (terminal.DRAIN_UNKNOWN, [])
    assert terminal.drain_state(0, _tree(), set(), set()) == (terminal.DRAIN_UNKNOWN, [])
    # The pid is gone from the table, or the number now belongs to something else.
    assert terminal.drain_state(999, _tree(), set(), set()) == (terminal.DRAIN_UNKNOWN, [])
    table = _tree()
    table[CLAUDE_PID] = _row(1, "node /opt/some-other-agent.js")  # pid recycled
    assert terminal.drain_state(CLAUDE_PID, table, set(), set()) == (terminal.DRAIN_UNKNOWN, [])


def test_own_branch_exclusion_never_hides_a_sibling_hook() -> None:
    """The caller runs inside the tree it watches: skip ITS branch only, never a sibling."""
    import os

    me = os.getpid()
    table = _tree(pid5001="/bin/zsh /Users/me/.claude/run-stop-hook.sh")
    table[me] = _row(CLAUDE_PID, "ccc close-now --session s1")
    own = terminal.own_branch_pids(CLAUDE_PID, table)
    assert own == {me}
    state, running = terminal.drain_state(CLAUDE_PID, table, set(), own)
    assert state == terminal.DRAIN_RUNNING and running == [
        "/bin/zsh /Users/me/.claude/run-stop-hook.sh"
    ]
    # Without the sibling the caller's own branch alone is a drained chain.
    del table[5001]
    assert terminal.drain_state(CLAUDE_PID, table, set(), own) == (terminal.DRAIN_DRAINED, [])


def test_own_branch_pids_walks_only_up_to_the_bound_pid() -> None:
    """A chain deeper than one level is collected whole; anything above *pid* is not."""
    import os

    me = os.getpid()
    table = _tree()
    table[7000] = _row(CLAUDE_PID, "/bin/zsh -c ccc close-now")
    table[me] = _row(7000, "ccc close-now --session s1")
    table[9999] = _row(1, "launchd")  # not on the chain at all
    assert terminal.own_branch_pids(CLAUDE_PID, table) == {me, 7000}
    assert terminal.own_branch_pids(0, table) == set()


def test_wait_for_stop_drain_needs_two_consecutive_clean_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook not yet spawned at the first scan must still prevent a DRAINED verdict."""
    from command_center import cli

    quiet = _tree(pid5001="node /opt/mcp/server.js")
    busy = _tree(pid5001="node /opt/mcp/server.js", pid5002="/bin/zsh /me/run-stop-hook.sh")
    scans = iter([quiet, busy, busy, busy, busy, busy, busy, busy])
    monkeypatch.setattr(cli, "_DRAIN_POLL_SEC", 0.0)
    monkeypatch.setattr(terminal, "stop_hook_tokens", lambda _cwd: set())
    monkeypatch.setattr(terminal, "ps_table", lambda: next(scans, busy))
    state, running = cli._wait_for_stop_drain(CLAUDE_PID, "", 0.05, 0.0)
    assert state == terminal.DRAIN_RUNNING and running


def test_wait_for_stop_drain_returns_drained_after_two_clean_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clean scans in a row (>= settle apart) are the proof close-now/switch-now need."""
    from command_center import cli

    quiet = _tree(pid5001="node /opt/mcp/server.js")
    monkeypatch.setattr(cli, "_DRAIN_POLL_SEC", 0.0)
    monkeypatch.setattr(terminal, "stop_hook_tokens", lambda _cwd: set())
    monkeypatch.setattr(terminal, "ps_table", lambda: quiet)
    assert cli._wait_for_stop_drain(CLAUDE_PID, "", 5.0, 0.0) == (terminal.DRAIN_DRAINED, [])


def test_wait_for_stop_drain_reports_unknown_when_ps_keeps_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An UNKNOWN scan resets the clean counter and is what the caller finally sees."""
    from command_center import cli

    quiet = _tree(pid5001="node /opt/mcp/server.js")
    scans = iter([quiet])  # one clean scan, then ps fails forever
    monkeypatch.setattr(cli, "_DRAIN_POLL_SEC", 0.0)
    monkeypatch.setattr(terminal, "stop_hook_tokens", lambda _cwd: set())
    monkeypatch.setattr(terminal, "ps_table", lambda: next(scans, {}))
    assert cli._wait_for_stop_drain(CLAUDE_PID, "", 0.05, 0.0) == (terminal.DRAIN_UNKNOWN, [])


# ---- probe_identity (tp#232) ----------------------------------------------
def test_probe_identity_types_every_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty table or start is PS_UNREADABLE, a missing pid PID_GONE — never an equal ""."""
    from command_center.snapshot import PsRow

    table = {7: PsRow(1, "ttys001", "S+", "claude"), 8: PsRow(1, "ttys001", "S", "node")}
    monkeypatch.setattr(terminal, "pid_start", lambda pid: "Tue" if pid in (7, 8) else "")
    assert terminal.probe_identity(0, table).kind == terminal.IDENTITY_PID_GONE
    assert terminal.probe_identity(7, {}).kind == terminal.IDENTITY_PS_UNREADABLE
    assert terminal.probe_identity(9, table).kind == terminal.IDENTITY_PID_GONE
    ok = terminal.probe_identity(7, table)
    assert (ok.kind, ok.start, ok.is_claude, ok.token(7)) == (
        terminal.IDENTITY_OK,
        "Tue",
        True,
        "7:Tue",
    )
    assert terminal.probe_identity(8, table).is_claude is False
    monkeypatch.setattr(terminal, "pid_start", lambda _pid: "")
    unreadable = terminal.probe_identity(7, table)
    assert unreadable.kind == terminal.IDENTITY_PS_UNREADABLE and unreadable.token(7) == ""
