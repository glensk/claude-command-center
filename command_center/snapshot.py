#!/usr/bin/env python3
"""``ccc snapshot`` / ``ccc restore-snapshot`` — save and rebuild the whole iTerm layout.

Before a reboot (a macOS update, a kernel panic you saw coming) ``ccc snapshot`` writes
the *shape* of your desk to a JSON file: every iTerm window, its tabs, each tab's split
topology, and what each pane was doing. ``ccc restore-snapshot`` rebuilds that shape —
every Claude Code session resumed under **its own account** (``claude --resume <id>``),
every other pane re-running the **startup invocation** it was launched with (``vim`` at
its file, ``man git``, ``htop``, ``ssh …``) when that program is on the configurable
``snapshot_restore_commands`` allowlist, else a shell at the recorded cwd with a printed
note of what used to run there.

The contract is deliberately "re-run the startup invocation", not "restore program
state": a ``vim`` that switched buffers reopens with its original argv.

Layering — everything below the iTerm boundary is pure and unit-tested:

* **Neutral model** (:class:`SnapPane` / :class:`SnapNode` / :class:`SnapTab` /
  :class:`SnapWindow`) plus a versioned JSON schema (``schema_version: 1``).
* **Capture** — a thin async collector reads iTerm's own variables per pane
  (``id``/``tty``/``jobPid``/``jobName``/``commandLine``/``path``/``name``) walking
  ``tab.root``'s splitter tree (``tab.sessions`` is unordered and is NEVER used for
  layout), and the pure :func:`build_snapshot` classifies those raw panes against the
  live Claude registry, one ``ps`` pass, exact ``KERN_PROCARGS2`` argv and one batched
  ``lsof`` cwd read.
* **Restore** — the pure :func:`build_plan` turns a snapshot into per-pane actions
  (skip / resume / re-run / note), and a two-phase async executor builds the whole empty
  layout first, then delivers each pane's command. Fresh tabs are addressed through
  ``tab.sessions`` — never ``current_session`` (see :func:`_sole_session`) — and the
  command is typed once the pane's foreground job has settled on the idle shell
  (:func:`_await_shell`, bounded), never into a shell still running its startup files.

Safety model: commands are rebuilt ONLY from exact ``KERN_PROCARGS2`` argv captured from
your own processes, re-quoted element-by-element with :func:`shlex.quote`. Free-text
fields (iTerm's ``commandLine``, ``ps``'s display string) are never executed — they only
ever reach a ``printf`` note. Snapshot files live 0600 in ccc's own 0700 state dir.

v1 is macOS + the iTerm2 Python API only: with ``launcher = "tmux"``, or with iTerm's
"Enable Python API" off, both commands refuse with a clear message rather than guessing.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from . import config
from .links import local_link
from .models import LiveSession

# pylint: disable=too-many-lines  # one cohesive feature: capture, plan, restore, render

SCHEMA_VERSION = 1

#: Directory / file modes for the snapshot store (chmod'd after creation to beat umask).
DIR_MODE = 0o700
FILE_MODE = 0o600

#: Programs whose captured argv is re-run verbatim on restore (config
#: ``snapshot_restore_commands`` overrides). Anything else restores as a shell + note.
DEFAULT_RESTORE_COMMANDS: tuple[str, ...] = (
    "vi",
    "vim",
    "nvim",
    "less",
    "man",
    "tail",
    "htop",
    "btop",
    "top",
    "ccc",
    "ssh",
)

#: argv0 basenames (leading ``-`` of a login shell stripped) that count as "just a shell".
SHELL_NAMES = frozenset({"zsh", "bash", "fish", "sh", "dash", "tcsh"})

#: Interpreters that exec a ``#!``-script: the allowlist must match the SCRIPT's name, not
#: theirs (``python3 …/bin/ccc`` is "ccc"). ``python``/``python3*`` are handled by prefix.
_INTERPRETERS = frozenset({"perl", "ruby", "node", "bun"})

#: Prefix of the note a non-allowlisted pane prints into its restored shell.
RESTORE_NOTE_PREFIX = "[ccc-restore] was running: "

_API_HINT = (
    "iTerm2's Python API is unavailable — enable it in iTerm2 → Settings → General → "
    "Magic → “Enable Python API” (and run this from a machine with iTerm2 running)."
)
_TMUX_HINT = (
    'snapshot/restore is iTerm2-only in v1, but launcher = "tmux" is configured — '
    "tmux already persists its own layout (`tmux attach`)."
)

# KERN_PROCARGS2 sysctl MIB: CTL_KERN=1, KERN_ARGMAX=8, KERN_PROCARGS2=49.
_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROCARGS2 = 49
_ARGMAX_FALLBACK = 262_144


# --------------------------------------------------------------------------- #
# Neutral layout model
# --------------------------------------------------------------------------- #
@dataclass
class SnapPane:  # pylint: disable=too-many-instance-attributes
    """One captured pane: what ran in it, and where.

    ``kind`` is ``"claude"`` (a tracked Claude Code session — ``session_id`` +
    ``config_dir`` say which id on which account), ``"command"`` (a foreground program
    whose exact ``argv`` we captured) or ``"shell"`` (nothing but the shell, or a
    program whose argv could not be read — ``display`` then names it for the note).
    ``no_codex`` mirrors the session row's Codex opt-out so a restored pane comes back
    with the same ``CCC_NO_CODEX`` pin it had.
    """

    kind: str
    cwd: str
    title: str = ""
    session_id: str = ""
    config_dir: str = ""
    no_codex: bool = False
    argv: list[str] = field(default_factory=list)
    display: str = ""


@dataclass
class SnapNode:
    """A splitter: ``split`` is the divider orientation, ``children`` are ordered."""

    split: str  # "vertical" | "horizontal"
    children: list[SnapNode | SnapPane] = field(default_factory=list)


@dataclass
class SnapTab:
    """One tab — a single pane, or a splitter tree of them."""

    tree: SnapNode | SnapPane


@dataclass
class SnapWindow:
    """One iTerm window: its frame (``[x, y, w, h]``), its tabs, and which was selected."""

    frame: tuple[int, int, int, int] | None = None
    selected_tab: int = 0
    tabs: list[SnapTab] = field(default_factory=list)


class PsRow(NamedTuple):
    """One ``ps -axo pid=,ppid=,tty=,stat=,command=`` row (keyed by pid by the caller)."""

    ppid: int
    tty: str
    stat: str
    command: str


ProcArgs = Callable[[int], list[str] | None]


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def pane_to_json(pane: SnapPane) -> dict[str, Any]:
    """Serialize a pane, omitting every empty field (``kind`` is always present)."""
    out: dict[str, Any] = {"kind": pane.kind}
    for key in ("cwd", "title", "session_id", "config_dir", "display"):
        value = getattr(pane, key)
        if value:
            out[key] = value
    if pane.argv:
        out["argv"] = list(pane.argv)
    if pane.no_codex:  # omitted when false, so existing snapshot files stay byte-identical
        out["no_codex"] = True
    return out


def node_to_json(node: SnapNode | SnapPane) -> dict[str, Any]:
    """Serialize a tree node: ``{"split": …, "children": […]}`` or ``{"pane": {…}}``."""
    if isinstance(node, SnapPane):
        return {"pane": pane_to_json(node)}
    return {"split": node.split, "children": [node_to_json(c) for c in node.children]}


def windows_to_json(windows: Sequence[SnapWindow], created_at: str) -> dict[str, Any]:
    """The full on-disk document for *windows* (schema v1)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at,
        "windows": [
            {
                "frame": list(win.frame) if win.frame else None,
                "selected_tab": win.selected_tab,
                "tabs": [{"tree": node_to_json(tab.tree)} for tab in win.tabs],
            }
            for win in windows
        ],
    }


