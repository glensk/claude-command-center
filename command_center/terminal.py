#!/usr/bin/env python3
"""Open a session in a new terminal tab (macOS: iTerm2 only — never Terminal.app).

Used by the TUI's one-key "resume" action: open a fresh tab rooted in the
session's cwd and run ``claude --resume <id>``.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import shlex
import shutil
import subprocess
import sys

# Named tab colors → RGB (iTerm2 tab background). Hex "#rrggbb" is also accepted.
_TAB_COLORS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "green": (13, 162, 0),
    "blue": (52, 120, 246),
    "orange": (255, 170, 0),
    "yellow": (255, 191, 0),
    "purple": (175, 82, 222),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
}


def color_rgb(name: str | None) -> tuple[int, int, int] | None:
    """Resolve a color name or ``#rrggbb`` to an RGB triple (or None)."""
    if not name:
        return None
    name = name.strip().lower()
    if name in _TAB_COLORS:
        return _TAB_COLORS[name]
    if name.startswith("#") and len(name) == 7:
        try:
            return (int(name[1:3], 16), int(name[3:5], 16), int(name[5:7], 16))
        except ValueError:
            return None
    return None


def set_tab(title: str | None, rgb: tuple[int, int, int] | None) -> None:
    """Set the iTerm2/WezTerm tab title and/or background color via OSC escapes."""
    try:
        if title:
            sys.stdout.write(f"\033]1;{title}\a")
        if rgb:
            red, green, blue = rgb
            sys.stdout.write(f"\033]6;1;bg;red;brightness;{red}\a")
            sys.stdout.write(f"\033]6;1;bg;green;brightness;{green}\a")
            sys.stdout.write(f"\033]6;1;bg;blue;brightness;{blue}\a")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _as_quote(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript ``"..."`` literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _dispatch_title_script(checks: str, *, setup: str = "") -> None:
    """Run a fire-and-forget AppleScript that walks every iTerm session and applies
    *checks* (a body keyed on the per-session ``sid``). *setup* runs once up front."""
    script = f"""
    tell application "iTerm2"
        {setup}
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    set sid to id of s
                    {checks}
                end repeat
            end repeat
        end repeat
    end tell
    """
    try:
        # Detached, output discarded: fire-and-forget so the refresh never stalls.
        subprocess.Popen(  # noqa: S603  # pylint: disable=consider-using-with
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        pass


def set_session_titles(titles: dict[str, str]) -> None:
    """Set each iTerm session's tab name (sticky, beats claude/codex OSC titles).

    *titles* maps ``$ITERM_SESSION_ID`` (e.g. ``w0t1p0:UUID``) to the desired tab
    title. Setting the name via AppleScript persists where a shell-set OSC title is
    clobbered by the CLI running in the tab. Only changed names are written, and the
    whole thing runs detached so it never blocks the TUI. macOS / iTerm2 only.
    """
    pairs = [(sid.split(":")[-1].strip(), title) for sid, title in titles.items()]
    pairs = [(uuid, title) for uuid, title in pairs if uuid]
    if not pairs or not shutil.which("osascript"):
        return
    checks = "\n".join(
        f'if sid is "{_as_quote(uuid)}" then\n'
        f'  if name of s is not "{_as_quote(title)}" then set name of s to "{_as_quote(title)}"\n'
        f"end if"
        for uuid, title in pairs
    )
    _dispatch_title_script(checks)


def set_session_titles_preserving(cores: dict[str, str], marker: str = "🔴 ") -> None:
    """Set each tab's title to ``[marker]<core>``, preserving a leading wait marker.

    Like :func:`set_session_titles`, but instead of forcing the whole title it only
    rewrites the badge+folder **core** (e.g. ``"🟧 cscs-api"``), leaving any leading
    *marker* — the ``set-iterm-wait-marker.sh`` "🔴 " glyph that flags a session
    waiting on the user — in place. So a waiting tab converges to ``🔴 🟧 cscs-api``
    rather than being reset to ``🟧 cscs-api`` (which would drop the marker). The
    marker-strip mirrors that script's: count characters, slice past the marker.

    *cores* maps ``$ITERM_SESSION_ID`` to the desired core. Idempotent — only tabs
    whose current core differs are rewritten. Detached. macOS / iTerm2 only.
    """
    pairs = [(sid.split(":")[-1].strip(), core) for sid, core in cores.items()]
    pairs = [(uuid, core) for uuid, core in pairs if uuid]
    if not pairs or not shutil.which("osascript"):
        return
    marker_q = _as_quote(marker)
    checks = "\n".join(
        f'if sid is "{_as_quote(uuid)}" then\n'
        f"  set n to name of s\n"
        f'  if n starts with "{marker_q}" then\n'
        f"    if (count of n) > mlen then\n"
        f"      set body to text (mlen + 1) thru -1 of n\n"
        f"    else\n"
        f'      set body to ""\n'
        f"    end if\n"
        f'    if body is not "{_as_quote(core)}" then '
        f'set name of s to ("{marker_q}" & "{_as_quote(core)}")\n'
        f"  else\n"
        f'    if n is not "{_as_quote(core)}" then set name of s to "{_as_quote(core)}"\n'
        f"  end if\n"
        f"end if"
        for uuid, core in pairs
    )
    _dispatch_title_script(checks, setup=f'set mlen to (count of "{marker_q}")')


def reset_tab_color() -> None:
    """Clear any custom iTerm2 tab color (back to the theme default)."""
    try:
        sys.stdout.write("\033]6;1;bg;*;default\a")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _tmux_session() -> str:
    """The persistent tmux session that hosts launcher="tmux" windows (``tmux_session``).

    Configurable so it can match the session an SSH auto-attach / LaunchAgent bootstrap
    creates, so a resume fired from a phone SSH login lands in the session the user is
    attached to.
    """
    from . import config  # local import: this module is used by lightweight CLI paths

    return str(getattr(config.load_config(), "tmux_session", "ccc") or "ccc")


def _launcher_mode() -> str:
    """Effective launcher: the ``launcher`` config value, with a tmux auto-fallback.

    "iterm" (default) opens AppleScript tabs; "tmux" opens windows in the persistent
    tmux session (``tmux_session``). When the config says "iterm" but there is no
    ``osascript`` on PATH (Linux, Termux, an SSH-only box), the tmux path engages
    automatically so resume/start still work.
    """
    from . import config  # local import: this module is used by lightweight CLI paths

    mode = str(getattr(config.load_config(), "launcher", "iterm") or "iterm").lower()
    if mode != "tmux" and shutil.which("osascript") is None:
        return "tmux"
    return mode


def _tmux_window(command: str, cwd: str | None = None) -> bool:
    """Run *command* in a new window of the persistent tmux session (create if absent).

    The window runs via tmux's default ``sh -c``, so *command* must already be
    shell-quoted by the caller. Returns False when tmux is missing or errors —
    callers then fall back to their "run this manually" notify.
    """
    tmux = shutil.which("tmux")
    if tmux is None:
        return False
    session = _tmux_session()
    try:
        has = subprocess.run(
            [tmux, "has-session", "-t", session],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if has.returncode != 0:
            create = [tmux, "new-session", "-d", "-s", session]
            subprocess.run(create, capture_output=True, timeout=5, check=True)
        args = [tmux, "new-window", "-t", session]
        if cwd:
            args += ["-c", cwd]
        args.append(command)
        subprocess.run(args, capture_output=True, timeout=5, check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _tmux_pane_for_session(
    panes: list[tuple[str, int, str]],
    children: dict[int, list[tuple[int, str]]],
    commands: dict[int, str],
    session_id: str,
) -> tuple[str, str] | None:
    """The ``(window_target, pane_id)`` whose pane hosts *session_id*'s claude.

    *window_target* is e.g. ``"ccc:2"`` (select-window handle) and *pane_id* is e.g.
    ``"%3"`` (kill-pane handle). *panes* rows are ``(window_target, pane_pid, pane_id)``
    from ``list-panes``; *children* maps ``ppid -> [(pid, command), ...]`` (the traversal
    edges from ``ps``) and *commands* maps ``pid -> command`` (every process's own argv). A
    launchd-spawned job's pane runs ``ccc start-job``, which **execs** claude in place — so
    the ``--session-id`` argv lives on the *pane_pid itself*, not a child. The walk therefore
    checks each node's OWN command (starting at pane_pid) before descending. BFS with a
    visited set defends against a cyclic / duplicated ``ps`` snapshot; the first pane whose
    tree carries the session's claude wins.

    The needle is matched at a token boundary (end-of-string or whitespace after the id) so a
    short id like ``abc-123`` never spuriously matches ``--session-id abc-1234`` — a plain
    scan rather than a regex, which keeps this module free of the heavier ``re``/stub imports.
    """
    needle = f"--session-id {session_id}"

    def carries_session(command: str) -> bool:
        idx = command.find(needle)
        while idx != -1:
            end = idx + len(needle)
            if end == len(command) or command[end].isspace():
                return True
            idx = command.find(needle, idx + 1)
        return False

    for window_target, pane_pid, pane_id in panes:
        visited: set[int] = set()
        queue: list[int] = [pane_pid]  # a plain list is BFS enough for a tiny process tree
        while queue:
            pid = queue.pop(0)
            if pid in visited:
                continue
            visited.add(pid)
            if carries_session(commands.get(pid, "")):
                return window_target, pane_id
            for child_pid, _command in children.get(pid, []):
                if child_pid not in visited:
                    queue.append(child_pid)
    return None


def _tmux_process_tree() -> (
    tuple[list[tuple[str, int, str]], dict[int, list[tuple[int, str]]], dict[int, str]] | None
):
    """Gather ``(panes, children, commands)`` for the pane locator, or ``None`` on failure.

    Shells out to ``tmux list-panes -a`` (``(window_target, pane_pid, pane_id)`` rows) and
    ``ps -axo pid=,ppid=,command=`` (the process tree). Returns ``None`` when tmux is
    missing, lists no panes, or either command errors — callers then degrade (no focus / no
    close). Shared by :func:`focus_tmux_window` and :func:`tmux_pane_for_session`.
    """
    tmux = shutil.which("tmux")
    if tmux is None:
        return None
    try:
        listing = subprocess.run(
            [
                tmux,
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index} #{pane_pid} #{pane_id}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if listing.returncode != 0 or not listing.stdout.strip():
            return None
        panes: list[tuple[str, int, str]] = []
        for line in listing.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                panes.append((parts[0], int(parts[1]), parts[2]))
            except ValueError:
                continue
        if not panes:
            return None

        procs = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if procs.returncode != 0:
            return None
        children: dict[int, list[tuple[int, str]]] = {}
        commands: dict[int, str] = {}
        for line in procs.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) < 3:
                continue
            try:
                pid, ppid = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            commands[pid] = fields[2]
            children.setdefault(ppid, []).append((pid, fields[2]))
        return panes, children, commands
    except (subprocess.SubprocessError, OSError):
        return None


def tmux_pane_for_session(session_id: str) -> tuple[str, str] | None:
    """Locate the live tmux ``(window_target, pane_id)`` hosting *session_id*'s claude.

    Best-effort: scans ``tmux``/``ps`` (see :func:`_tmux_process_tree`) and walks each pane's
    process tree for the claude carrying ``--session-id <session_id>``. Returns ``None`` when
    tmux is absent or no pane hosts the session. Used by ``ccc close-now`` to ``kill-pane``.
    """
    gathered = _tmux_process_tree()
    if gathered is None:
        return None
    panes, children, commands = gathered
    return _tmux_pane_for_session(panes, children, commands, session_id)


def focus_tmux_window(session_id: str) -> bool:
    """Select + surface the tmux window hosting *session_id* (launchd-spawned jobs).

    ccc jobs fired by the launchd future-sync watcher land in tmux windows of the persistent
    session (``tmux_session``), not iTerm tabs — so they have no ``iterm_session_id`` and the
    plain ``focus_iterm_session`` path dead-ends. This locates the window by walking each
    pane's process tree for the claude carrying ``--session-id <session_id>`` (``ccc start-job``
    execs claude in place, so it is the pane_pid itself), selects that window, and brings it on
    screen: if a client is already attached to the session, the selected window is now visible
    and we just raise iTerm; otherwise a fresh iTerm tab attaches to the session.

    Returns False when tmux is absent, no pane's process tree contains the session's claude, or
    every surfacing attempt fails.
    """
    tmux = shutil.which("tmux")
    if tmux is None:
        return False
    gathered = _tmux_process_tree()
    if gathered is None:
        return False
    panes, children, commands = gathered
    located = _tmux_pane_for_session(panes, children, commands, session_id)
    if located is None:
        return False
    target, _pane_id = located
    try:
        if (
            subprocess.run(
                [tmux, "select-window", "-t", target],
                capture_output=True,
                timeout=5,
                check=False,
            ).returncode
            != 0
        ):
            return False

        tmux_session = target.split(":", 1)[0]
        clients = subprocess.run(
            [tmux, "list-clients", "-t", tmux_session],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if clients.returncode == 0 and clients.stdout.strip():
            # A terminal is already attached and now shows the selected window — just raise
            # iTerm. osascript failure is still success: the window IS already selected.
            _osascript('tell application "iTerm2" to activate')
            return True
        # No client attached: open a fresh iTerm tab that attaches to the session. No
        # _tmux_window fallback here — attaching from inside tmux is nonsense.
        command = f"tmux attach -t {shlex.quote(tmux_session)}"
        return _iterm(command) or _iterm_api_tab(command)
    except (subprocess.SubprocessError, OSError):
        return False


def resume_in_new_tab(
    cwd: str, session_id: str, config_dir: str = "", *, no_codex: bool = False
) -> bool:
    """Open a new terminal tab (or tmux window) in *cwd* running ``claude --resume <id>``.

    *config_dir* pins the Claude account and *no_codex* carries the session's Codex
    opt-out: the returned command string is prefixed with
    :func:`command_center.accounts.session_launch_env_prefix`, so the resume bills the
    session's OWN account (the default account unsets ``CLAUDE_CONFIG_DIR``; any other
    sets it) and inherits ``CCC_NO_CODEX=1`` when the row demands it — a fresh tab's
    ambient env can never silently bill the wrong account or re-enable Codex.
    """
    from .accounts import LaunchTarget, ensure_trusted, session_launch_env_prefix

    ensure_trusted(config_dir, cwd)  # the tab's claude must not park on the trust dialog
    prefix = session_launch_env_prefix(LaunchTarget(config_dir, no_codex))
    if _launcher_mode() == "tmux":
        return _tmux_window(f"{prefix}claude --resume {shlex.quote(session_id)}", cwd=cwd)
    command = f"{prefix}cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
    return _iterm(command) or _iterm_api_tab(command)


def resume_halted_in_new_tab(
    cwd: str, session_id: str, script_path: str, config_dir: str = "", *, no_codex: bool = False
) -> bool:
    """Open a new terminal tab that resumes a rate-limit-halted session.

    Runs ``<script_path> <id> now`` (claude-session-continue.py) in *cwd*. The
    orchestrator only dispatches once the limit reset is already confirmed (by the
    headless ``--wait-only`` detector), so ``now`` resumes immediately rather than
    re-probing; if the limit is secretly back the resume just 429s and the session
    re-halts (detected next tick) — no orphaned waiting tab. The tab keeps the
    resumed REPL open. Returns False (no launch) when *cwd* or *script_path* is
    missing. macOS / iTerm2 only.

    The whole shell command is shlex-quoted (for the shell) AND the resulting
    string is ``_as_quote``-escaped before it is embedded in the AppleScript
    ``"..."`` literal, so a cwd/path containing a double-quote can't break out.
    """
    import os

    from .accounts import LaunchTarget, ensure_trusted, session_launch_env_prefix

    if not cwd or not os.path.isdir(cwd) or not script_path:
        return False
    ensure_trusted(config_dir, cwd)
    prefix = session_launch_env_prefix(LaunchTarget(config_dir, no_codex))
    if _launcher_mode() == "tmux":
        return _tmux_window(
            f"{prefix}{shlex.quote(script_path)} {shlex.quote(session_id)} now", cwd=cwd
        )
    command = (
        f"{prefix}cd {shlex.quote(cwd)} && {shlex.quote(script_path)} {shlex.quote(session_id)} now"
    )
    return _iterm(command) or _iterm_api_tab(command)


def start_job_in_new_tab(session_id: str, force: bool = False, auto: bool = False) -> bool:
    """Open a new terminal tab that launches a parked future job via ``ccc start-job``.

    ``ccc start-job`` reads the draft's cwd + prompt from the store, clears the draft
    flag, then execs ``claude --session-id <id> "<prompt>"`` in that repo — so the
    prompt never has to survive shell/AppleScript quoting (it is passed via env).
    *force* passes ``--force`` through, skipping the premature-start confirmation
    (used by the TUI after its own ConfirmScreen already asked). *auto* passes
    ``--auto`` (unattended dispatch, e.g. the daemon's parked-prompt fire): guards
    are never bypassed — a tripped one disarms the job instead of asking.
    """
    flag = (" --force" if force else "") + (" --auto" if auto else "")
    command = f"ccc start-job{flag} {shlex.quote(session_id)}"
    if _launcher_mode() == "tmux":
        return _tmux_window(command)
    # Under launchd (the WatchPaths agent) osascript is TCC-blocked, so the
    # Python-API tab is the working path there; tmux stays the last resort for
    # a Mac with iTerm not running / API disabled — the job still launches.
    return _iterm(command) or _iterm_api_tab(command) or _tmux_window(command)


def focus_iterm_session(iterm_session_id: str) -> bool:
    """Bring the existing iTerm tab/window for *iterm_session_id* to the front.

    ``iterm_session_id`` is the value of ``$ITERM_SESSION_ID`` (e.g. ``w0t1p0:UUID``);
    the AppleScript session ``id`` is the UUID after the colon.
    """
    uuid = iterm_session_id.split(":")[-1].strip()
    if not uuid:
        return False
    script = f'''
    tell application "iTerm2"
        activate
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                    if id of aSession is "{uuid}" then
                        select aWindow
                        tell aTab to select
                        return "true"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "false"
    '''
    out = _osascript(script)
    return out is not None and "true" in out.lower()


def focus_tty(tty: str) -> bool:
    """Bring the iTerm2 tab whose session device is *tty* to the front.

    *tty* is a device path like ``/dev/ttys001`` (as reported by ``ps`` and by
    iTerm's ``tty of session``) — the title-independent way to locate a tab by the
    process running in it. Selects the matching session, its tab and window, then
    activates iTerm2. Returns True if found and focused. macOS / iTerm2 only.
    """
    if not tty:
        return False
    script = f'''
    tell application "iTerm2"
        activate
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                    if tty of aSession is "{_as_quote(tty)}" then
                        select aWindow
                        tell aTab to select
                        tell aSession to select
                        return "true"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "false"
    '''
    out = _osascript(script)
    return out is not None and "true" in out.lower()


def focus_session_name(needle: str) -> bool:
    """Bring the first iTerm2 tab whose session *name* contains *needle* to the front.

    A title-based fallback for :func:`focus_tty` (e.g. the ccc TUI tab title ``!!!``).
    macOS / iTerm2 only.
    """
    if not needle:
        return False
    script = f'''
    tell application "iTerm2"
        activate
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                    if (name of aSession) contains "{_as_quote(needle)}" then
                        select aWindow
                        tell aTab to select
                        tell aSession to select
                        return "true"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "false"
    '''
    out = _osascript(script)
    return out is not None and "true" in out.lower()


def launch_ccc_tab() -> bool:
    """Open a new iTerm2 tab running the ``ccc`` TUI (iTerm2 only)."""
    return _iterm("ccc") or _iterm_api_tab("ccc")


def is_iterm_frontmost() -> bool:
    """True if iTerm2 is the frontmost (active) macOS application.

    Uses ``lsappinfo`` (no Accessibility/Automation permission, unlike a System
    Events query). Lets ``ccc jump`` tell "I'm looking at iTerm" (toggle) apart from
    "I'm in another app" (just bring ccc forward).
    """
    if not shutil.which("lsappinfo"):
        return False
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=3, check=False
        ).stdout.strip()
        if not asn:
            return False
        info = subprocess.run(
            ["lsappinfo", "info", "-only", "bundleID", asn],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return "com.googlecode.iterm2" in info


def current_iterm_session() -> tuple[str, str] | None:
    """Return ``(session_uuid, tty)`` of iTerm2's current session, or None.

    The UUID is iTerm's AppleScript ``id of session`` — the tail of a stored
    ``$ITERM_SESSION_ID`` (``w0t1p0:UUID``); the tty is e.g. ``/dev/ttys001``.
    """
    out = _osascript(
        """
        tell application "iTerm2"
            set cs to current session of current window
            return (id of cs) & "|" & (tty of cs)
        end tell
        """
    )
    if not out:
        return None
    parts = out.strip().split("|", 1)
    if len(parts) != 2 or not parts[0]:
        return None
    return parts[0], parts[1]


def close_iterm_session(iterm_session_id: str) -> str:
    """Close the iTerm pane (and its tab, if it was the only pane) for a session.

    *iterm_session_id* is ``$ITERM_SESSION_ID`` (e.g. ``w0t1p0:UUID``); the
    AppleScript session ``id`` is the UUID after the colon. Returns ``"tab"`` if the
    whole tab closed (the session was the only pane), ``"session"`` if just that
    pane closed, or ``""`` if it could not be located / no iTerm / no osascript.

    Callers should SIGTERM the session's process first so the pane is running only
    its shell — iTerm then closes it without the "a job is still running" prompt
    under the common "confirm only if there are jobs besides the shell" setting.
    """
    uuid = iterm_session_id.split(":")[-1].strip()
    if not uuid:
        return ""
    script = f'''
    tell application "iTerm2"
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                    if id of aSession is "{uuid}" then
                        set paneCount to (count of sessions of aTab)
                        if paneCount <= 1 then
                            close aTab
                            return "tab"
                        else
                            close aSession
                            return "session"
                        end if
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return ""
    '''
    out = _osascript(script)
    if out is None:
        return ""
    result = out.strip().lower()
    return result if result in ("tab", "session") else ""


def _osascript(script: str) -> str | None:
    """Run an AppleScript; return its stdout on success, or None on failure."""
    if not shutil.which("osascript"):
        return None
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def _iterm_api_tab(command: str) -> bool:
    """Create an iTerm tab over the Python-API websocket (no TCC Apple events).

    launchd contexts (the WatchPaths future-sync agent, the daemon) cannot
    osascript iTerm — TCC Automation silently denies without ever prompting —
    but iTerm's own API socket is outside TCC, so a phone-launched job still
    lands in a real, locatable iTerm tab (verified 2026-07-17: cookie-less
    connect from a non-iTerm env succeeds). Degrades False on anything missing
    (``iterm2`` package, the API setting, iTerm itself) so callers fall through.
    """
    try:
        import asyncio  # pylint: disable=import-outside-toplevel

        import iterm2  # pylint: disable=import-outside-toplevel

        async def _go() -> bool:
            conn = await asyncio.wait_for(iterm2.Connection.async_create(), timeout=8)
            app = await iterm2.async_get_app(conn, create_if_needed=True)
            if app is None:
                return False
            window = app.current_terminal_window
            if window is None:
                window = await iterm2.Window.async_create(conn)
                tab = window.current_tab if window else None
            else:
                tab = await window.async_create_tab()
            if tab is None:
                return False
            session = tab.current_session
            if session is None:
                return False
            await session.async_send_text(command + "\n")
            await app.async_activate()
            return True

        return bool(asyncio.run(_go()))
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def send_text_to_session(iterm_session_id: str, text: str) -> bool:
    """Type *text* into an EXISTING iTerm session (by ``$ITERM_SESSION_ID``) and submit.

    The delivery path for a parked prompt attached to a live Claude session: the
    text goes in wrapped in bracketed-paste markers — a multi-line prompt must
    arrive as ONE paste, since a bare newline would submit each line separately —
    followed, after a beat for the composer to ingest the paste, by a lone CR that
    submits it. Python-API socket (TCC-free, works from the launchd daemon);
    ``False`` on any failure so the caller can fall back to a resume tab.
    """
    uuid = (iterm_session_id or "").split(":")[-1].strip()
    if not uuid or not text:
        return False
    try:
        import asyncio  # pylint: disable=import-outside-toplevel

        import iterm2  # pylint: disable=import-outside-toplevel

        async def _go() -> bool:
            conn = await asyncio.wait_for(iterm2.Connection.async_create(), timeout=8)
            app = await iterm2.async_get_app(conn)
            if app is None:
                return False
            session = app.get_session_by_id(uuid)
            if session is None:
                return False
            await session.async_send_text("\x1b[200~" + text + "\x1b[201~")
            await asyncio.sleep(0.4)
            await session.async_send_text("\r")
            return True

        return bool(asyncio.run(_go()))
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def fire_attached_in_new_tab(session_id: str) -> bool:
    """Open a new tab that resumes a session WITH its parked prompt.

    The fallback when an attached prompt's own session tab is gone at fire time:
    the tab runs ``ccc fire-attached <id>``, which claims the delivery (one-shot
    conditional ``fire_at`` update), pins the session's account, and execs
    ``claude --resume <id> "<prompt>"`` in the session's cwd — the prompt itself
    never has to survive shell/AppleScript quoting.
    """
    command = f"ccc fire-attached {shlex.quote(session_id)}"
    if _launcher_mode() == "tmux":
        return _tmux_window(command)
    return _iterm(command) or _iterm_api_tab(command) or _tmux_window(command)


def _iterm(command: str) -> bool:
    # command is already shell-quoted; _as_quote escapes it for the AppleScript "..."
    # literal too, so a cwd/path containing a double-quote can't break out.
    escaped = _as_quote(command)
    script = f'''
    tell application "iTerm2"
        if (count of windows) = 0 then
            create window with default profile
        else
            tell current window to create tab with default profile
        end if
        tell current session of current window to write text "{escaped}"
        activate
    end tell
    '''
    return _osascript(script) is not None


# Standing rule (2026-09-01): a launch never falls back to Terminal.app. On
# 2026-09-01 a `ccc start-job` dispatched by gitlab-ci-watch's acceptance test
# reached the old `_terminal_app` fallback and opened a Terminal.app window that
# nobody was watching (and parked on the trust dialog). iTerm2 (AppleScript,
# then the Python API) or tmux — or a loud False — are the only outcomes.
