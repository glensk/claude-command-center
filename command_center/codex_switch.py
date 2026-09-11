#!/usr/bin/env python3
"""``/switch`` for the Codex TUI: move a live thread to another seat, same tab.

The Codex twin of ``ccc switch-account -N`` (``/cpriv-to-cwork``). Codex has no notion of
an account beyond ``$CODEX_HOME/auth.json``, so "switch the seat" means: quit this TUI,
port the thread's rollout into the target ``CODEX_HOME`` and relaunch
``codex … resume <thread>`` under that home in the very same iTerm tab / tmux pane. The
conversation continues; only the ChatGPT login that bills it changes.

Zero model tokens, by construction: the trigger is a ``UserPromptSubmit`` hook (a
``$CODEX_HOME/hooks/codex-switch-hook.py`` that pipes the payload into ``ccc
codex-switch`` — see docs/reference.md), which Codex runs CLIENT-SIDE before any request
and whose ``{"decision": "block"}`` answer cancels the prompt — so it works after "Usage
limit reached", exactly like the inline expansion of the Claude slash command does. The
composer rejects an unknown ``/switch`` outright ("Unrecognized command"), but a leading
space or a ``/`` inside the name bypasses that check
(``slash_input.rs::validate_submission``), so the accepted spellings are `` /switch``,
``//switch``, ``/switch/`` and the bare word ``switch``, each optionally followed by one
seat token (``<seat>!`` forces a seat ccc's oracle marks held/blocked).

Three commands, mirroring ``switch-account`` / ``switch-now``:

* ``ccc codex-switch`` (:func:`run_switch`) — the hook side. Reads the hook JSON on
  stdin, plans the relaunch (which seat, which codex process, its exact argv + launch
  env), writes a 0600 launch record, spawns the detached relauncher and prints the block
  decision whose ``reason`` says what happens next — or WHY nothing will (every refusal is
  a block too: a refused switch must never turn into a model turn).
* ``ccc codex-switch-now`` (:func:`run_switch_now`) — the detached relauncher. Waits for
  the hook process to be gone (Codex has consumed the block), types ``/quit`` (the TUI has
  no SIGTERM handler and would leave the terminal raw — so there is NO signal fallback:
  a TUI that does not quit is left alive and the user notified), waits for the process to
  exit and for the tab's tty to be back at a shell prompt, ports the rollout and types the
  trampoline line. Once the source has exited, every later failure relaunches the SOURCE
  seat instead, so the tab never ends dead.
* ``ccc codex-switch-exec <token>`` (:func:`run_switch_exec`) — the trampoline the tab
  runs. Loads the launch record and ``execve``s codex: no argv or environment value is
  ever typed into a shell (quoting, history, length — none of it applies).

Why the rollout is PORTED by prefix-merge rather than copied or hard-linked: ccc's seat
readers (``usage._codex_rollout_snapshot``, ``codex_in_claude.codex_refusal``) attribute
the newest ``rate_limits`` event found under ``<home>/sessions`` to THAT seat. A shared
inode would staple one seat's figures onto the other's card; a plain copy would carry
the source seat's "usage limit reached" block into the target home and mark a healthy
seat as refusing until its first turn; and nulling EVERY ``rate_limits`` on each hop
would, on the way back, erase the destination's own events from earlier visits. So: the
destination's existing file must be a prefix of the source after normalisation
(``rate_limits`` nulled on both sides), that prefix is kept byte-for-byte from the
destination (its own telemetry intact — and Codex's SQLite history projection, which
continues from a byte offset, stays aligned), and only the new tail is scrubbed. Anything
else is divergence: the destination is set aside and the switch refused.

Why argv and env come from ``sysctl KERN_PROCARGS2`` (``snapshot.procargs_full``) and
not ``ps``: a tp-launched review carries a multi-kilobyte multi-line prompt as ONE
positional argument, and ``ps`` joins argv with spaces — unsplittable. The relaunch keeps
every allow-listed TUI option verbatim, before AND after a ``resume`` (Codex 0.153.4
accepts the whole TUI option set there, with highest precedence); an option this module
does not know REFUSES the switch rather than guessing whether it takes a value. Dropped:
the positional prompt / session id, ``-i`` (startup input) and ``resume``'s picker flags.
Refused outright: ``--remote`` (a remote app-server keeps its own login — a local
``CODEX_HOME`` change means nothing there) and a ``-p/--profile`` whose
``<home>/<name>.config.toml`` the target seat lacks or spells differently. The carried
environment is the families a launcher pins (``AI_*``, ``TP_*`` …) minus Codex's own
storage-routing variables and anything secret-shaped — values live only in the record.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import fcntl
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config

# The accepted spellings (see the module docstring for why the composer needs them):
# " /switch", "//switch", "/switch/", "switch" — plus ONE optional seat token.
SWITCH_RE = re.compile(r"^\s*/{0,2}switch/?(?:\s+(?P<target>[^\s/]+))?\s*$", re.IGNORECASE)

EVENT = "codex-switch"  # events.log tag shared by all three commands

# How long the relauncher waits at each step. `/quit` is graceful and quick; the READY
# wait is long because a launcher wrapping the TUI (tp's `open -H` child runner) has its
# own bookkeeping to finish before the shell prompt comes back.
HOOK_EXIT_WAIT_SEC = 10.0
QUIT_WAIT_SEC = 15.0
READY_WAIT_SEC = 45.0
READY_SETTLE_SEC = 1.0
SLASH_ENTER_DELAY_SEC = 0.4  # > the composer's 120 ms paste-Return suppression window
POLL_SEC = 0.25
# A second `/switch` while one is in flight must not dispatch a second relauncher.
PENDING_TTL_SEC = 120
RECORD_TTL_SEC = 86_400

# The TUI options of `codex` (0.153.4 `codex --help`) — the COMPLETE allow-list, valid
# before and after `resume`. A value option keeps its one value (separate, `--opt=v` or
# the attached short form `-mgpt-5`), a flag is kept as is, `-i/--image` (startup input)
# and every positional are dropped, `--remote*` refuses, and any other `-…` REFUSES the
# switch: this module never guesses whether an unknown option consumes the next word.
_VALUE_OPTIONS = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
    }
)
_SHORT_VALUE_OPTIONS = frozenset({"-c", "-m", "-p", "-s", "-C", "-a", "-i"})
_FLAGS = frozenset(
    {
        "--strict-config",
        "--oss",
        "--approve-for-me",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--search",
        "--no-alt-screen",
    }
)
_DROPPED_VALUE_OPTIONS = frozenset({"-i", "--image"})
_REMOTE_OPTIONS = frozenset({"--remote", "--remote-auth-token-env"})
_RESUME_FLAGS = frozenset({"--last", "--all", "--include-non-interactive"})
_NON_TUI_SUBCOMMANDS = frozenset(
    {
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "mcp-server",
        "app-server",
        "app",
        "completion",
        "agents",
        "plugin",
        "remote-control",
        "archive",
        "queue",
    }
)
PROFILE_SUFFIX = ".config.toml"  # `<CODEX_HOME>/<profile>.config.toml` (config/mod.rs)

# Environment families a LAUNCHER (tp, ccc, a wrapper function) pins on the command line.
# Only these are carried: the relaunch lands in the same interactive shell, so everything
# the shell itself exports is inherited again — and `KERN_PROCARGS2` of the shell shows
# its environment at exec time, BEFORE .zshrc ran, so a plain diff would re-carry every
# .zshrc export (measured: 30+ variables on this machine).
_ENV_CARRY_PREFIXES = ("AI_", "TP_", "CCC_", "CLAUDE_SESSION_", "CODEX_")
# Codex's own storage/identity routing (names verified in the 0.153.4 binary): carrying
# any of them would point the target seat at the source seat's files.
_ENV_NEVER = frozenset(
    {
        "CODEX_HOME",
        "CODEX_SQLITE_HOME",
        "CODEX_MANAGED_PACKAGE_ROOT",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CODEX_NON_INTERACTIVE",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CCC_INTERNAL",
    }
)
_SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|_PAT$|_URL$", re.I)
_ENV_CARRY_MAX = 64
_ENV_VALUE_MAX = 2048
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_SHELL_NAMES = frozenset({"zsh", "bash", "sh", "dash", "ksh", "fish"})
_TOKEN_RE = re.compile(r"[0-9a-f]{12,32}")
_THREAD_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,}")


class SwitchError(RuntimeError):
    """A refused switch — the message is what the user reads in the block reason."""


@dataclass(frozen=True)
class HookInput:
    """The fields of Codex's ``UserPromptSubmit`` payload this module uses."""

    session_id: str
    transcript_path: str
    cwd: str
    prompt: str