def pane_from_json(data: dict[str, Any]) -> SnapPane:
    """Rebuild a :class:`SnapPane` from its JSON form (missing fields default empty)."""
    argv = data.get("argv") or []
    return SnapPane(
        kind=str(data.get("kind") or "shell"),
        cwd=str(data.get("cwd") or ""),
        title=str(data.get("title") or ""),
        session_id=str(data.get("session_id") or ""),
        config_dir=str(data.get("config_dir") or ""),
        no_codex=bool(data.get("no_codex")),
        argv=[str(a) for a in argv],
        display=str(data.get("display") or ""),
    )


def node_from_json(data: dict[str, Any]) -> SnapNode | SnapPane:
    """Rebuild a tree node from its JSON form."""
    if "pane" in data:
        return pane_from_json(data.get("pane") or {})
    children = data.get("children") or []
    return SnapNode(
        split="vertical" if data.get("split") == "vertical" else "horizontal",
        children=[node_from_json(c) for c in children],
    )


def windows_from_json(data: dict[str, Any]) -> list[SnapWindow]:
    """Rebuild the window list from a loaded snapshot document."""
    windows: list[SnapWindow] = []
    for raw in data.get("windows") or []:
        frame_raw = raw.get("frame")
        frame: tuple[int, int, int, int] | None = None
        if isinstance(frame_raw, (list, tuple)) and len(frame_raw) == 4:
            frame = (
                int(frame_raw[0]),
                int(frame_raw[1]),
                int(frame_raw[2]),
                int(frame_raw[3]),
            )
        tabs = [SnapTab(node_from_json(t.get("tree") or {})) for t in raw.get("tabs") or []]
        windows.append(
            SnapWindow(frame=frame, selected_tab=int(raw.get("selected_tab") or 0), tabs=tabs)
        )
    return windows


def iter_panes(node: SnapNode | SnapPane) -> list[SnapPane]:
    """Every leaf pane of *node*, in tree (left-to-right, top-to-bottom) order."""
    if isinstance(node, SnapPane):
        return [node]
    out: list[SnapPane] = []
    for child in node.children:
        out.extend(iter_panes(child))
    return out


def count_json_panes(node: dict[str, Any]) -> int:
    """Leaf-pane count of a JSON tree node (used by the ``--list`` summary)."""
    if "pane" in node:
        return 1
    return sum(count_json_panes(c) for c in node.get("children") or [])


# --------------------------------------------------------------------------- #
# Process observation — pure parsers + their live wrappers
# --------------------------------------------------------------------------- #
def normalize_tty(value: str | None) -> str:
    """Normalize a tty spelling to ``/dev/<name>`` (``""`` for none — ``ps``'s ``??``)."""
    text = (value or "").strip()
    if not text or text in {"?", "??", "-"}:
        return ""
    if text.startswith("/dev/"):
        return text
    return f"/dev/{text}"


def parse_ps(text: str) -> dict[int, PsRow]:
    """Parse ``ps -axo pid=,ppid=,tty=,stat=,command=`` into ``{pid: PsRow}``.

    The command column keeps its spaces (it is a display string only — never executed),
    so the split is capped at four.
    """
    rows: dict[int, PsRow] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[4] if len(parts) > 4 else ""
        rows[pid] = PsRow(ppid=ppid, tty=normalize_tty(parts[2]), stat=parts[3], command=command)
    return rows


def read_ps() -> dict[int, PsRow]:
    """One live ``ps`` pass (empty on any failure — every caller degrades)."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,tty=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_ps(result.stdout) if result.returncode == 0 else {}


def parse_lsof_cwds(text: str) -> dict[int, str]:
    """Parse ``lsof -a -p <pids> -d cwd -Fpn`` field output into ``{pid: cwd}``."""
    out: dict[int, str] = {}
    pid: int | None = None
    for line in text.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(value)
            except ValueError:
                pid = None
        elif tag == "n" and pid is not None:
            out.setdefault(pid, value)
    return out


def read_cwds(pids: Sequence[int]) -> dict[int, str]:
    """Batched cwd read for *pids* — one ``lsof`` call (empty on any failure)."""
    wanted = sorted({p for p in pids if p > 0})
    if not wanted:
        return {}
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", ",".join(str(p) for p in wanted), "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    # lsof exits 1 when *some* pid is gone but still prints the rest — parse regardless.
    return parse_lsof_cwds(result.stdout)


def parse_procargs2(raw: bytes) -> list[str]:
    """Decode a ``KERN_PROCARGS2`` blob into the process's exact ``argv``.

    Layout: a little-endian ``int32`` ``argc``, the NUL-terminated executable path, NUL
    padding, then ``argc`` NUL-separated argv strings. Decoding is lossy-safe
    (``errors="replace"``) — an undecodable byte must never crash a capture.
    """
    if len(raw) < 5:
        return []
    argc = int.from_bytes(raw[:4], "little", signed=False)
    if argc <= 0 or argc > 4096:
        return []
    body = raw[4:]
    end = body.find(b"\0")
    if end < 0:
        return []
    pos = end + 1
    while pos < len(body) and body[pos] == 0:  # padding between exec_path and argv[0]
        pos += 1
    args: list[str] = []
    while len(args) < argc and pos < len(body):
        end = body.find(b"\0", pos)
        if end < 0:
            args.append(body[pos:].decode("utf-8", errors="replace"))
            break
        args.append(body[pos:end].decode("utf-8", errors="replace"))
        pos = end + 1
    return args


def procargs(pid: int) -> list[str] | None:
    """The exact ``argv`` of *pid* via ``sysctl(KERN_PROCARGS2)``; ``None`` when unknown.

    macOS-only (the MIB does not exist elsewhere) and best-effort: a vanished process, a
    process owned by another user, or a non-Darwin platform all yield ``None``, which the
    classifier reads as "record it as a shell + note instead of a command".
    """
    if sys.platform != "darwin" or pid <= 0:
        return None
    try:
        import ctypes  # pylint: disable=import-outside-toplevel
        import ctypes.util  # pylint: disable=import-outside-toplevel

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        argmax = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(argmax))
        mib2 = (ctypes.c_int * 2)(_CTL_KERN, _KERN_ARGMAX)
        if libc.sysctl(mib2, 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
            argmax.value = _ARGMAX_FALLBACK
        cap = argmax.value if 0 < argmax.value <= 4 * _ARGMAX_FALLBACK else _ARGMAX_FALLBACK
        buf = ctypes.create_string_buffer(cap)
        size = ctypes.c_size_t(cap)
        mib3 = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        if libc.sysctl(mib3, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        args = parse_procargs2(buf.raw[: size.value])
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return None
    return args or None


# --------------------------------------------------------------------------- #
# Pure classifier
# --------------------------------------------------------------------------- #
def argv0_name(command: str) -> str:
    """The program name of a ``ps`` command string: basename of argv0, ``-`` stripped."""
    head = (command or "").strip().split(None, 1)
    if not head:
        return ""
    return os.path.basename(head[0]).lstrip("-")


def is_shell_command(command: str) -> bool:
    """True when a ``ps`` command string is nothing but an interactive shell."""
    return argv0_name(command) in SHELL_NAMES


def _is_interpreter(name: str) -> bool:
    """True for an interpreter that runs the script named by the NEXT argv element."""
    return name == "python" or name.startswith("python3") or name in _INTERPRETERS


def effective_name(parts: Sequence[str]) -> str:
    """The program name the allowlist should be matched against — seeing through wrappers.

    A ``#!``-script is exec'd as ``<interpreter> <script> …``, so plain ``argv[0]``
    matching would call the ccc TUI "python3" and never relaunch it despite ``ccc`` being
    allowlisted. When argv0 is an interpreter and the next element is a path (not a flag —
    ``python3 -m http.server`` stays ``python3``), that script's basename is the effective
    name. Everything else is just ``argv0`` with a login shell's leading ``-`` stripped.
    """
    if not parts:
        return ""
    name = os.path.basename(parts[0]).lstrip("-")
    if _is_interpreter(name) and len(parts) >= 2 and not parts[1].startswith("-"):
        return os.path.basename(parts[1])
    return name


def ancestors(ps_rows: dict[int, PsRow], pid: int) -> list[int]:
    """*pid*'s ancestor pids, parent first, stopping at pid 1 / a cycle / an unknown pid."""
    out: list[int] = []
    seen = {pid}
    current = ps_rows.get(pid)
    while current is not None and current.ppid > 1 and current.ppid not in seen:
        out.append(current.ppid)
        seen.add(current.ppid)
        current = ps_rows.get(current.ppid)
    return out


def descendants(ps_rows: dict[int, PsRow], pid: int) -> set[int]:
    """Every transitive child of *pid*."""
    children: dict[int, list[int]] = {}
    for child, row in ps_rows.items():
        children.setdefault(row.ppid, []).append(child)
    out: set[int] = set()
    stack = list(children.get(pid, []))
    while stack:
        current = stack.pop()
        if current in out:
            continue
        out.add(current)
        stack.extend(children.get(current, []))
    return out


def own_subtree(ps_rows: dict[int, PsRow], own_pid: int) -> set[int]:
    """Pids belonging to the snapshot process itself — never a pane's "foreground" job.

    ``ccc snapshot`` runs *inside* one of the panes it is capturing, so its own process,
    everything it spawned (``ps``/``lsof``), and the wrappers between it and its login
    shell (``uv run`` → ``python`` → ``ccc``) would otherwise be recorded as that pane's
    program. The walk up stops **at** the pane's login shell without excluding it, so the
    pane still classifies as a plain shell at its cwd.
    """
    if own_pid <= 0:
        return set()
    out = {own_pid} | descendants(ps_rows, own_pid)
    for pid in ancestors(ps_rows, own_pid):
        row = ps_rows.get(pid)
        if row is None or is_shell_command(row.command):
            break
        out.add(pid)
    return out


def shell_pid_for_tty(ps_rows: dict[int, PsRow], tty: str) -> int | None:
    """The pane's login shell on *tty*: the shallowest shell process attached to it."""
    best: tuple[int, int] | None = None
    for pid, row in ps_rows.items():
        if row.tty != tty or not is_shell_command(row.command):
            continue
        key = (len(ancestors(ps_rows, pid)), pid)
        if best is None or key < best:
            best = key
    return best[1] if best else None


def foreground_pid(
    ps_rows: dict[int, PsRow], tty: str, job_pid: int | None, excluded: set[int]
) -> int | None:
    """The pane's foreground process: iTerm's ``jobPid``, else the deepest ``+`` job.

    iTerm's own ``jobPid`` is authoritative when it names a process we can still see;
    the fallback scans *tty* for the deepest process whose ``ps`` state carries the
    foreground-group ``+`` flag (ties broken by the higher, i.e. younger, pid).
    """
    if job_pid and job_pid in ps_rows and job_pid not in excluded:
        return job_pid
    best: tuple[int, int] | None = None
    for pid, row in ps_rows.items():
        if pid in excluded or row.tty != tty or "+" not in row.stat:
            continue
        key = (len(ancestors(ps_rows, pid)), pid)
        if best is None or key > best:
            best = key
    return best[1] if best else None


def allowlisted_ancestor(
    ps_rows: dict[int, PsRow], pid: int, shell_pid: int | None, allowlist: Sequence[str]
) -> int:
    """Walk *pid* up toward its login shell and return the HIGHEST allowlisted process.

    ``man git`` spawns a ``less`` child that owns the terminal, so the deepest foreground
    process is the pager — but re-running ``less`` restores nothing. Picking the topmost
    allowlisted ancestor instead restores ``man git``. With nothing allowlisted on the
    chain, *pid* itself is kept.

    Matching goes through :func:`effective_name`, so it sees a ``python3 …/bin/ccc``
    ancestor as "ccc" — the same rule :func:`_pane_action` applies at restore time. Only
    the MATCH uses ``ps``'s display text; the argv that is actually re-run still comes
    from ``KERN_PROCARGS2``.
    """
    names = set(allowlist)
    best = pid
    for candidate in [pid, *ancestors(ps_rows, pid)]:
        if candidate == shell_pid:
            break
        row = ps_rows.get(candidate)
        if row is None or is_shell_command(row.command):
            break
        if effective_name(row.command.split()) in names:
            best = candidate
    return best


def _live_pane_index(
    live: Sequence[LiveSession], store_rows: dict[str, str], ps_rows: dict[int, PsRow]
) -> tuple[dict[str, LiveSession], dict[str, LiveSession]]:
    """Two lookup tables for Claude panes: by iTerm pane uuid, and by tty."""
    by_uuid: dict[str, LiveSession] = {}
    by_tty: dict[str, LiveSession] = {}
    for session in live:
        if not session.alive:
            continue
        uuid = (store_rows.get(session.session_id) or "").split(":")[-1].strip().upper()
        if uuid:
            by_uuid.setdefault(uuid, session)
        row = ps_rows.get(session.pid)
        if row is not None and row.tty:
            by_tty.setdefault(row.tty, session)
    return by_uuid, by_tty