@dataclass(frozen=True)
class Seat:
    """One configured Codex login: its ccc label and ``CODEX_HOME``."""

    label: str
    home: Path


@dataclass(frozen=True)
class LaunchRecord:
    """What ``codex-switch-exec`` execs — written 0600 by the hook side, read by the tab.

    ``options`` are the allow-listed TUI options of the ORIGINAL launch (verbatim),
    ``env`` the carried launcher pins; ``resume <thread_id>`` and ``CODEX_HOME`` are
    appended at exec time from the other fields, so the record can relaunch either seat.
    """

    token: str
    thread_id: str
    exe: str
    options: list[str]
    env: dict[str, str]
    source_home: str
    target_home: str
    source_label: str
    target_label: str
    transcript: str  # rollout path relative to the source home
    created: int


@dataclass(frozen=True)
class Plan:
    """Everything the relauncher needs, decided on the hook side."""

    record: LaunchRecord
    source: Seat
    target: Seat
    codex_pid: int
    target_note: str = ""  # e.g. "forced past ccc's hold" for an explicit `<seat>!`
    env_names: tuple[str, ...] = field(default_factory=tuple)  # names only — never values


# --------------------------------------------------------------------------- #
# Hook input + trigger
# --------------------------------------------------------------------------- #
def parse_hook_input(raw: str) -> HookInput:
    """The ``UserPromptSubmit`` JSON → :class:`HookInput` (``ValueError`` when not one)."""
    try:
        data = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        raise ValueError(f"hook input is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    event = str(data.get("hook_event_name") or "")
    if event and event != "UserPromptSubmit":
        raise ValueError(f"not a UserPromptSubmit payload (hook_event_name={event!r})")
    return HookInput(
        session_id=str(data.get("session_id") or ""),
        transcript_path=str(data.get("transcript_path") or ""),
        cwd=str(data.get("cwd") or ""),
        prompt=str(data.get("prompt") or ""),
    )


def switch_target(prompt: str) -> tuple[bool, str, bool]:
    """``(is_a_switch, seat_token, force)`` for a submitted *prompt* (token ``""`` = next).

    ``force`` is the trailing ``!`` on the seat token: the deliberate override for a seat
    ccc's oracle marks held/blocked (a bare token is refused with the oracle's verdict).
    """
    match = SWITCH_RE.match(prompt or "")
    if match is None:
        return False, "", False
    token = (match.group("target") or "").strip()
    force = token.endswith("!")
    return True, token.rstrip("!"), force


# --------------------------------------------------------------------------- #
# Seats
# --------------------------------------------------------------------------- #
def seats() -> list[Seat]:
    """Every configured seat in canonical order (``default`` → ``private`` → extras).

    ``config.codex_home()`` honours ``$CODEX_HOME`` — the hook inherits the codex
    process's environment, where a tp launch pins it — so the variable is dropped from
    this process first: the registry must describe the MACHINE, not the caller.
    """
    os.environ.pop("CODEX_HOME", None)
    return [Seat(label, Path(home)) for label, home in config.codex_homes().items()]


def same_home(one: Path, other: Path) -> bool:
    """Symlink-safe identity of two ``CODEX_HOME`` paths."""
    try:
        return one.expanduser().resolve() == other.expanduser().resolve()
    except OSError:  # pragma: no cover - resolve() fails only on exotic filesystems
        return str(one) == str(other)


def seat_of_home(home: Path, all_seats: list[Seat]) -> Seat | None:
    """The configured seat whose home is *home*, or ``None`` (an unregistered home)."""
    return next((seat for seat in all_seats if same_home(seat.home, home)), None)


def seat_aliases(entries: list[str] | None = None) -> dict[str, str]:
    """``alias → label`` from the ``codex_seat_aliases`` config key (``["work=default"]``).

    Same tolerance as :func:`config.parse_codex_homes_extra`: an entry without ``=``, a
    blank side or a repeated alias is skipped, never fatal. Aliases are matched
    case-insensitively, so they are stored lower-cased.
    """
    raw_entries = config.load_config().codex_seat_aliases if entries is None else entries
    aliases: dict[str, str] = {}
    for entry in raw_entries:
        alias, sep, label = str(entry).partition("=")
        alias, label = alias.strip().lower(), label.strip()
        if not sep or not alias or not label or alias in aliases:
            continue
        aliases[alias] = label
    return aliases


def resolve_seat(token: str, all_seats: list[Seat], aliases: dict[str, str]) -> Seat:
    """The seat *token* names: a label, a configured alias, or the login e-mail."""
    wanted = (token or "").strip()
    if not wanted:
        raise SwitchError("no seat named")
    by_label = {seat.label: seat for seat in all_seats}
    label = aliases.get(wanted.lower(), wanted)
    if label in by_label:
        return by_label[label]
    if "@" in wanted:
        for seat in all_seats:
            if (seat_email(seat) or "").lower() == wanted.lower():
                return seat
    known = ", ".join(sorted(by_label)) or "none configured"
    extra = f"; aliases: {', '.join(sorted(aliases))}" if aliases else ""
    raise SwitchError(f"unknown seat {wanted!r} (seats: {known}{extra})")


def seat_email(seat: Seat) -> str:
    """The login e-mail behind *seat* (``""`` when unreadable) — display only."""
    try:
        from .usage import codex_account_email  # pylint: disable=import-outside-toplevel

        return codex_account_email(seat.home) or ""
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return ""


def ranked_labels() -> tuple[list[str], str]:
    """Seat labels ccc's oracle would try now, best first, plus a reason when it says none.

    :func:`codex_in_claude.codex_homes_in_order` with ``probe=False``: an interactive
    thread may write, and a read-only probe seat must never be promoted by it (plan D5).
    Cooldowns, recorded refusals, 100 % windows, pins and the fill policy are all its
    business — this module only removes the current seat. A failing oracle yields an
    empty list; the caller then refuses the automatic pick.
    """
    os.environ.pop("CODEX_HOME", None)
    try:
        from .codex_in_claude import codex_homes_in_order  # pylint: disable=import-outside-toplevel

        ranked = codex_homes_in_order(probe=False)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return [], f"seat oracle unavailable: {exc}"
    return [candidate.label for candidate in ranked], ""


def next_seat(current: Seat, all_seats: list[Seat], ranked: list[str]) -> Seat:
    """The oracle's best seat that is not *current*."""
    by_label = {seat.label: seat for seat in all_seats}
    for label in ranked:
        if label != current.label and label in by_label:
            return by_label[label]
    others = ", ".join(seat.label for seat in all_seats if seat.label != current.label)
    raise SwitchError(
        f"no other seat has headroom right now ({others or 'none configured'} — all held/"
        "blocked per `codex-in-claude order`); name one deliberately: /switch <seat>!"
    )


# --------------------------------------------------------------------------- #
# The codex process: pid, argv, environment
# --------------------------------------------------------------------------- #
def _program_name(command: str) -> str:
    words = command.split()
    return words[0].rsplit("/", 1)[-1].lstrip("-") if words else ""


def find_codex_pid(start_pid: int, table: dict[int, Any]) -> int:
    """The OUTERMOST ancestor of *start_pid* (itself included) whose program is ``codex``.

    Exact program name, so ``codex-code-mode-host`` or a launcher whose argv merely
    mentions codex never matches; a codex whose parent is also codex is an in-process
    helper, and the walk continues to the TUI that owns the terminal.
    """
    seen: set[int] = set()
    current, found = start_pid, 0
    while current > 0 and current not in seen:
        seen.add(current)
        row = table.get(current)
        if row is None:
            break
        if _program_name(row.command) == "codex":
            found = current
        current = row.ppid
    return found


def shell_pid_of(pid: int, table: dict[int, Any]) -> int:
    """The nearest ancestor shell of *pid* (the tab's shell), or 0."""
    seen: set[int] = set()
    current = table[pid].ppid if pid in table else 0
    while current > 0 and current not in seen:
        seen.add(current)
        row = table.get(current)
        if row is None:
            return 0
        if _program_name(row.command) in _SHELL_NAMES:
            return current
        current = row.ppid
    return 0


def procargs(pid: int) -> tuple[str, list[str], dict[str, str]]:
    """``(executable, argv, environ)`` of *pid* — ``OSError`` when it cannot be read."""
    from . import snapshot  # pylint: disable=import-outside-toplevel

    full = snapshot.procargs_full(pid)
    if full is None:
        raise OSError(f"KERN_PROCARGS2 unavailable for pid {pid}")
    return full


def relaunch_options(argv: list[str]) -> list[str]:  # pylint: disable=too-many-branches
    """The allow-listed TUI options of a codex *argv*; prompt / session id / resume dropped.

    ``codex [OPTIONS] [PROMPT]`` or ``codex [OPTIONS] resume [OPTIONS] [ID]`` — the same
    option set is valid in both regions (0.153.4 ``SessionTuiCli``), so both are kept in
    order. Unknown options and ``--remote*`` refuse; a non-TUI subcommand refuses.
    """
    kept: list[str] = []
    args = list(argv[1:])
    i, in_resume = 0, False
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break  # everything after is positional (the prompt)
        if arg.startswith("-") and arg != "-":
            name, eq, attached = arg, "", ""
            if not arg.startswith("--") and len(arg) > 2 and arg[:2] in _SHORT_VALUE_OPTIONS:
                name, attached = arg[:2], arg[2:]  # `-mgpt-5.6-sol`, `-cfoo=bar`
            else:
                name, eq, _value = arg.partition("=")
            if name in _REMOTE_OPTIONS:
                raise SwitchError(
                    "this thread runs against a remote app-server (--remote) — a local "
                    "CODEX_HOME change would not move its login; nothing to switch"
                )
            if name in _VALUE_OPTIONS:
                if eq or attached:
                    kept.append(arg)
                    i += 1
                elif i + 1 < len(args):
                    kept.extend([arg, args[i + 1]])
                    i += 2
                else:
                    raise SwitchError(f"codex option {arg} has no value")
                continue
            if name in _DROPPED_VALUE_OPTIONS:
                i += 1 if (eq or attached) else 2
                continue
            if name in _FLAGS:
                kept.append(arg)
                i += 1
                continue
            if in_resume and name in _RESUME_FLAGS:
                i += 1
                continue
            raise SwitchError(
                f"unknown codex option {name} — extend codex_switch._FLAGS / _VALUE_OPTIONS "
                "after checking `codex --help`"
            )
        if not in_resume and arg == "resume":
            in_resume = True
            i += 1
            continue
        if not in_resume and arg in _NON_TUI_SUBCOMMANDS:
            raise SwitchError(f"`codex {arg}` is not an interactive TUI — nothing to switch")
        i += 1  # a positional: the prompt (root) or the session id / name (resume)
    return kept


def option_value(options: list[str], names: frozenset[str]) -> str:
    """The LAST value given for any option in *names* (``""`` when absent)."""
    found = ""
    for index, arg in enumerate(options):
        name, eq, value = arg.partition("=")
        if not eq and len(name) > 2 and name[:2] in names:
            found = name[2:]
        elif name in names:
            found = value if eq else (options[index + 1] if index + 1 < len(options) else "")
    return found


def env_carry(codex_env: dict[str, str], shell_env: dict[str, str]) -> dict[str, str]:
    """The launch-specific environment worth carrying: a launcher family, not the shell's.

    A variable travels when its name is in :data:`_ENV_CARRY_PREFIXES` (what tp / ccc /
    a wrapper pin for one launch: ``AI_PUSH_LOCAL_ONLY=1``, ``TP_*``, ``CODEX_*``), is not
    one of Codex's storage-routing names (:data:`_ENV_NEVER`), is not secret-shaped, has
    no control characters, and the tab's shell did not already start with that value.
    The result lives in the 0600 launch record only — never in argv, logs or reasons.
    """
    carry: dict[str, str] = {}
    for key in sorted(codex_env):
        value = codex_env[key]
        if not key.startswith(_ENV_CARRY_PREFIXES) or key in _ENV_NEVER:
            continue
        if _SECRET_NAME_RE.search(key) or not _ENV_NAME_RE.fullmatch(key):
            continue
        if shell_env.get(key) == value:
            continue
        if len(value) > _ENV_VALUE_MAX or has_control_chars(value):
            continue
        carry[key] = value
        if len(carry) >= _ENV_CARRY_MAX:
            break
    return carry


def has_control_chars(text: str) -> bool:
    """A newline (or any control char) in a typed line would submit it early."""
    return any(ord(ch) < 32 or ch == "\x7f" for ch in text)


# --------------------------------------------------------------------------- #
# Launch records + pending markers (app_home/codex-switch/)
# --------------------------------------------------------------------------- #
def record_dir() -> Path:
    """Where launch records and pending markers live (created 0700 on demand)."""
    path = config.app_home() / "codex-switch"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_record(record: LaunchRecord) -> Path:
    """Persist *record* as ``<token>.json`` (0600); prune records older than a day."""
    directory = record_dir()
    path = directory / f"{record.token}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(asdict(record), sort_keys=True).encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    cutoff = time.time() - RECORD_TTL_SEC
    for stale in directory.glob("*.json"):
        try:
            if stale != path and stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue
    return path


def read_record(token: str) -> LaunchRecord:
    """The launch record *token* names (``SwitchError`` when missing or malformed)."""
    if not _TOKEN_RE.fullmatch(token or ""):
        raise SwitchError(f"malformed launch token {token!r}")
    path = record_dir() / f"{token}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LaunchRecord(
            token=str(data["token"]),
            thread_id=str(data["thread_id"]),
            exe=str(data["exe"]),
            options=[str(opt) for opt in data["options"]],
            env={str(k): str(v) for k, v in dict(data["env"]).items()},
            source_home=str(data["source_home"]),
            target_home=str(data["target_home"]),
            source_label=str(data["source_label"]),
            target_label=str(data["target_label"]),
            transcript=str(data["transcript"]),
            created=int(data["created"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SwitchError(f"no usable launch record for token {token}: {exc}") from exc


def pending_path(thread_id: str) -> Path:
    return record_dir() / f"{thread_id}.pending"


def pending_token(thread_id: str) -> str:
    """The token of a switch still in flight for *thread_id* (``""`` when none / expired)."""
    path = pending_path(thread_id)
    try:
        if time.time() - path.stat().st_mtime > PENDING_TTL_SEC:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mark_pending(thread_id: str, token: str) -> None:
    pending_path(thread_id).write_text(token, encoding="utf-8")


def clear_pending(thread_id: str) -> None:
    try:
        pending_path(thread_id).unlink()
    except OSError:
        pass


def exec_line(token: str, *, source: bool = False) -> str:
    """The trampoline line typed into the tab (also the manual command)."""
    return f"ccc codex-switch-exec {token}" + (" -S" if source else "")


# --------------------------------------------------------------------------- #
# Rollout: validation, locks, port
# --------------------------------------------------------------------------- #
def home_of_transcript(transcript_path: str) -> Path | None:
    """The ``CODEX_HOME`` a rollout path lives in, or ``None`` when it is not one.

    Rollouts sit at ``<home>/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``; the home
    is four directories up from the file. Anything else is refused rather than guessed.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path)
    parents = list(path.parents)
    if not path.is_absolute() or len(parents) < 5 or parents[3].name != "sessions":
        return None
    return parents[4]


def rollout_thread_id(path: Path) -> str:
    """The thread id recorded in the rollout's first (``session_meta``) line, or ``""``."""
    try:
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
        obj = json.loads(first)
        if obj.get("type") != "session_meta":
            return ""
        return str((obj.get("payload") or {}).get("id") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def writer_lock_held(home: Path, thread_id: str) -> bool:
    """True when a live Codex holds *thread_id*'s writer lock under *home*.

    Codex 0.153 takes ``flock(LOCK_EX)`` on ``<home>/thread-writer-locks/<uuid>.lock``
    (``writer_lock.rs``: std ``File::try_lock``); a stale FILE is harmless, a held lock
    is a live writer. Probed non-blockingly and released at once.
    """
    path = home / "thread-writer-locks" / f"{thread_id}.lock"
    if not path.is_file():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def sqlite_rollout_path(home: Path, thread_id: str) -> str | None:
    """The rollout path *home*'s state DB records for *thread_id* (``None`` = no row/DB).

    Read-only, best-effort: Codex's resolver treats an existing SQLite path as
    authoritative, so a row pointing anywhere but the mirrored path would make the
    relaunch resume a different file. Nothing here ever writes to that database.
    """
    candidates = sorted(home.glob("state_*.sqlite"), reverse=True)
    if not candidates:
        return None
    try:
        conn = sqlite3.connect(f"file:{candidates[0]}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "select rollout_path from threads where id = ?", (thread_id,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def profile_mismatch(options: list[str], source_home: Path, target_home: Path) -> str:
    """ "" when the launch's ``-p/--profile`` resolves identically in both homes, else why.

    Codex resolves a v2 profile as ``<CODEX_HOME>/<name>.config.toml`` — per home, not
    through the shared base ``config.toml`` — so a launch valid on the source can be
    invalid on the target. Same file (symlink) or identical bytes passes.
    """
    name = option_value(options, frozenset({"-p", "--profile"}))
    if not name:
        return ""
    src, dst = source_home / f"{name}{PROFILE_SUFFIX}", target_home / f"{name}{PROFILE_SUFFIX}"
    if not dst.is_file():
        return f"profile {name!r} has no {dst} on the target seat"
    try:
        if src.is_file() and (src.samefile(dst) or src.read_bytes() == dst.read_bytes()):
            return ""
    except OSError:
        pass
    return f"profile {name!r} differs between {src} and {dst}"


def normalize_rollout_line(line: str) -> str:
    """*line* with a ``rate_limits`` payload nulled; any other line comes back as-is.

    Only lines that carry the key are parsed at all, and a line that fails to parse is
    left untouched — the conversation must survive a scrub that cannot understand it.
    Deterministic, so the same original line normalises identically on both seats.
    """
    if '"rate_limits"' not in line:
        return line
    stripped = line.rstrip("\r\n")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return line
    if not isinstance(obj, dict):
        return line
    changed = False
    if isinstance(obj.get("rate_limits"), dict):
        obj["rate_limits"] = None
        changed = True
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("rate_limits"), dict):
        payload["rate_limits"] = None
        changed = True
    if not changed:
        return line
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def merge_rollout(source: list[str], destination: list[str]) -> list[str]:
    """Prefix-merge: the destination's own bytes for the shared prefix, the tail scrubbed.

    Raises :class:`SwitchError` on divergence — a destination longer than the source or
    differing inside the prefix (after normalisation) — because overwriting it would
    delete a turn only the destination has.
    """
    if len(destination) > len(source):
        raise SwitchError(
            f"the target copy has {len(destination) - len(source)} more line(s) than the "
            "source — it was resumed there since the last switch; diverged"
        )
    merged: list[str] = []
    for index, line in enumerate(source):
        if index < len(destination):
            existing = destination[index]
            if line == existing or normalize_rollout_line(line) == normalize_rollout_line(existing):
                merged.append(existing)
                continue
            raise SwitchError(
                f"the target copy differs from the source at line {index + 1}; diverged"
            )
        merged.append(normalize_rollout_line(line))
    return merged


def port_rollout(record: LaunchRecord) -> Path:
    """Port the thread's rollout from the source home to the mirrored path in the target.

    Atomic (temp file + rename), mode preserved. A diverged destination is set aside as
    ``<name>.switch-diverged-<ts>`` and the switch refused. The thread's
    ``session_index.jsonl`` name lines ride along so the target's picker shows the title.
    """
    source_home, target_home = Path(record.source_home), Path(record.target_home)
    src = source_home / record.transcript
    dst = target_home / record.transcript
    if rollout_thread_id(src) != record.thread_id:
        raise SwitchError(f"{src} is not the rollout of thread {record.thread_id}")
    if writer_lock_held(target_home, record.thread_id):
        raise SwitchError(f"a live Codex holds this thread open under {target_home} — refusing")
    known = sqlite_rollout_path(target_home, record.thread_id)
    if known and Path(known) != dst:
        raise SwitchError(
            f"seat {record.target_label!r} knows this thread under a different rollout "
            f"({known}) — resume it there by hand"
        )
    try:
        with src.open(encoding="utf-8") as fh:
            source_lines = fh.readlines()
        destination_lines: list[str] = []
        if dst.is_file():
            with dst.open(encoding="utf-8") as fh:
                destination_lines = fh.readlines()
    except OSError as exc:
        raise SwitchError(f"could not read the rollouts: {exc}") from exc
    try:
        merged = merge_rollout(source_lines, destination_lines)
    except SwitchError as exc:
        aside = dst.with_name(f"{dst.name}.switch-diverged-{int(time.time())}")
        try:
            os.replace(dst, aside)
        except OSError:
            aside = dst
        raise SwitchError(f"{exc}; the target copy was set aside as {aside.name}") from exc
    tmp = dst.with_name(dst.name + ".switch-tmp")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fout:
            fout.writelines(merged)
        os.chmod(tmp, src.stat().st_mode & 0o777)
        if writer_lock_held(target_home, record.thread_id):  # re-check right before publishing
            raise SwitchError(f"a live Codex took this thread under {target_home} — refusing")
        os.replace(tmp, dst)
    except OSError as exc:
        raise SwitchError(f"could not port the rollout to {dst}: {exc}") from exc
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    _port_index_lines(record.thread_id, source_home, target_home)
    return dst


def _port_index_lines(thread_id: str, source_home: Path, target_home: Path) -> None:
    """Best-effort: append the thread's name lines to the target's ``session_index.jsonl``."""
    src = source_home / "session_index.jsonl"
    dst = target_home / "session_index.jsonl"
    try:
        lines = [
            line
            for line in src.read_text(encoding="utf-8").splitlines()
            if f'"{thread_id}"' in line
        ]
        if not lines:
            return
        existing = dst.read_text(encoding="utf-8").splitlines() if dst.is_file() else []
        new = [line for line in lines if line not in existing]
        if new:
            with dst.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(new) + "\n")
    except OSError:
        return


# --------------------------------------------------------------------------- #
# Planning (hook side)
# --------------------------------------------------------------------------- #
def plan_switch(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    hook: HookInput,
    target_token: str,
    *,
    force: bool = False,
    start_pid: int | None = None,
    table: dict[int, Any] | None = None,
) -> Plan:
    """Decide the whole relaunch from the hook payload; raise :class:`SwitchError` to refuse."""
    from . import terminal  # pylint: disable=import-outside-toplevel

    thread_id = hook.session_id.strip()
    if not _THREAD_RE.fullmatch(thread_id):
        raise SwitchError(f"hook payload names no thread id (session_id={thread_id!r})")
    pending = pending_token(thread_id)
    if pending:
        raise SwitchError(f"a switch of this thread is already in flight ({exec_line(pending)})")
    # (1) The rollout: the hook's transcript_path is authoritative — it must be a real
    # file inside a registered seat, and its session_meta must name this very thread.
    transcript = Path(hook.transcript_path) if hook.transcript_path else None
    source_home = home_of_transcript(hook.transcript_path)
    if transcript is None or source_home is None or not transcript.is_file():
        raise SwitchError(f"hook payload has no usable transcript_path ({hook.transcript_path!r})")
    all_seats = seats()
    source = seat_of_home(source_home, all_seats)
    if source is None:
        raise SwitchError(
            f"{source_home} is not a registered seat (codex_home_private / codex_homes_extra)"
        )
    if rollout_thread_id(transcript) != thread_id:
        raise SwitchError(f"{transcript.name} does not carry session_meta for thread {thread_id}")
    # (2) The process: the outermost codex above this hook, in the foreground of its tty,
    # whose own CODEX_HOME agrees with where the transcript lives.
    ps = table if table is not None else terminal.ps_table()
    codex_pid = find_codex_pid(os.getpid() if start_pid is None else start_pid, ps)
    if not codex_pid:
        raise SwitchError("could not find the codex process above this hook")
    row = ps[codex_pid]
    if "+" not in row.stat:
        raise SwitchError(f"codex pid {codex_pid} is not the foreground process of its terminal")
    try:
        exe, argv, codex_env = procargs(codex_pid)
    except OSError as exc:
        raise SwitchError(f"could not read argv/env of codex pid {codex_pid}: {exc}") from exc
    process_home = Path(codex_env.get("CODEX_HOME") or "~/.codex").expanduser()
    if not same_home(process_home, source.home):
        raise SwitchError(
            f"codex pid {codex_pid} runs with CODEX_HOME={process_home} but the transcript "
            f"lives under {source.home} — refusing to guess which seat bills it"
        )
    options = relaunch_options(argv)
    if "-C" not in options and "--cd" not in options and hook.cwd:
        options = ["-C", hook.cwd, *options]  # the shell's cwd is not the thread's
    # (3) The target: explicit (label / alias / e-mail, `!` forces past the oracle) or the
    # oracle's best seat that is not the current one.
    ranked, why = ranked_labels()
    note = ""
    if target_token:
        target = resolve_seat(target_token, all_seats, seat_aliases())
        if target.label not in ranked:
            if not force:
                raise SwitchError(
                    f"seat {target.label!r} is not available per ccc's seat oracle"
                    + (f" ({why})" if why else " (see `codex-in-claude order`)")
                    + f" — `/switch {target_token}!` forces it"
                )
            note = f"forced past ccc's hold on {target.label!r}"
    else:
        if not ranked and why:
            raise SwitchError(f"{why} — name the seat explicitly: /switch <seat>!")
        target = next_seat(source, all_seats, ranked)
    if same_home(target.home, source.home):
        raise SwitchError(f"this thread already runs on seat {target.label!r} — nothing to do")
    if not (target.home / "auth.json").is_file():
        raise SwitchError(
            f"seat {target.label!r} ({target.home}) is not logged in — "
            f"run `CODEX_HOME={target.home} codex login` first"
        )
    mismatch = profile_mismatch(options, source.home, target.home)
    if mismatch:
        raise SwitchError(mismatch)
    if writer_lock_held(target.home, thread_id):
        raise SwitchError(f"a live Codex already holds this thread open under {target.home}")
    relative = transcript.relative_to(source_home)
    known = sqlite_rollout_path(target.home, thread_id)
    if known and Path(known) != target.home / relative:
        raise SwitchError(
            f"seat {target.label!r} knows this thread under a different rollout ({known}) — "
            "resume it there by hand"
        )
    # (4) The launch record: options verbatim, launcher env carried (values in the record only).
    shell_env: dict[str, str] = {}
    shell_pid = shell_pid_of(codex_pid, ps)
    if shell_pid:
        try:
            shell_env = procargs(shell_pid)[2]
        except OSError:
            shell_env = {}
    carry = env_carry(codex_env, shell_env)
    record = LaunchRecord(
        token=secrets.token_hex(8),
        thread_id=thread_id,
        exe=exe or "codex",
        options=options,
        env=carry,
        source_home=str(source.home),
        target_home=str(target.home),
        source_label=source.label,
        target_label=target.label,
        transcript=str(relative),
        created=int(time.time()),
    )
    return Plan(
        record=record,
        source=source,
        target=target,
        codex_pid=codex_pid,
        target_note=note,
        env_names=tuple(sorted(carry)),
    )


def block(reason: str) -> str:
    """The JSON Codex expects from a hook that cancels the prompt."""
    return json.dumps({"decision": "block", "reason": reason})


def _log(thread_id: str, detail: str) -> None:
    from . import hooks  # pylint: disable=import-outside-toplevel

    hooks._log_event(thread_id, EVENT, detail)  # pylint: disable=protected-access


def run_switch(args: Any) -> int:  # pylint: disable=too-many-return-statements,too-many-locals
    """``ccc codex-switch`` — the hook side (stdin: the UserPromptSubmit JSON).

    Prints a block decision in EVERY outcome that concerns a switch, so the prompt never
    reaches the model; a non-switch prompt prints nothing and exits 0 (the hook script
    pre-filters, this is the belt to its braces). Exit 0 always — a non-zero exit is how
    a hook reports a failure, and Codex's fail-open would then run the turn.
    """
    source_file = getattr(args, "input", "") or ""
    raw = Path(source_file).read_text(encoding="utf-8") if source_file else sys.stdin.read()
    try:
        hook = parse_hook_input(raw)
    except ValueError as exc:
        print(block(f"switch refused: {exc}"))
        return 0
    matched, token, force = switch_target(hook.prompt)
    explicit = (getattr(args, "target", "") or "").strip()
    if not matched and not explicit:
        return 0
    if explicit:
        token, force = explicit.rstrip("!"), explicit.endswith("!") or force
    try:
        plan = plan_switch(
            hook, token, force=force, start_pid=int(getattr(args, "pid", 0) or 0) or None
        )
    except SwitchError as exc:
        _log(hook.session_id or "-", f"refused: {exc}")
        print(block(f"switch refused: {exc}"))
        return 0
    email = seat_email(plan.target)
    who = f"{plan.target.label} ({email})" if email else plan.target.label
    record = plan.record
    carried = f" carrying {', '.join(plan.env_names)}" if plan.env_names else ""
    if getattr(args, "dry_run", False):
        print(
            block(
                f"DRY RUN — would switch thread {record.thread_id} from seat "
                f"{plan.source.label!r} to {who} by quitting codex pid {plan.codex_pid} and "
                f"execing {record.exe} {' '.join(record.options)} resume {record.thread_id} "
                f"under CODEX_HOME={record.target_home}{carried}"
                + (f" [{plan.target_note}]" if plan.target_note else "")
            )
        )
        return 0
    iterm = os.environ.get("ITERM_SESSION_ID", "")
    pane = os.environ.get("TMUX_PANE", "")
    if not iterm and not pane:
        reason = "no iTerm tab or tmux pane to relaunch in ($ITERM_SESSION_ID / $TMUX_PANE unset)"
        _log(record.thread_id, f"refused: {reason}")
        print(block(f"switch refused: {reason}"))
        return 0
    from . import spawn  # pylint: disable=import-outside-toplevel

    write_record(record)
    spawn_args = [
        "codex-switch-now",
        "--token",
        record.token,
        "--pid",
        str(plan.codex_pid),
        "--hook-pid",
        str(os.getppid()),
        "--iterm",
        iterm,
        "--tmux-pane",
        pane,
    ]
    if not spawn.spawn_ccc(spawn_args):
        reason = (
            "could not spawn the relauncher (ccc codex-switch-now) — quit codex and run: "
            + exec_line(record.token)
        )
        _log(record.thread_id, f"refused: {reason}")
        print(block(f"switch refused: {reason}"))
        return 0
    mark_pending(record.thread_id, record.token)
    _log(
        record.thread_id,
        f"dispatched: {plan.source.label} → {plan.target.label} pid {plan.codex_pid} "
        f"token {record.token}{carried}",
    )
    print(
        block(
            f"⇄ switching this thread to seat {who}: Codex quits now and resumes here via "
            f"`{exec_line(record.token)}` (CODEX_HOME={record.target_home}) — nothing was "
            "sent to the model." + (f" Note: {plan.target_note}." if plan.target_note else "")
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# The detached relauncher
# --------------------------------------------------------------------------- #
def _wait_until(predicate: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SEC)


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def tty_free_of_codex(tty: str, table: dict[int, Any]) -> bool:
    """*tty* is owned by an idle shell: no ``codex`` program on it, a shell in the foreground.

    The Codex twin of :func:`terminal.tty_ready_for_input`, matching the PROGRAM name
    rather than a substring: a launcher's own command line (``tp open 210 -e codex``)
    mentions codex without being it.
    """
    from . import snapshot  # pylint: disable=import-outside-toplevel

    want = snapshot.normalize_tty(tty)
    if not want:
        return False
    on_tty = [row for row in table.values() if snapshot.normalize_tty(row.tty) == want]
    if not on_tty or any(_program_name(row.command) == "codex" for row in on_tty):
        return False
    foreground = [row for row in on_tty if "+" in row.stat]
    return bool(foreground) and all(
        _program_name(row.command) in _SHELL_NAMES for row in foreground
    )


def run_switch_now(args: Any) -> int:  # pylint: disable=too-many-return-statements,too-many-branches,too-many-statements,too-many-locals
    """``ccc codex-switch-now`` — quit the TUI, port the rollout, type the trampoline.

    Fail-closed before the source exits (abort, Codex left alive, notification) and
    fail-SAFE after it (the source seat is relaunched instead, then the notification).
    """
    from . import terminal  # pylint: disable=import-outside-toplevel

    token = (args.token or "").strip()
    pid = int(args.pid or 0)
    hook_pid = int(getattr(args, "hook_pid", 0) or 0)
    iterm = (args.iterm or "").strip()
    pane = (args.tmux_pane or "").strip()
    try:
        record = read_record(token)
    except SwitchError as exc:
        print(f"codex-switch-now: {exc}", file=sys.stderr)
        return 1
    thread_id = record.thread_id
    if pid <= 0:
        print("codex-switch-now: --pid required", file=sys.stderr)
        return 1

    def log(detail: str) -> None:
        _log(thread_id, detail)
        print(f"codex-switch-now: {detail}", file=sys.stderr)

    def notify(message: str) -> None:
        try:
            from . import notify as notify_mod  # pylint: disable=import-outside-toplevel

            notify_mod.notify("ccc codex-switch", message, config.load_config().notify)
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass

    def deliver(text: str, *, newline: bool = True) -> bool:
        if pane:
            return terminal.tmux_send_keys(pane, text, newline=newline)
        return terminal.type_into_iterm_session(iterm, text, newline=newline)

    def deliver_slash(command: str) -> bool:
        """Type a TUI slash command: the text first, the Return a beat later.

        The composer's paste-burst heuristic (``paste_burst.rs``: ≥ 3 chars within 8 ms
        each) would otherwise swallow a Return that arrives with the burst as a newline
        inside the "paste" and never submit the command.
        """
        if not deliver(command, newline=False):
            return False
        time.sleep(SLASH_ENTER_DELAY_SEC)
        return deliver("", newline=True)

    def abort(reason: str) -> int:
        """Before the source exited: leave it alive, say so."""
        clear_pending(thread_id)
        log(f"{reason} — Codex left running, nothing typed")
        notify(f"{thread_id[:8]}: {reason}. Quit Codex by hand, then run: {exec_line(token)}")
        return 1

    def fall_back(reason: str) -> int:
        """After the source exited: relaunch the SOURCE seat so the tab never ends dead."""
        clear_pending(thread_id)
        line = exec_line(token, source=True)
        typed = deliver(line)
        log(f"{reason} — {'relaunched the source seat' if typed else 'could not type'} ({line})")
        notify(
            f"{thread_id[:8]}: {reason}. "
            + (f"Back on seat {record.source_label!r}." if typed else f"Run by hand: {line}")
        )
        return 1

    # (1) The terminal: its tty binds the pid and proves idleness later.
    pane_pid, tty = 0, ""
    if pane:
        info = terminal.tmux_pane_info(pane)
        if info is None:
            return abort("the tmux pane is gone")
        pane_pid, tty = info[0], info[1]
        if tty and not tty.startswith("/dev/"):
            tty = f"/dev/{tty}"
    elif iterm:
        tty = terminal.iterm_session_tty(iterm)
    else:
        return abort("no terminal evidence (neither --iterm nor --tmux-pane)")
    if not tty:
        return abort("the tab's tty is unknown")

    def bound_tui(table: dict[int, Any]) -> str:
        """ "" when *pid* is still the foreground codex on this tty, else the reason."""
        row = table.get(pid)
        if row is None or _program_name(row.command) != "codex":
            return f"pid {pid} is not a live codex process"
        if terminal.pid_tty(pid, table) != tty:
            return f"codex pid {pid} does not sit on this tab's tty {tty}"
        if "+" not in row.stat:
            return f"codex pid {pid} is no longer the foreground process"
        if pane and not terminal.pid_descends_from(pid, pane_pid, table):
            return "the codex pid is not inside the tmux pane"
        return ""

    problem = bound_tui(terminal.ps_table())
    if problem:
        return abort(problem)
    started = terminal.pid_start(pid)
    # (2) The hook's block must have landed: wait for the hook process itself to be
    # gone (Codex consumed its answer), then settle, then re-bind before typing.
    if hook_pid > 0 and not _wait_until(lambda: _pid_gone(hook_pid), HOOK_EXIT_WAIT_SEC):
        return abort(f"the hook process {hook_pid} did not exit within {HOOK_EXIT_WAIT_SEC:.0f}s")
    time.sleep(0.5)
    problem = bound_tui(terminal.ps_table())
    if problem:
        return abort(problem)
    if started and terminal.pid_start(pid) != started:
        return abort(f"pid {pid} was recycled before /quit")
    if not deliver_slash("/quit"):
        return abort("could not type /quit into the tab")
    if not _wait_until(lambda: _pid_gone(pid), QUIT_WAIT_SEC):
        return abort(f"codex pid {pid} did not quit within {QUIT_WAIT_SEC:.0f}s of /quit")
    # (3) The tty must be back at a shell prompt — twice, a settle apart — and the source
    # writer lock released: a launcher wrapping the TUI (tp) exits only after its own
    # bookkeeping, and text typed before that lands in it.
    if not _wait_until(lambda: tty_free_of_codex(tty, terminal.ps_table()), READY_WAIT_SEC):
        return fall_back(f"{tty} did not return to a shell prompt within {READY_WAIT_SEC:.0f}s")
    time.sleep(READY_SETTLE_SEC)
    if not tty_free_of_codex(tty, terminal.ps_table()):
        return fall_back(f"{tty} did not stay at a shell prompt")
    if not _wait_until(
        lambda: not writer_lock_held(Path(record.source_home), thread_id), QUIT_WAIT_SEC
    ):
        return fall_back("the source seat still holds the thread's writer lock")
    # (4) Port the rollout now that the writer is gone and the file is complete.
    try:
        ported = port_rollout(record)
    except SwitchError as exc:
        return fall_back(str(exc))
    # (5) Deliver into the same pane/tab. No new-tab fallback.
    line = exec_line(token)
    if not deliver(line):
        return fall_back("could not type the relaunch into the tab")
    clear_pending(thread_id)
    log(
        f"relaunched on seat {record.target_label!r} ({ported.name}) via "
        f"{'tmux' if pane else 'iterm'}: {line}"
    )
    return 0


# --------------------------------------------------------------------------- #
# The trampoline
# --------------------------------------------------------------------------- #
def exec_argv_env(
    record: LaunchRecord, *, source: bool = False, base_env: dict[str, str] | None = None
) -> tuple[list[str], dict[str, str]]:
    """The ``(argv, environ)`` :func:`run_switch_exec` execs (pure, for tests)."""
    home = record.source_home if source else record.target_home
    env = dict(os.environ if base_env is None else base_env)
    for name in _ENV_NEVER:
        env.pop(name, None)
    env.update(record.env)
    env["CODEX_HOME"] = home
    argv = ["codex", *record.options, "resume", record.thread_id]
    return argv, env


def run_switch_exec(args: Any) -> int:
    """``ccc codex-switch-exec <token> [-S]`` — exec codex from the launch record.

    Replaces this process (the shell's foreground job becomes codex itself). ``-S``
    relaunches the SOURCE seat — the relauncher's fall-back after a failed port.
    """
    try:
        record = read_record((args.token or "").strip())
    except SwitchError as exc:
        print(f"codex-switch-exec: {exc}", file=sys.stderr)
        return 1
    source = bool(getattr(args, "source", False))
    exe = record.exe
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        print(f"codex-switch-exec: {exe} is not executable — run codex by hand", file=sys.stderr)
        return 1
    argv, env = exec_argv_env(record, source=source)
    label = record.source_label if source else record.target_label
    _log(record.thread_id, f"exec seat {label!r}: {exe} {' '.join(argv[1:])}")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(exe, argv, env)
    return 1  # pragma: no cover - execve does not return