def _classify_pane(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-return-statements
    raw: dict[str, Any],
    by_uuid: dict[str, LiveSession],
    by_tty: dict[str, LiveSession],
    ps_rows: dict[int, PsRow],
    args_of: ProcArgs,
    cwds: dict[int, str],
    excluded: set[int],
    allowlist: Sequence[str],
) -> SnapPane:
    """Turn one RAW pane dict into a :class:`SnapPane` (see the module docstring)."""
    title = str(raw.get("name") or "")
    tty = normalize_tty(raw.get("tty"))
    uuid = str(raw.get("uuid") or "").strip().upper()
    session = by_uuid.get(uuid) or (by_tty.get(tty) if tty else None)
    if session is not None:
        return SnapPane(
            kind="claude",
            cwd=session.cwd,
            title=title,
            session_id=session.session_id,
            config_dir=session.config_dir,
            no_codex=bool(getattr(session, "no_codex", False)),
        )

    shell_pid = shell_pid_for_tty(ps_rows, tty) if tty else None
    job_pid = raw.get("job_pid")
    candidate = foreground_pid(ps_rows, tty, int(job_pid) if job_pid else None, excluded)
    shell_cwd = cwds.get(shell_pid or -1, "")
    fallback_cwd = str(raw.get("path") or "")
    if candidate is None:
        return SnapPane(kind="shell", cwd=shell_cwd or fallback_cwd, title=title)

    row = ps_rows.get(candidate)
    if row is None or is_shell_command(row.command):
        cwd = cwds.get(candidate, "") or shell_cwd or fallback_cwd
        return SnapPane(kind="shell", cwd=cwd, title=title)

    chosen = allowlisted_ancestor(ps_rows, candidate, shell_pid, allowlist)
    chosen_row = ps_rows.get(chosen) or row
    display = str(raw.get("command_line") or "").strip() or chosen_row.command
    cwd = cwds.get(chosen, "") or cwds.get(candidate, "") or shell_cwd or fallback_cwd
    argv = args_of(chosen)
    if not argv:
        # No exact argv (another user's process, or a non-Darwin box): a shell + note is
        # the honest restore — we never reconstruct a command line from display text.
        return SnapPane(kind="shell", cwd=cwd, title=title, display=display)
    return SnapPane(kind="command", cwd=cwd, title=title, argv=list(argv), display=display)


def _classify_tree(
    node: dict[str, Any], classify: Callable[[dict[str, Any]], SnapPane]
) -> SnapNode | SnapPane:
    """Map a RAW splitter tree onto the neutral model, preserving child order."""
    if "pane" in node:
        return classify(node.get("pane") or {})
    children = [_classify_tree(c, classify) for c in node.get("children") or []]
    if len(children) == 1:  # a one-child splitter is just its pane
        return children[0]
    return SnapNode(
        split="vertical" if node.get("split") == "vertical" else "horizontal",
        children=children,
    )


def build_snapshot(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    raw_windows: Sequence[dict[str, Any]],
    live: Sequence[LiveSession],
    store_rows: dict[str, str],
    ps_rows: dict[int, PsRow],
    args_of: ProcArgs,
    cwds: dict[int, str],
    own_pid: int,
    allowlist: Sequence[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Classify a RAW iTerm layout walk into the on-disk snapshot document (schema v1).

    Pure: every OS fact arrives as an argument — the raw tree from the iTerm collector,
    the live Claude registry (*live* + *store_rows*, ``session_id → iterm_session_id``),
    one ``ps`` pass, an ``argv`` reader, the batched ``lsof`` cwds, and this process's own
    pid so its own pane is not recorded as running ``ccc snapshot``.
    """
    names = list(allowlist) if allowlist is not None else list(DEFAULT_RESTORE_COMMANDS)
    by_uuid, by_tty = _live_pane_index(live, store_rows, ps_rows)
    excluded = own_subtree(ps_rows, own_pid)

    def classify(raw: dict[str, Any]) -> SnapPane:
        return _classify_pane(raw, by_uuid, by_tty, ps_rows, args_of, cwds, excluded, names)

    windows: list[SnapWindow] = []
    for raw_window in raw_windows:
        frame_raw = raw_window.get("frame")
        frame: tuple[int, int, int, int] | None = None
        if isinstance(frame_raw, (list, tuple)) and len(frame_raw) == 4:
            frame = (
                int(frame_raw[0]),
                int(frame_raw[1]),
                int(frame_raw[2]),
                int(frame_raw[3]),
            )
        tabs = [
            SnapTab(_classify_tree(tab.get("tree") or {}, classify))
            for tab in raw_window.get("tabs") or []
        ]
        windows.append(
            SnapWindow(
                frame=frame,
                selected_tab=int(raw_window.get("selected_tab") or 0),
                tabs=tabs,
            )
        )
    stamp = created_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return windows_to_json(windows, stamp)


def raw_pane_uuids(raw_windows: Sequence[dict[str, Any]]) -> list[str]:
    """Every pane uuid in a RAW layout walk (used by the non-empty-iTerm guard)."""
    out: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        if "pane" in node:
            out.append(str((node.get("pane") or {}).get("uuid") or "").strip().upper())
            return
        for child in node.get("children") or []:
            walk(child)

    for window in raw_windows:
        for tab in window.get("tabs") or []:
            walk(tab.get("tree") or {})
    return out


def other_pane_count(raw_windows: Sequence[dict[str, Any]], own_uuid: str) -> int:
    """How many panes iTerm already has BESIDES the one running this command.

    macOS restores app windows itself after a reboot, so restoring into a non-empty iTerm
    silently duplicates the whole layout. Anything > 0 makes ``restore-snapshot`` refuse
    unless ``-y`` is given. An unknown *own_uuid* simply counts every pane.
    """
    wanted = (own_uuid or "").split(":")[-1].strip().upper()
    uuids = raw_pane_uuids(raw_windows)
    if wanted and wanted in uuids:
        uuids.remove(wanted)
    return len(uuids)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def snapshots_dir(create: bool = True) -> Path:
    """``app_home()/snapshots`` — created 0700 (chmod'd after mkdir, to beat umask)."""
    path = config.app_home() / "snapshots"
    if create:
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(DIR_MODE)
        except OSError:  # pragma: no cover - exotic filesystem
            pass
    return path


def snapshot_sort_key(path: Path) -> tuple[str, int, float]:
    """Chronological sort key for a snapshot file name (``<stamp>[-N].json``).

    The bare ``<stamp>.json`` is sequence 1 and its ``-2``/``-3`` collision siblings
    follow, which a plain name sort would get backwards (``-`` sorts before ``.``).
    """
    stem = path.stem
    parts = stem.split("-")
    if len(parts) > 2 and parts[-1].isdigit():
        base, seq = "-".join(parts[:-1]), int(parts[-1])
    else:
        base, seq = stem, 1
    try:
        mtime = path.stat().st_mtime
    except OSError:  # pragma: no cover - raced deletion
        mtime = 0.0
    return (base, seq, mtime)


def list_snapshots(directory: Path | None = None) -> list[Path]:
    """Every snapshot file, newest last."""
    folder = directory if directory is not None else snapshots_dir(create=False)
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"), key=snapshot_sort_key)


def latest_snapshot(directory: Path | None = None) -> Path | None:
    """The newest snapshot file, or ``None`` when none has been written yet."""
    files = list_snapshots(directory)
    return files[-1] if files else None


def snapshot_name(directory: Path, now: datetime | None = None) -> Path:
    """A free ``<YYYYmmdd-HHMMSS>[-N].json`` path in *directory* (collision-safe)."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}.json"
    seq = 2
    while path.exists():
        path = directory / f"{stamp}-{seq}.json"
        seq += 1
    return path


def write_snapshot(
    data: dict[str, Any], directory: Path | None = None, now: datetime | None = None
) -> Path:
    """Atomically write *data* as a new 0600 snapshot file; return its path."""
    folder = directory if directory is not None else snapshots_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = snapshot_name(folder, now)
    handle, tmp = tempfile.mkstemp(dir=str(folder), prefix=".snapshot-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def resolve_snapshot(name: str | None, directory: Path | None = None) -> Path | None:
    """Resolve a ``restore-snapshot`` argument to a file.

    Empty ⇒ the newest snapshot; a value containing ``/`` ⇒ that path as given; anything
    else ⇒ that name in the snapshots dir, with ``.json`` appended if needed.
    """
    folder = directory if directory is not None else snapshots_dir(create=False)
    text = (name or "").strip()
    if not text:
        return latest_snapshot(folder)
    if "/" in text:
        path = Path(os.path.expanduser(text))
        return path if path.is_file() else None
    for candidate in (folder / text, folder / f"{text}.json"):
        if candidate.is_file():
            return candidate
    return None


def load_snapshot(path: Path) -> dict[str, Any]:
    """Read + validate a snapshot document (raises ``ValueError`` on anything odd)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: unreadable snapshot ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
        raise ValueError(f"{path}: not a ccc snapshot (no 'windows' list)")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"{path}: unsupported schema_version {version!r} (expected 1)")
    return data


# --------------------------------------------------------------------------- #
# Restore planning (pure)
# --------------------------------------------------------------------------- #
@dataclass
class PaneAction:
    """What restore will do to one rebuilt pane.

    ``command`` is the single shell line typed into the fresh pane (``None`` = nothing
    to run); ``note`` explains a degrade to the user; ``skipped`` marks a pane that is
    deliberately left alone (its Claude session is already live) and ``error`` a pane
    that could NOT be restored (a blocked resume) — the exit code keys off ``error``.
    """

    command: str | None = None
    note: str = ""
    skipped: bool = False
    error: bool = False
    kind: str = ""
    title: str = ""
    cwd: str = ""


@dataclass
class PlanNode:
    """A splitter in the restore plan (mirrors :class:`SnapNode`)."""

    split: str
    children: list[PlanNode | PaneAction] = field(default_factory=list)


@dataclass
class TabPlan:
    """One tab of the restore plan."""

    tree: PlanNode | PaneAction


@dataclass
class WindowPlan:
    """One window of the restore plan."""

    frame: tuple[int, int, int, int] | None = None
    selected_tab: int = 0
    tabs: list[TabPlan] = field(default_factory=list)


Blockers = Callable[[str, str, str], list[str]]


def iter_actions(node: PlanNode | PaneAction) -> list[PaneAction]:
    """Every leaf action of *node*, in tree order."""
    if isinstance(node, PaneAction):
        return [node]
    out: list[PaneAction] = []
    for child in node.children:
        out.extend(iter_actions(child))
    return out


def plan_actions(plan: Sequence[WindowPlan]) -> list[PaneAction]:
    """Every leaf action of a whole plan, in window → tab → tree order."""
    return [a for win in plan for tab in win.tabs for a in iter_actions(tab.tree)]


def _action(
    pane: SnapPane,
    command: str | None,
    note: str = "",
    *,
    skipped: bool = False,
    error: bool = False,
) -> PaneAction:
    """A :class:`PaneAction` carrying *pane*'s presentation fields (kind/title/cwd)."""
    return PaneAction(
        command=command,
        note=note,
        skipped=skipped,
        error=error,
        kind=pane.kind,
        title=pane.title,
        cwd=pane.cwd,
    )


def _claude_action(pane: SnapPane, blockers: Blockers) -> PaneAction:
    """Plan a live-free Claude pane: refuse when blocked, else resume on its own seat."""
    from . import accounts  # pylint: disable=import-outside-toplevel

    reasons = blockers(pane.session_id, pane.cwd, pane.config_dir)
    if reasons:
        return _action(
            pane,
            f"cd {shlex.quote(pane.cwd)}",
            "cannot resume: " + "; ".join(reasons),
            error=True,
        )
    prefix = accounts.session_launch_env_prefix(pane)
    resume = f"cd {shlex.quote(pane.cwd)} && claude --resume {shlex.quote(pane.session_id)}"
    return _action(pane, prefix + resume)


def _pane_action(  # pylint: disable=too-many-return-statements
    pane: SnapPane, live_ids: set[str], blockers: Blockers, allowlist: Sequence[str]
) -> PaneAction:
    """Plan ONE pane (see :func:`build_plan` for the rule order)."""
    if pane.kind == "claude" and pane.session_id in live_ids:
        return _action(pane, None, "already running — skipped", skipped=True)
    if not pane.cwd:
        return _action(pane, None, "no cwd recorded")
    if not os.path.isdir(pane.cwd):
        # A degrade, not an error: the directory is simply gone (renamed repo, unmounted
        # volume), so there is nowhere to `cd` — leave the fresh pane where it lands.
        return _action(pane, None, f"cwd no longer exists: {pane.cwd}")
    if pane.kind == "claude":
        return _claude_action(pane, blockers)
    cd = f"cd {shlex.quote(pane.cwd)}"
    if pane.kind == "command" and pane.argv:
        # effective_name sees through a `#!`-script's interpreter (python3 …/bin/ccc ->
        # "ccc"); what gets re-run is still the FULL exact argv, quoted element by element.
        if effective_name(pane.argv) in set(allowlist):
            return _action(pane, cd + " && " + " ".join(shlex.quote(a) for a in pane.argv))
        was = pane.display or " ".join(pane.argv)
        printf = f"printf '%s\\n' {shlex.quote(RESTORE_NOTE_PREFIX + was)}"
        note = f"not on snapshot_restore_commands — printed a note instead: {was}"
        return _action(pane, f"{cd} && {printf}", note)
    if pane.display:  # a program we could not read argv for — keep the breadcrumb
        printf = f"printf '%s\\n' {shlex.quote(RESTORE_NOTE_PREFIX + pane.display)}"
        return _action(pane, f"{cd} && {printf}", f"was running: {pane.display}")
    return _action(pane, cd)


def build_plan(
    snapshot: dict[str, Any],
    live_ids: set[str],
    blockers: Blockers,
    allowlist: Sequence[str],
) -> list[WindowPlan]:
    """Turn a loaded snapshot into per-window/tab/pane actions. Pure except ``isdir``.

    Rule order per pane: a Claude session that is **already live** is skipped untouched;
    a pane whose recorded cwd is missing/gone degrades to "nothing to run" (a note, not
    an error); a Claude session with :func:`core.resume_blockers` reasons becomes an
    **error** pane that only ``cd``s; a clean Claude session is resumed on its own
    account; an allowlisted command is re-run from its exact argv; anything else prints
    a ``[ccc-restore] was running: …`` note into a shell at the cwd.
    """
    names = list(allowlist)

    def convert(node: SnapNode | SnapPane) -> PlanNode | PaneAction:
        if isinstance(node, SnapPane):
            return _pane_action(node, live_ids, blockers, names)
        return PlanNode(split=node.split, children=[convert(c) for c in node.children])

    plan: list[WindowPlan] = []
    for window in windows_from_json(snapshot):
        plan.append(
            WindowPlan(
                frame=window.frame,
                selected_tab=window.selected_tab,
                tabs=[TabPlan(convert(tab.tree)) for tab in window.tabs],
            )
        )
    return plan


# --------------------------------------------------------------------------- #
# iTerm2 adapters (thin)
# --------------------------------------------------------------------------- #
async def _read_pane(session: Any) -> dict[str, Any]:
    """RAW facts for one iTerm session (every variable read degrades to ``None``)."""

    async def var(name: str) -> Any:
        try:
            return await session.async_get_variable(name)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return None

    job_pid = await var("jobPid")
    try:
        job_pid = int(job_pid) if job_pid else None
    except (TypeError, ValueError):
        job_pid = None
    return {
        "uuid": getattr(session, "session_id", "") or "",
        "tty": normalize_tty(await var("tty")),
        "job_pid": job_pid,
        "job_name": await var("jobName") or "",
        "command_line": await var("commandLine") or "",
        "path": await var("path") or "",
        "name": await var("name") or "",
    }


async def _read_tree(node: Any) -> dict[str, Any]:
    """Walk an iTerm ``Splitter``/``Session`` tree into the RAW node form.

    ``tab.root`` is always a ``Splitter``; a one-child splitter is collapsed to its pane
    so a plain tab is a bare ``{"pane": …}`` node.
    """
    children = getattr(node, "children", None)
    if children is None:  # a leaf Session
        return {"pane": await _read_pane(node)}
    converted = [await _read_tree(child) for child in children]
    if len(converted) == 1:
        return converted[0]
    return {
        "split": "vertical" if getattr(node, "vertical", False) else "horizontal",
        "children": converted,
    }


async def _read_window(window: Any) -> dict[str, Any]:
    """RAW facts for one iTerm window: frame, selected-tab index and its tab trees."""
    frame: list[int] | None = None
    try:
        box = await window.async_get_frame()
        frame = [
            int(box.origin.x),
            int(box.origin.y),
            int(box.size.width),
            int(box.size.height),
        ]
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        frame = None
    tabs = list(getattr(window, "tabs", []) or [])
    current = getattr(window, "current_tab", None)
    selected = 0
    for index, tab in enumerate(tabs):
        if current is not None and getattr(tab, "tab_id", None) == getattr(current, "tab_id", None):
            selected = index
            break
    return {
        "frame": frame,
        "selected_tab": selected,
        "tabs": [{"tree": await _read_tree(tab.root)} for tab in tabs],
    }


async def collect_raw(connection: Any) -> list[dict[str, Any]]:
    """Walk every iTerm window/tab/pane over *connection* into the RAW layout form."""
    import iterm2  # pylint: disable=import-outside-toplevel

    app = await iterm2.async_get_app(connection)
    if app is None:
        return []
    return [await _read_window(window) for window in app.terminal_windows]


Splitter = Callable[[Any, bool], Awaitable[Any]]


async def layout_tree(
    node: PlanNode | PaneAction, session: Any, split: Splitter
) -> list[tuple[PaneAction, Any]]:
    """Rebuild *node*'s split topology under *session*; return its leaves in tree order.

    Child 0 keeps the session it was handed; every later child is a fresh pane obtained
    by splitting the **previous** one, which is what preserves left-to-right /
    top-to-bottom order. *split* is injected (``session, vertical → new session``) so the
    whole walk is unit-testable against fake sessions.
    """
    if isinstance(node, PaneAction):
        return [(node, session)]
    if not node.children:
        return []
    sessions = [session]
    for _ in node.children[1:]:
        sessions.append(await split(sessions[-1], node.split == "vertical"))
    out: list[tuple[PaneAction, Any]] = []
    for child, child_session in zip(node.children, sessions, strict=False):
        out.extend(await layout_tree(child, child_session, split))
    return out


async def _split_pane(session: Any, vertical: bool) -> Any:
    """The live splitter used by the executor."""
    return await session.async_split_pane(vertical=vertical)


def _sole_session(tab: Any) -> Any:
    """The one session of a tab the Python API has just created (``None`` if it has none).

    Never key a fresh tab off ``Tab.current_session`` (or a fresh window off
    ``Window.current_tab``): the ``active_session_id`` / ``selected_tab_id`` behind them
    are only filled in by a later layout notification, so on a just-created object both
    answer ``None`` — which once made the executor skip EVERY pane of a restore while
    the report still counted them as restored. A fresh tab has exactly one session and
    ``tab.sessions`` lists it; ``current_session`` is only the fallback for an odd tab.
    """
    sessions = list(getattr(tab, "sessions", None) or [])
    if len(sessions) == 1:
        return sessions[0]
    return getattr(tab, "current_session", None)


def _first_tab(window: Any) -> Any:
    """The tab a fresh window was created with (see :func:`_sole_session`)."""
    tabs = list(getattr(window, "tabs", None) or [])
    if tabs:
        return tabs[0]
    return getattr(window, "current_tab", None)


async def _build_window(
    connection: Any, window_plan: WindowPlan
) -> tuple[Any, list[tuple[PaneAction, Any]], list[str]]:
    """Phase 1 for one window: create it, size it, add its tabs and rebuild every split.

    Returns the window, its ``(action, session)`` leaves and the failures of tabs iTerm
    handed back without a session — one line per pane that consequently gets nothing,
    in the same wording :func:`_deliver` uses, so :func:`report_restore` counts them.
    """
    import iterm2  # pylint: disable=import-outside-toplevel

    window = await iterm2.Window.async_create(connection)
    if window is None:
        raise RuntimeError("iTerm2 refused to create a window")
    if window_plan.frame:
        try:
            box = iterm2.util.Frame(
                origin=iterm2.util.Point(window_plan.frame[0], window_plan.frame[1]),
                size=iterm2.util.Size(window_plan.frame[2], window_plan.frame[3]),
            )
            await window.async_set_frame(box)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass  # multi-monitor clamping is out of scope (documented v1 limitation)
    leaves: list[tuple[PaneAction, Any]] = []
    failures: list[str] = []
    for index, tab_plan in enumerate(window_plan.tabs):
        tab = _first_tab(window) if index == 0 else await window.async_create_tab()
        session = _sole_session(tab) if tab is not None else None
        if session is None:
            for action in iter_actions(tab_plan.tree):
                if not action.skipped:
                    failures.append(
                        f"{action.kind} pane in {action.cwd or '?'}: iTerm2 returned no "
                        f"session for tab {index + 1}"
                    )
            continue
        leaves.extend(await layout_tree(tab_plan.tree, session, _split_pane))
    return window, leaves, failures


# A fresh pane is "quiet" once its foreground job has been the idle shell for this many
# consecutive polls; past the timeout the command is typed anyway (the tty queues it).
_SHELL_NAMES = frozenset({"zsh", "bash", "fish", "sh", "dash", "ksh", "tcsh", "csh", "nu"})
_SHELL_POLL_SECONDS = 0.25
_SHELL_STABLE_POLLS = 3
_SHELL_WAIT_TIMEOUT = 20.0


def is_shell_job(job_name: str | None) -> bool:
    """True when iTerm's ``jobName`` names an interactive shell (``-zsh`` → ``zsh``)."""
    name = os.path.basename(str(job_name or "").strip()).lstrip("-")
    return name in _SHELL_NAMES


async def _await_shell(session: Any, timeout: float | None = None) -> bool:
    """Wait until *session*'s foreground job has settled on the idle shell.

    A just-created pane runs ``login`` → the shell's startup files (each child program
    shows up as ``jobName``) → the prompt. Text typed earlier is queued by the tty and
    normally survives all of that, but a startup program that flushes the tty input
    would eat it — so, like the AppleScript rung's ``repeat while is processing``, the
    executor types only into a quiet pane. Returns False when the timeout ran out (the
    caller types anyway rather than stranding the restore on one slow ``.zshrc``).
    """
    limit = _SHELL_WAIT_TIMEOUT if timeout is None else timeout
    stable = 0
    waited = 0.0
    while waited < limit:
        try:
            job = await session.async_get_variable("jobName")
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return False  # no variables at all: nothing to wait for, type anyway
        stable = stable + 1 if is_shell_job(job) else 0
        if stable >= _SHELL_STABLE_POLLS:
            return True
        await asyncio.sleep(_SHELL_POLL_SECONDS)
        waited += _SHELL_POLL_SECONDS
    return False


async def _deliver(leaves: Sequence[tuple[PaneAction, Any]]) -> list[str]:
    """Phase 2: type each pane's command, restore non-claude names; collect failures.

    Every pane that gets a command is first awaited to a quiet shell — concurrently, so
    a window of many tabs waits for its slowest startup once, not per tab.
    """
    failures: list[str] = []
    await asyncio.gather(*(_await_shell(session) for action, session in leaves if action.command))
    for action, session in leaves:
        if action.command:
            try:
                await session.async_send_text(action.command + "\n")
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                failures.append(f"{action.kind} pane in {action.cwd or '?'}: {exc}")
                continue
        if action.title and action.kind != "claude":
            try:
                await session.async_set_name(action.title)
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                pass  # a pane title is cosmetic — never fail a restore over it
    return failures


async def execute_plan(connection: Any, plan: Sequence[WindowPlan]) -> list[str]:
    """Two-phase restore: build EVERY empty window/tab/split, then deliver the commands.

    Building the whole layout first means a failure half-way leaves a usable (if bare)
    desk rather than a half-typed one, and no pane receives text before its neighbours
    exist. Returns the list of per-pane failure descriptions (empty ⇒ everything landed).
    """
    import iterm2  # pylint: disable=import-outside-toplevel

    # ``Window.async_create_tab`` asserts on ``Window.delegate``, which ONLY
    # ``async_get_app`` installs. The guard's ``collect_raw`` used to be the sole caller,
    # so ``-y`` (which skips the guard) failed every multi-tab window with a bare
    # AssertionError. Install it here, where the tabs are created.
    await iterm2.async_get_app(connection)
    failures: list[str] = []
    built: list[tuple[Any, int, list[tuple[PaneAction, Any]]]] = []
    for index, window_plan in enumerate(plan):
        try:
            window, leaves, lost = await _build_window(connection, window_plan)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            failures.append(f"window {index + 1}: {exc}")
            continue
        failures.extend(f"window {index + 1}: {text}" for text in lost)
        built.append((window, window_plan.selected_tab, leaves))
    for window, selected, leaves in built:
        failures.extend(await _deliver(leaves))
        tabs = list(getattr(window, "tabs", []) or [])
        if 0 <= selected < len(tabs):
            try:
                await tabs[selected].async_select()
            except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                pass  # cosmetic, like the pane names
    return failures


def _connect(coro_factory: Callable[[Any], Awaitable[Any]]) -> Any:
    """Run *coro_factory* against a fresh iTerm2 connection; raise ``RuntimeError`` if none.

    The single iTerm boundary for both commands, so the "Enable Python API" advice is
    worded once.
    """
    try:
        import iterm2  # pylint: disable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - iterm2 is a hard dependency
        raise RuntimeError(_API_HINT) from exc

    async def _go() -> Any:
        connection = await asyncio.wait_for(iterm2.Connection.async_create(), timeout=10)
        return await coro_factory(connection)

    try:
        return asyncio.run(_go())
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        raise RuntimeError(f"{_API_HINT} ({exc})") from exc


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def pane_summary(pane: SnapPane) -> str:
    """One-line description of a captured pane (``snapshot -n`` / ``--list``)."""
    if pane.kind == "claude":
        return f"claude   {pane.session_id[:8]}  {pane.cwd or '?'}"
    if pane.kind == "command":
        return f"command  {' '.join(pane.argv)}  [{pane.cwd or '?'}]"
    extra = f"  (was: {pane.display})" if pane.display else ""
    return f"shell    {pane.cwd or '?'}{extra}"


def describe_snapshot(data: dict[str, Any]) -> list[str]:
    """Render a whole snapshot document as indented ``window → tab → pane`` lines."""
    lines: list[str] = []
    for windex, window in enumerate(windows_from_json(data), start=1):
        frame = ",".join(str(v) for v in window.frame) if window.frame else "—"
        lines.append(f"window {windex}  frame [{frame}]  selected tab {window.selected_tab + 1}")
        for tindex, tab in enumerate(window.tabs, start=1):
            panes = iter_panes(tab.tree)
            lines.append(f"  tab {tindex}  ({len(panes)} pane{'s' if len(panes) != 1 else ''})")
            for pane in panes:
                lines.append(f"    {pane_summary(pane)}")
    return lines


def describe_plan(plan: Sequence[WindowPlan]) -> list[str]:
    """Render a restore plan as indented ``window → tab → pane`` lines."""
    lines: list[str] = []
    for windex, window in enumerate(plan, start=1):
        frame = ",".join(str(v) for v in window.frame) if window.frame else "—"
        lines.append(f"window {windex}  frame [{frame}]  selected tab {window.selected_tab + 1}")
        for tindex, tab in enumerate(window.tabs, start=1):
            actions = iter_actions(tab.tree)
            lines.append(f"  tab {tindex}  ({len(actions)} pane{'s' if len(actions) != 1 else ''})")
            for action in actions:
                mark = "!" if action.error else ("-" if action.skipped else "+")
                lines.append(f"    {mark} {action.command or '(nothing to run)'}")
                if action.note:
                    lines.append(f"      note: {action.note}")
    return lines


def snapshot_counts(data: dict[str, Any]) -> tuple[int, int, int, int]:
    """``(windows, tabs, panes, claude panes)`` of a snapshot document."""
    windows = windows_from_json(data)
    tabs = sum(len(w.tabs) for w in windows)
    panes = [p for w in windows for t in w.tabs for p in iter_panes(t.tree)]
    claude = sum(1 for p in panes if p.kind == "claude")
    return (len(windows), tabs, len(panes), claude)


def _file_age_label(path: Path) -> str:
    """Compact age of *path* (``12m``, ``3h``, ``5d``) via the shared humanizer."""
    from .models import humanize_age  # pylint: disable=import-outside-toplevel

    try:
        stamp = int(path.stat().st_mtime * 1000)
    except OSError:  # pragma: no cover - raced deletion
        return "—"
    return humanize_age(stamp)


def restore_commands(cfg: Any) -> list[str]:
    """The effective restore allowlist for *cfg* (falls back to the shipped default)."""
    values = list(getattr(cfg, "snapshot_restore_commands", None) or DEFAULT_RESTORE_COMMANDS)
    return [str(v).strip() for v in values if str(v).strip()]


# --------------------------------------------------------------------------- #
# CLI entry points
# --------------------------------------------------------------------------- #
def _capture() -> dict[str, Any]:
    """Do a full live capture (iTerm walk + ps + lsof + argv) into a snapshot document."""
    from .adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel
    from .store import Store  # pylint: disable=import-outside-toplevel

    raw_windows = _connect(collect_raw)
    live = [s for s in ClaudeAdapter().discover() if s.alive]
    with Store() as store:
        store_rows = {s.session_id: (s.iterm_session_id or "") for s in store.list_sessions(True)}
    ps_rows = read_ps()
    # One lsof for every pid, unless the machine is busy enough that the argv list would
    # get unwieldy — then only the processes attached to a terminal can matter anyway.
    if len(ps_rows) <= 400:
        pids = set(ps_rows)
    else:
        pids = {pid for pid, row in ps_rows.items() if row.tty}
    cwds = read_cwds(sorted(pids))
    return build_snapshot(
        raw_windows,
        live,
        store_rows,
        ps_rows,
        procargs,
        cwds,
        os.getpid(),
        allowlist=restore_commands(config.load_config()),
    )


def _list_snapshots() -> int:
    """``ccc snapshot --list`` — the saved snapshots, oldest first."""
    files = list_snapshots()
    if not files:
        print(f"no snapshots yet in {local_link(snapshots_dir(create=False))}")
        return 0
    for path in files:
        try:
            counts = snapshot_counts(load_snapshot(path))
        except ValueError as exc:
            print(f"{path.name:26} unreadable ({exc})")
            continue
        windows, tabs, panes, claude = counts
        print(
            f"{path.name:26} {_file_age_label(path):>5} ago  "
            f"{windows} window(s), {tabs} tab(s), {panes} pane(s), {claude} claude  "
            f"{local_link(path, path.name)}"
        )
    return 0


def run_snapshot(args: argparse.Namespace) -> int:
    """``ccc snapshot`` — capture the current iTerm layout (see the module docstring)."""
    if getattr(args, "list", False):
        return _list_snapshots()
    cfg = config.load_config()
    if cfg.launcher == "tmux":
        print(f"error: {_TMUX_HINT}", file=sys.stderr)
        return 1
    try:
        data = _capture()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    windows, tabs, panes, claude = snapshot_counts(data)
    if not windows:
        print("error: iTerm2 reports no windows — nothing to snapshot", file=sys.stderr)
        return 1
    if getattr(args, "dry_run", False):
        for line in describe_snapshot(data):
            print(line)
        print(f"dry run — {windows} window(s), {tabs} tab(s), {panes} pane(s), {claude} claude")
        return 0
    path = write_snapshot(data)
    print(local_link(path))
    print(f"saved {windows} window(s), {tabs} tab(s), {panes} pane(s), {claude} claude session(s)")
    return 0


def _load_for_restore(args: argparse.Namespace) -> tuple[Path, dict[str, Any]] | None:
    """Resolve + load the snapshot named by *args* (prints its own error and returns None)."""
    path = resolve_snapshot(getattr(args, "name", None))
    if path is None:
        wanted = getattr(args, "name", None) or "(latest)"
        print(
            f"error: no snapshot {wanted} — `ccc snapshot --list` shows what is saved",
            file=sys.stderr,
        )
        return None
    try:
        return path, load_snapshot(path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _print_restore_header(path: Path, data: dict[str, Any]) -> None:
    """Announce WHICH snapshot is being restored and — prominently — how old it is."""
    windows, tabs, panes, claude = snapshot_counts(data)
    print(
        f"restoring {path.name} — taken {_file_age_label(path)} ago "
        f"({data.get('created_at', '?')}): {windows} window(s), {tabs} tab(s), "
        f"{panes} pane(s), {claude} claude session(s)"
    )


def report_restore(actions: Sequence[PaneAction], failures: Sequence[str]) -> int:
    """Print the per-pane outcome and return the exit code (1 ⇒ something errored)."""
    for failure in failures:
        print(f"error: {failure}", file=sys.stderr)
    errored = [a for a in actions if a.error]
    for action in errored:
        print(f"error: {action.cwd or '?'}: {action.note}", file=sys.stderr)
    skipped = sum(1 for a in actions if a.skipped)
    ok = len(actions) - len(errored) - len(failures) - skipped
    print(
        f"restored {max(ok, 0)} pane(s), skipped {skipped}, failed {len(errored) + len(failures)}"
    )
    return 1 if (failures or errored) else 0


def run_restore(args: argparse.Namespace) -> int:
    """``ccc restore-snapshot`` — rebuild a saved layout (see the module docstring)."""
    from . import core  # pylint: disable=import-outside-toplevel
    from .adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel

    cfg = config.load_config()
    if cfg.launcher == "tmux":
        print(f"error: {_TMUX_HINT}", file=sys.stderr)
        return 1
    loaded = _load_for_restore(args)
    if loaded is None:
        return 1
    _print_restore_header(*loaded)
    live_ids = {s.session_id for s in ClaudeAdapter().discover() if s.alive}
    plan = build_plan(loaded[1], live_ids, core.resume_blockers, restore_commands(cfg))
    actions = plan_actions(plan)
    if getattr(args, "dry_run", False):
        for line in describe_plan(plan):
            print(line)
        return 1 if any(a.error for a in actions) else 0
    try:
        failures = _restore(plan, bool(getattr(args, "yes", False)))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if failures is None:  # the non-empty-iTerm guard refused (it printed the reason)
        return 1
    return report_restore(actions, failures)


def _restore(plan: Sequence[WindowPlan], force: bool) -> list[str] | None:
    """Guard the current iTerm, then execute *plan*. ``None`` ⇒ the guard refused."""
    own_uuid = os.environ.get("ITERM_SESSION_ID", "")

    async def _go(connection: Any) -> list[str] | None:
        if not force:
            existing = other_pane_count(await collect_raw(connection), own_uuid)
            if existing:
                print(
                    f"error: iTerm2 already has {existing} other pane(s) open. macOS restores "
                    "app windows itself after a reboot, so restoring now would DUPLICATE the "
                    "layout. Close them first, or re-run with -y/--yes to restore anyway.",
                    file=sys.stderr,
                )
                return None
        return await execute_plan(connection, plan)

    result = _connect(_go)
    return result if result is None else list(result)
