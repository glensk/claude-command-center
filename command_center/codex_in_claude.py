#!/usr/bin/env python3
# pylint: disable=invalid-name  # filename intentionally hyphenated (matches codex-review.py)
"""codex-in-claude.py — pick the OpenAI Codex model for Claude Code's codex commands,
and run one delegated task round (engine behind /codex-implement-task-and-claude-review).

This single script is the shared control point for every Codex-related Claude Code
command (``/codex-implement-task-and-claude-review`` and ``/codex-debate``):

* ``models``      — list the Codex models available on this login.
* ``get-model``   — print the model resolved for a given command.
* ``set-model``   — set the model for a command (or the global default).
* ``pick``        — interactive numbered picker for the model (terminal only).
* ``sync-skills`` — stamp the resolved model into the ``description:`` frontmatter of the
                    codex skills/commands, so Claude Code's ``/codex…`` help shows it.
* ``delegate``    — run ONE Codex round, **printing the model as the first stdout line**,
                    then Codex's reply. Used by the codex-implement-task-and-claude-review skill.
* ``usage``       — print the current Codex rate-limit usage (5h + weekly windows).
* ``headroom``    — decide whether enough quota remains for optional offload work.

Model source: ``codex debug models`` (``--refresh``), else the offline cache
``~/.codex/models_cache.json`` (fast / wifi-friendly default). Only models with
``visibility == "list"`` are shown unless ``--include-hidden``.

Config (JSON, atomic writes)::

    ~/.config/codex-in-claude/config.json        # override via $CODEX_IN_CLAUDE_CONFIG
    {"default": "gpt-5.6-sol", "delegate-review": null, "debate": null}

Resolution order for a command: per-command value -> ``default`` -> ``gpt-5.6-sol``.

``set-model`` / ``set-effort`` / ``pick`` also re-stamp the ``[codex <model> effort=<e>]``
marker into the codex skill/command descriptions (see ``sync-skills``), so the model in
Claude Code's slash-command help never drifts from the config.

``delegate`` exit codes (the skill branches on these):
  0 ok | 2 usage | 3 invalid-model | 4 codex-missing-or-auth | 5 timeout-or-stall |
  6 codex-nonzero | 7 bad-patch (reserved) | 8 quota-exhausted (skipped, see below).

Launch policy: every ``codex exec`` line is assembled by
:mod:`command_center.codex_launch` — the named ``hardened-ro``/``hardened-rw`` permission
profile (NEVER ``-s/--sandbox``, which forces the legacy sandbox and drops the profile's deny
rules), a validated absolute ``-C`` root (``$HOME`` and its ancestors refused; an implicit cwd
only inside a git work tree), and a 0600 session journal that ``--resume`` re-validates against.
A refused launch exits ``EX_USAGE`` (2) with the reason.

Supervision: ``codex exec`` runs in its own process group and the WHOLE tree is killed on
wall timeout, idle stall, or parent SIGTERM/SIGINT — a killed delegate can never leave
codex editing the workspace behind the caller's back. The wall timeout defaults by effort
(``DEFAULT_TIMEOUTS``: low 600 s .. xhigh 2700 s; ``-t`` overrides, ``-t 0`` = no wall at
all — the recommended mode when the task simply takes as long as it takes), an idle
watchdog (``-i``, default 900 s of total silence) converts hangs into fast failures, codex
stderr is streamed through (``codex› `` prefix) for live progress, and the prompt itself
tells codex its time budget so it spends the clock implementing instead of exploring.

Watchability + rounds: every run refreshes a heartbeat JSON under ``RUNS_DIR``; ``runs``
lists all in-flight delegates (elapsed, idle seconds, output volume, last line) from one
file read each — the cheap way to check on a long run without reading transcripts. Each
run reports its codex session UUID as a final ``### SESSION`` line (also included in
timeout/stall errors); ``--resume <uuid>`` re-attaches the next round (or a retry after a
kill) to that session with all its discovered context intact. Unless ``--no-repo-map`` is
given, the prompt is prefixed with the repo's ``repo_scope_short.md`` (or a git top-level
summary) so codex starts oriented instead of exploring from zero.

Concurrency + quota awareness: each ``delegate`` first runs a **quota preflight** — it reads
the Codex ``rate_limits`` (5h + weekly) from ``$CODEX_HOME/sessions/**/rollout-*.jsonl`` and,
if a live window is ``>=100%`` used, prints the reset time and exits ``EX_QUOTA`` (8) WITHOUT
launching codex (bypass: ``-Q`` / ``$CODEX_IN_CLAUDE_IGNORE_QUOTA``). It then takes one of N
cross-process flock slots, where the effective cap is **usage-tapered**: <50% used -> 3,
50-75% -> 2, >75% -> 1 (N ceiling = ``-j/--max-concurrent`` -> ``$CODEX_IN_CLAUDE_MAX_CONCURRENT``
-> 3; ``0`` = unlimited). Stale windows (reset already passed) are ignored; unknown usage fails
open. Keeps a fan-out from thrashing CPU/API and from slamming the wall with many in-flight runs.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import argparse
import contextlib
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

from . import codex_launch

DEFAULT_MODEL = "gpt-5.6-sol"  # newest/best per the Codex catalog
COMMANDS = ("delegate-review", "debate")  # codex-related commands this manager governs
EFFORTS = ("low", "medium", "high", "xhigh")  # codex reasoning levels (API-validated)
CODEX_CACHE = Path.home() / ".codex" / "models_cache.json"

# Concurrency cap: at most this many `delegate` processes run `codex exec` at once.
DEFAULT_MAX_CONCURRENT = 3
SLOT_DIR = Path(
    os.environ.get(
        "CODEX_IN_CLAUDE_SLOT_DIR",
        str(Path.home() / ".config" / "codex-in-claude" / "slots"),
    )
)

# Default wall timeout for one delegate round, keyed by reasoning effort. Higher
# effort reasons (and therefore explores) far longer: a fixed 600s default made
# every xhigh run on a non-trivial repo time out during discovery.
# ``-t 0`` disables the wall entirely (the idle watchdog still guards stalls).
DEFAULT_TIMEOUTS = {"low": 600, "medium": 900, "high": 1500, "xhigh": 2700}
# Kill a run after this long with NO output at all (network hang / wedged CLI),
# clamped to the wall timeout. 0 disables.
DEFAULT_IDLE_TIMEOUT = 900

# Heartbeat files for in-flight delegate runs (see ``runs``): one small JSON per
# running delegate, refreshed every few seconds, removed on exit. Lets a caller
# check progress cheaply (one file read) instead of tailing full transcripts.
RUNS_DIR = Path(
    os.environ.get(
        "CODEX_IN_CLAUDE_RUNS_DIR",
        str(Path.home() / ".config" / "codex-in-claude" / "runs"),
    )
)

# codex exec prints its session id in the startup banner; captured so a later
# round (or a retry after a kill) can `codex exec resume <id>` with the
# session's discovered context intact.
_SESSION_RE = re.compile(
    r"session id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

# delegate exit codes
EX_OK, EX_USAGE = 0, 2
EX_INVALID_MODEL, EX_NO_CODEX, EX_TIMEOUT, EX_CODEX_FAIL, EX_BAD_PATCH = 3, 4, 5, 6, 7
EX_QUOTA = 8  # skipped: Codex quota exhausted (>=100% used on a live window)


def _effective_timeout(explicit: int | None, effort: str) -> int:
    """Wall timeout for a round: explicit ``-t`` (0 = unlimited), else effort-scaled."""
    if explicit is not None:
        return max(0, explicit)
    return DEFAULT_TIMEOUTS.get(effort, DEFAULT_TIMEOUTS["medium"])


def _session_id_of(text: str) -> str | None:
    """The codex session UUID from a run's stderr banner, if present."""
    match = _SESSION_RE.search(text)
    return match.group(1) if match else None


def _write_heartbeat(
    path: Path,
    meta: dict[str, Any],
    started: float,
    last_activity: float,
    out_buf: list[str],
    err_buf: list[str],
) -> None:
    """Atomically refresh one run's heartbeat JSON (never fatal)."""
    now = time.monotonic()
    last_line = next((line.strip() for line in reversed(err_buf or out_buf) if line.strip()), "")
    payload = {
        "pid": os.getpid(),
        **meta,
        "elapsed_s": int(now - started),
        "idle_s": int(now - last_activity),
        "lines": len(out_buf) + len(err_buf),
        "last_line": last_line[:200],
        "updated": int(time.time()),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


class CodexStalledError(RuntimeError):
    """Codex produced no output for longer than the idle watchdog allows."""

    def __init__(self, idle_seconds: int, stderr_text: str) -> None:
        super().__init__(f"no codex output for {idle_seconds}s")
        self.idle_seconds = idle_seconds
        self.stderr_text = stderr_text


def _exec_codex(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    idle_timeout: int = 0,
    heartbeat_path: Path | None = None,
    heartbeat_meta: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``codex exec`` supervised; the process TREE can never outlive us.

    Differences from a bare ``subprocess.run``:

    - the child gets its own process group (``start_new_session``) and the whole
      group is killed (SIGTERM, 2 s grace, SIGKILL) on wall timeout, idle stall,
      parent SIGTERM/SIGINT, or any exception — a killed delegate previously
      left ``codex exec`` (and its shell-tool children) running detached, still
      editing the workspace in ``--write`` mode;
    - codex's stderr is streamed line-by-line to our stderr (prefixed
      ``codex› ``) so a caller watching the output file can SEE whether codex is
      exploring, editing, or hung — instead of total silence until the end;
    - an idle watchdog (``idle_timeout`` seconds with no output on either
      stream) converts a wedged run into a fast, diagnosable failure.

    Raises FileNotFoundError (binary missing), subprocess.TimeoutExpired (wall),
    or CodexStalledError (idle) — partial output attached to the last two.
    """
    proc = subprocess.Popen(  # pylint: disable=consider-using-with  # lifetime managed by the finally sweep
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    out_buf: list[str] = []
    err_buf: list[str] = []
    last_activity = [time.monotonic()]

    def reader(stream: Any, buf: list[str], tee: bool) -> None:
        for line in stream:
            buf.append(line)
            last_activity[0] = time.monotonic()
            if tee:
                shown = line if len(line) <= 400 else line[:400] + "…\n"
                sys.stderr.write("codex› " + shown)
                sys.stderr.flush()
        stream.close()

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, out_buf, False), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, err_buf, True), daemon=True),
    ]
    for thread in threads:
        thread.start()

    def kill_group() -> None:
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)

    previous: dict[int, Any] = {}

    def relay(signum: int, _frame: Any) -> None:
        kill_group()
        signal.signal(signum, previous[signum])  # restore, then re-deliver
        os.kill(os.getpid(), signum)

    # Signal handlers are only legal in the main thread; elsewhere the finally
    # sweep still covers every non-signal exit path.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, relay)
    start = time.monotonic()
    tick = 0
    try:
        while True:
            if heartbeat_path is not None and tick % 5 == 0:
                _write_heartbeat(
                    heartbeat_path, heartbeat_meta or {}, start, last_activity[0], out_buf, err_buf
                )
            tick += 1
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if timeout and now - start >= timeout:
                    kill_group()
                    raise subprocess.TimeoutExpired(
                        cmd, timeout, output="".join(out_buf), stderr="".join(err_buf)
                    ) from None
                if idle_timeout and now - last_activity[0] >= idle_timeout:
                    kill_group()
                    raise CodexStalledError(int(now - last_activity[0]), "".join(err_buf)) from None
        for thread in threads:
            thread.join(timeout=2)
        return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out_buf), "".join(err_buf))
    finally:
        kill_group()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if heartbeat_path is not None:
            with contextlib.suppress(OSError):
                heartbeat_path.unlink()
            with contextlib.suppress(OSError):
                heartbeat_path.with_suffix(".tmp").unlink()


def _stderr_tail(text: str, lines: int = 8) -> str:
    """The last few non-empty stderr lines — what codex was doing when killed."""
    kept = [line for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _repo_map(cwd: str | None, explicit: str | None = None, limit: int = 4000) -> str | None:
    """A compact orientation map of the repo, injected into the delegate prompt.

    Discovery is what burns delegate wall-clock: a run that has to find its way
    around a repo can spend its whole budget exploring. ``explicit`` (the
    ``--repo-map FILE`` flag) wins — the caller curates exactly the context
    codex gets; else prefer the repo's own ``repo_scope_short.md`` (a
    human-curated map); else fall back to a top-level tracked-file summary from
    git. Never fatal — returns None when nothing useful exists.
    """
    if explicit:
        try:
            text = Path(explicit).read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            print(f"WARNING: --repo-map {explicit} unreadable ({exc}); no map.", file=sys.stderr)
            return None
        if not text:
            return None
        return f"REPO MAP (from {Path(explicit).name} — trust it for orientation):\n{text[:limit]}"
    if not cwd:
        return None
    root = Path(cwd)
    with contextlib.suppress(OSError):
        scope = root / "repo_scope_short.md"
        if scope.is_file():
            text = scope.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return (
                    "REPO MAP (from repo_scope_short.md — trust it for orientation):\n"
                    + text[:limit]
                )
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not listing.strip():
        return None
    counts: dict[str, int] = {}
    top_files: list[str] = []
    for line in listing.splitlines():
        if "/" in line:
            top = line.split("/", 1)[0] + "/"
            counts[top] = counts.get(top, 0) + 1
        else:
            top_files.append(line)
    entries = [f"{d} ({n} files)" for d, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    entries += top_files
    return "REPO MAP (top-level tracked entries):\n" + "\n".join(entries[:40])


# Codex usage (rate-limit) reading — Codex has no usage API; it writes a rate_limits
# block onto token_count events in $CODEX_HOME/sessions/**/rollout-*.jsonl.
_CODEX_SCAN_LIMIT = (
    200  # newest rollout files to scan for a usable window (short runs log windowless)
)

# Optional offloads should leave room for interactive/debate work.  The function seam
# below deliberately owns this bootstrap value so a learned reserve (3 x P95 debate-round
# cost + 10% margin) can replace it without changing the decision/output machinery.
_HEADROOM_BOOTSTRAP_RESERVE_PERCENT = 35.0
_HEADROOM_MIN_SAMPLES = 10
_HEADROOM_MIN_RESERVE_PERCENT = 5.0
_HEADROOM_MAX_RESERVE_PERCENT = 60.0
_HEADROOM_STALE_AFTER_SECONDS = 6 * 3600
_HEADROOM_RESET_FRESH_SECONDS = 10 * 60
_COST_HISTORY_SECONDS = 90 * 24 * 3600
_FIVE_HOUR_MINUTES = 5 * 60
_SEVEN_DAY_MINUTES = 7 * 24 * 60


# --------------------------------------------------------------------------- #
# Clickable-terminal helpers (OSC 8) — per repo convention.
# --------------------------------------------------------------------------- #
def osc8_link(target: str, label: str | None = None) -> str:
    """Wrap *target* (URL) as an OSC 8 hyperlink; degrades to plain text."""
    label = label or target
    return f"\x1b]8;;{target}\x1b\\{label}\x1b]8;;\x1b\\"


def local_link(path: Path) -> str:
    """Clickable link to a local *path* (openterm:// for iTerm2/WezTerm, file:// fallback)."""
    abspath = str(path.resolve())
    return osc8_link("openterm://" + urllib.parse.quote(abspath), abspath)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def config_path() -> Path:
    """Shared config file path ($CODEX_IN_CLAUDE_CONFIG override)."""
    env = os.environ.get("CODEX_IN_CLAUDE_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "codex-in-claude" / "config.json"


def load_config() -> dict:
    """Load the config, tolerating a missing/corrupt file (returns defaults).

    ``codex_home`` / ``codex_home_until`` are the account pin (see
    :func:`pinned_codex_home`); ``codex-review.py`` reads the same two keys.
    """
    path = config_path()
    base = {
        "default": DEFAULT_MODEL,
        "delegate-review": None,
        "debate": None,
        "effort": "xhigh",
        "codex_home": None,
        "codex_home_until": None,
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base.update(data)
    except (OSError, ValueError):
        pass
    return base


def save_config(cfg: dict) -> Path:
    """Atomically persist *cfg*; returns the path written."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=str(path.parent), prefix=".cfg.", suffix=".tmp", delete=False
    ) as handle:
        json.dump(cfg, handle, indent=2, sort_keys=True)
        handle.write("\n")
        tmp = handle.name
    os.replace(tmp, path)
    return path


def resolve_model(for_command: str | None) -> str:
    """Resolve the model for *for_command*: per-command -> default -> DEFAULT_MODEL."""
    cfg = load_config()
    if for_command and cfg.get(for_command):
        return str(cfg[for_command])
    return str(cfg.get("default") or DEFAULT_MODEL)


def resolve_effort() -> str | None:
    """Configured reasoning effort, or None to use the model's catalog default."""
    val = load_config().get("effort")
    return str(val) if val in EFFORTS else None


# --------------------------------------------------------------------------- #
# Description markers — make the active model visible in Claude Code's help
#
# Claude Code renders the one-line help for `/codex-debate` and
# `/codex-implement-task-and-claude-review` from the `description:` YAML frontmatter of
# their skill/command markdown, and that text is STATIC (read at session start). So the
# model is surfaced by stamping a leading `[codex <model> effort=<e>]` marker into those
# descriptions whenever the config changes — a prefix, so it survives the terminal's
# right-truncation of long descriptions.
# --------------------------------------------------------------------------- #
MARKER_RE = re.compile(r"^\[codex\s+[^\]]*\]\s*")
_BLOCK_SCALARS = (">", ">-", ">+", "|", "|-", "|+")


def claude_home() -> Path:
    """Claude Code's config dir ($CLAUDE_CONFIG_DIR override, else ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def marker_surfaces() -> list[tuple[str, Path]]:
    """(config key, markdown file) pairs whose ``description:`` carries the model marker.

    Skill files and command files both count: a command file shadows a same-named skill in
    the slash-command listing, so whichever exists must stay in sync. Missing paths are
    reported, never an error — not every surface exists on every machine.
    """
    home = claude_home()
    return [
        ("debate", home / "skills" / "codex-debate" / "SKILL.md"),
        (
            "delegate-review",
            home / "skills" / "codex-implement-task-and-claude-review" / "SKILL.md",
        ),
        ("debate", home / "commands" / "codex-debate.md"),
        ("delegate-review", home / "commands" / "codex-implement-task-and-claude-review.md"),
        ("", home / "commands" / "codex-model.md"),  # "" = the global default model
    ]


def marker_for(key: str) -> str:
    """The ``[codex <model> effort=<e>]`` marker for config *key* (``debate`` / …)."""
    model = resolve_model(key)
    effort = resolve_effort() or effort_of(model)
    return f"[codex {model} effort={effort}]" if effort != "?" else f"[codex {model}]"


def _yaml_dq(value: str) -> str:
    """Double-quote *value* as a YAML scalar (escaping ``\\`` and ``"``).

    Needed because the marker starts with ``[``, which YAML would read as a flow sequence
    at the head of a plain scalar. Block scalars are literal text and need no quoting.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def inject_marker(text: str, marker: str) -> str:
    """Return *text* with its frontmatter ``description:`` prefixed by *marker*.

    Idempotent: an existing marker is replaced, never stacked. Raises ``ValueError`` when
    the file has no usable frontmatter description.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("no YAML frontmatter")
    end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), None)
    if end is None:
        raise ValueError("unterminated YAML frontmatter")
    idx = next((i for i in range(1, end) if lines[i].startswith("description:")), None)
    if idx is None:
        raise ValueError("no description: key in frontmatter")

    value = lines[idx].partition(":")[2].strip()
    if value in _BLOCK_SCALARS:
        # Folded/literal block: stamp the first non-blank continuation line.
        body = next((i for i in range(idx + 1, end) if lines[i].strip()), None)
        if body is None:
            raise ValueError("empty block description")
        indent = lines[body][: len(lines[body]) - len(lines[body].lstrip())]
        content = MARKER_RE.sub("", lines[body].strip(), count=1)
        lines[body] = f"{indent}{marker} {content}\n"
    else:
        quote = value[0] if value[:1] in ("'", '"') else ""
        inner = value[1:-1] if quote and value.endswith(quote) and len(value) > 1 else value
        if quote == '"':  # undo YAML escaping before re-quoting
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        elif quote == "'":
            inner = inner.replace("''", "'")
        inner = MARKER_RE.sub("", inner, count=1)
        lines[idx] = f"description: {_yaml_dq(f'{marker} {inner}')}\n"
    return "".join(lines)


def sync_markers() -> list[tuple[str, Path, str]]:
    """Stamp the current model marker into every surface; returns (status, path, detail).

    Status is ``updated`` / ``ok`` (already current) / ``missing`` / ``error``. Files are
    rewritten **in place** (truncate + write, never temp+rename): a dotfiles setup often
    hard-links these paths to a tracked working copy, and an atomic replace would break the
    link and silently fork the two copies.
    """
    out: list[tuple[str, Path, str]] = []
    for key, path in marker_surfaces():
        if not path.exists():
            out.append(("missing", path, ""))
            continue
        try:
            old = path.read_text(encoding="utf-8")
            new = inject_marker(old, marker_for(key))
        except (OSError, ValueError) as exc:
            out.append(("error", path, str(exc)))
            continue
        if new == old:
            out.append(("ok", path, key))
            continue
        try:
            path.write_text(new, encoding="utf-8")
        except OSError as exc:
            out.append(("error", path, str(exc)))
            continue
        out.append(("updated", path, key))
    return out


# --------------------------------------------------------------------------- #
# Model catalog
# --------------------------------------------------------------------------- #
def _parse_models(blob: str) -> list[dict]:
    """Extract the model list from a `codex debug models` / cache JSON blob."""
    data = json.loads(blob)
    models = data if isinstance(data, list) else data.get("models", [])
    return [m for m in models if isinstance(m, dict)]


def list_models(*, refresh: bool, include_hidden: bool, timeout: int = 30) -> list[dict]:
    """Return the available Codex models.

    Default reads the offline cache (fast, works on a bad connection); ``--refresh``
    calls ``codex debug models`` (network) and falls back to the cache on failure.
    """
    models: list[dict] = []
    if refresh:
        try:
            proc = subprocess.run(
                ["codex", "debug", "models"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                models = _parse_models(proc.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            models = []
    if not models:
        try:
            models = _parse_models(CODEX_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            models = []
    if not include_hidden:
        models = [m for m in models if m.get("visibility", "list") == "list"]
    return models


def effort_of(slug: str) -> str:
    """Catalog default reasoning effort for *slug* (informational), or '?'."""
    for m in list_models(refresh=False, include_hidden=True):
        if m.get("slug") == slug:
            return str(m.get("default_reasoning_level") or "?")
    return "?"


def valid_slug(slug: str) -> bool:
    """True if *slug* is a known model (visible or hidden)."""
    return any(m.get("slug") == slug for m in list_models(refresh=False, include_hidden=True))


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_models(args: argparse.Namespace) -> int:
    """List models, starring the ones currently configured."""
    models = list_models(refresh=args.refresh, include_hidden=args.include_hidden)
    if not models:
        print(
            "No models found (cache empty and/or `codex debug models` unavailable).\n"
            "Try: codex-in-claude.py models --refresh",
            file=sys.stderr,
        )
        return EX_NO_CODEX
    cfg = load_config()
    configured = {cfg.get("default"), cfg.get("delegate-review"), cfg.get("debate")}
    width = max(len(str(m.get("slug", ""))) for m in models)
    print(f"Available Codex models  (default: {cfg.get('default') or DEFAULT_MODEL})\n")
    for m in models:
        slug = str(m.get("slug", ""))
        star = "*" if slug in configured else " "
        eff = str(m.get("default_reasoning_level") or "?")
        vis = str(m.get("visibility", "list"))
        hide = "  [hidden]" if vis != "list" else ""
        desc = str(m.get("description") or "")
        print(f" {star} {slug:<{width}}  effort={eff:<7}{hide}  {desc}")
    print()
    print("per-command:")
    for cmd in COMMANDS:
        print(f"    {cmd:<16} -> {resolve_model(cmd)}")
    print(f"    {'effort':<16} -> {resolve_effort() or 'default (each model own)'}")
    print(f"\nconfig: {local_link(config_path())}")
    print("change: codex-in-claude.py pick   |   set-model <slug> [--for debate|delegate-review]")
    return EX_OK


def cmd_get_model(args: argparse.Namespace) -> int:
    """Print the resolved model for a command (or the global default)."""
    print(resolve_model(args.for_command))
    return EX_OK


def _report_sync(rows: list[tuple[str, Path, str]], *, verbose: bool = False) -> None:
    """Print a one-line-per-surface summary of a marker sync (quiet unless interesting)."""
    icon = {"updated": "✅", "ok": "•", "missing": "–", "error": "❌"}
    for status, path, detail in rows:
        if status == "ok" and not verbose:
            continue
        if status == "missing" and not verbose:
            continue
        suffix = f"  ({detail})" if detail and status == "error" else ""
        print(f"  {icon.get(status, '?')} {status:<8} {path}{suffix}")


def cmd_sync_skills(args: argparse.Namespace) -> int:
    """Stamp the resolved model into the codex skill/command descriptions."""
    if args.check:
        stale: list[Path] = []
        bad: list[tuple[Path, str]] = []
        for key, path in marker_surfaces():
            if not path.exists():
                continue
            try:
                old = path.read_text(encoding="utf-8")
                if inject_marker(old, marker_for(key)) != old:
                    stale.append(path)
            except (OSError, ValueError) as exc:
                bad.append((path, str(exc)))
        for path, why in bad:
            print(f"  ❌ error    {path}  ({why})", file=sys.stderr)
        for path in stale:
            print(f"  ⚠️  stale    {path}")
        if bad:
            return EX_USAGE
        if stale:
            print("\nrun: codex-in-claude.py sync-skills")
            return 1
        print("all codex skill/command descriptions are current")
        return EX_OK

    rows = sync_markers()
    _report_sync(rows, verbose=True)
    if any(status == "error" for status, _, _ in rows):
        return EX_USAGE
    if any(status == "updated" for status, _, _ in rows):
        print("\nNote: Claude Code reads these descriptions at session start —")
        print("      the new model shows in `/codex…` help in the NEXT session.")
    return EX_OK


def cmd_pick(args: argparse.Namespace) -> int:
    """Interactive numbered model picker (terminal only), then set + sync."""
    models = list_models(refresh=args.refresh, include_hidden=False)
    if not models:
        print(
            "No models found (cache empty and/or `codex debug models` unavailable).\n"
            "Try: codex-in-claude.py models --refresh",
            file=sys.stderr,
        )
        return EX_NO_CODEX
    if not sys.stdin.isatty() or os.environ.get("CI"):
        print(
            "pick needs an interactive terminal. Non-interactive alternative:\n"
            "  codex-in-claude.py set-model <slug> [--for delegate-review|debate|all]",
            file=sys.stderr,
        )
        return EX_USAGE
    target = args.for_command or "all"
    current = resolve_model(None if target == "all" else target)
    width = max(len(str(m.get("slug", ""))) for m in models)
    print(f"Pick the Codex model for {target}  (current: {current})\n")
    for num, m in enumerate(models, 1):
        slug = str(m.get("slug", ""))
        mark = "*" if slug == current else " "
        eff = str(m.get("default_reasoning_level") or "?")
        print(f" {mark} {num}) {slug:<{width}}  effort={eff:<7}  {m.get('description') or ''}")
    try:
        raw = input("\nnumber (Enter = keep current): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return EX_USAGE
    if not raw:
        print(f"kept {current}")
        return EX_OK
    if not raw.isdigit() or not 1 <= int(raw) <= len(models):
        print(f"Not a listed number: {raw!r}", file=sys.stderr)
        return EX_USAGE
    args.slug = str(models[int(raw) - 1].get("slug", ""))
    args.for_command = target
    return cmd_set_model(args)


def cmd_set_model(args: argparse.Namespace) -> int:
    """Set the model for a command (or the global default with --for all/omitted)."""
    slug = args.slug
    if not valid_slug(slug):
        known = ", ".join(
            str(m.get("slug")) for m in list_models(refresh=False, include_hidden=False)
        )
        print(
            f"Unknown model '{slug}'. Known (visible): {known or '(none)'}\n"
            "Use --include-hidden via `models -H` to see hidden ones.",
            file=sys.stderr,
        )
        return EX_INVALID_MODEL
    cfg = load_config()
    target = args.for_command
    if target in (None, "all"):
        cfg["default"] = slug
        where = "default (all commands)"
        if target == "all":  # "all" also clears per-command pins, else they'd shadow it
            for cmd in COMMANDS:
                cfg[cmd] = None
    else:
        cfg[target] = slug
        where = target
    path = save_config(cfg)
    print(f"set {where} model -> {slug}\nconfig: {path}")
    _report_sync(sync_markers())
    return EX_OK


def cmd_home(args: argparse.Namespace) -> int:
    """``home [PATH] [-u DATE] [-c]`` — show, set or clear the Codex account pin.

    No arguments: print the effective ``CODEX_HOME`` and where it comes from (env / pin /
    default), the pin's expiry, and the account e-mail behind that home when readable.
    ``PATH`` sets the pin (must hold an ``auth.json``, i.e. a completed
    ``CODEX_HOME=<PATH> codex login``); ``-u/--until`` bounds it (ISO date, inclusive);
    ``-c/--clear`` removes it. The pin governs ``delegate``, ``usage``/``headroom`` and
    ``codex-review.py`` (debates) alike; an explicit ``$CODEX_HOME`` still overrides it.
    """
    cfg = load_config()
    if args.clear:
        cfg["codex_home"] = None
        cfg["codex_home_until"] = None
        save_config(cfg)
        print("codex home pin cleared — back to $CODEX_HOME / ~/.codex")
        return EX_OK
    if args.path:
        home = Path(args.path).expanduser()
        if not (home / "auth.json").is_file():
            print(
                f"error: {home} has no auth.json — run `CODEX_HOME={home} codex login` first",
                file=sys.stderr,
            )
            return EX_USAGE
        if args.until:
            try:
                date.fromisoformat(args.until)
            except ValueError:
                print(f"error: -u/--until must be an ISO date, got {args.until!r}", file=sys.stderr)
                return EX_USAGE
        cfg["codex_home"] = str(home)
        cfg["codex_home_until"] = args.until or None
        save_config(cfg)
    elif args.until:
        print("error: -u/--until needs a PATH (or use -c to clear)", file=sys.stderr)
        return EX_USAGE

    effective = _codex_home()
    if os.environ.get("CODEX_HOME"):
        source = "env $CODEX_HOME"
    elif pinned_codex_home(cfg) is not None:
        until = cfg.get("codex_home_until")
        source = f"config pin (until {until}, inclusive)" if until else "config pin (no expiry)"
    else:
        source = "default"
        if cfg.get("codex_home"):
            source += f" (pin on {cfg['codex_home']} expired {cfg.get('codex_home_until')})"
    email = ""
    try:
        from .usage import codex_account_email  # pylint: disable=import-outside-toplevel

        email = codex_account_email(effective) or ""
    except Exception:  # pylint: disable=broad-exception-caught  # display-only
        email = ""
    if getattr(args, "json", False):
        # The machine contract external drivers (codex-review.py) consume instead of
        # re-implementing pin/hold resolution — ONE selector, everywhere.
        label = "default"
        try:
            from . import quota  # pylint: disable=import-outside-toplevel

            for name, home_path in quota._canonical_codex_homes().items():  # noqa: SLF001
                if home_path.expanduser().resolve() == effective.expanduser().resolve():
                    label = name
                    break
        except Exception:  # pylint: disable=broad-exception-caught  # metadata only
            pass
        print(
            json.dumps(
                {
                    "home": str(effective),
                    "source": source,
                    "label": label,
                    "email": email,
                    "until": str(cfg.get("codex_home_until") or ""),
                }
            )
        )
        return EX_OK
    print(f"codex home: {effective}  [{source}]" + (f"  account: {email}" if email else ""))
    return EX_OK


def cmd_get_effort(_args: argparse.Namespace) -> int:
    """Print the configured reasoning effort (or 'default' = the model's own default)."""
    print(resolve_effort() or "default")
    return EX_OK


def cmd_set_effort(args: argparse.Namespace) -> int:
    """Set the global reasoning effort; 'default' clears it (each model uses its own)."""
    level = args.level
    cfg = load_config()
    if level == "default":
        cfg["effort"] = None
        msg = "effort -> default (each model's own)"
    elif level in EFFORTS:
        cfg["effort"] = level
        msg = f"effort -> {level}"
    else:
        print(f"Unknown effort '{level}'. Choose: {', '.join(EFFORTS)}, default.", file=sys.stderr)
        return EX_USAGE
    path = save_config(cfg)
    print(f"{msg}\nconfig: {path}")
    _report_sync(sync_markers())
    return EX_OK


_PATCH_CONTRACT = (
    "You are Codex, implementing a task delegated by Claude Code. You are READ-ONLY: "
    "you CANNOT edit files. Inspect the repo as needed, then produce a COMPLETE solution "
    "as a single git-apply-able unified diff. Output EXACTLY these two sections and nothing "
    "after the diff:\n"
    "### SELF-CHECK\n"
    "<bullets: what you changed and why it is correct, edge cases considered, and the exact "
    "test/lint/build commands that SHOULD be run to verify it>\n"
    "### DIFF\n"
    "```diff\n"
    "<unified diff, paths relative to the repo root, applies cleanly with `git apply`>\n"
    "```\n"
)

_WRITE_CONTRACT = (
    "You are Codex, implementing a task delegated by Claude Code. You MAY edit files in this "
    "workspace. Implement the task fully, then RUN the project's tests/lint to verify your work "
    "and fix until they pass. Do NOT commit or push. End with a SELF-CHECK section:\n"
    "### SELF-CHECK\n"
    "<files changed; commands you ran and their pass/fail results; any caveats or risks>\n"
)

_SCOUT_CONTRACT = (
    "You are Codex, SCOUTING a task for Claude Code before implementation. You are READ-ONLY: "
    "inspect the repo (read files, run read-only commands) and return a short PLAN. Do NOT write "
    "code, edits, or a diff. Output EXACTLY this section, ~25 lines max:\n"
    "### PLAN\n"
    "<(1) the files/symbols you will change; (2) the approach as a short numbered list of steps; "
    "(3) risks, unknowns, or questions that need the caller's decision>\n"
)


_DISCIPLINE = (
    " Work with discovery discipline either way: when the TASK names files, functions, or "
    "line numbers, open those FIRST and trust them; prefer targeted searches over repo-wide "
    "exploration; leave time to implement and verify. A verified partial result beats an "
    "unfinished perfect one."
)


def _build_delegate_prompt(
    task: str,
    *,
    write: bool,
    feedback: str | None,
    round_no: int,
    scout: bool = False,
    budget_minutes: int | None = None,
    idle_minutes: int | None = None,
    repo_map: str | None = None,
) -> str:
    """Compose the Codex prompt: contract + time budget + repo map + task (+ feedback)."""
    header = _SCOUT_CONTRACT if scout else (_WRITE_CONTRACT if write else _PATCH_CONTRACT)
    parts = [header]
    idle_part = (
        f" The run IS killed after ~{idle_minutes} minutes with NO output at all."
        if idle_minutes
        else ""
    )
    if budget_minutes:
        parts += [
            f"\nTIME BUDGET: ~{budget_minutes} minutes of wall-clock for this ENTIRE run, "
            "enforced externally — overrunning yields NOTHING, not a partial result."
            + idle_part
            + _DISCIPLINE
            + "\n",
        ]
    elif idle_minutes:
        parts += [
            "\nTIME: no hard wall-clock limit on this run — take the time the task needs."
            + idle_part
            + _DISCIPLINE
            + "\n",
        ]
    if repo_map:
        parts += ["\n---\n", repo_map.strip(), "\n"]
    parts += ["\n---\nTASK:\n", task.strip(), "\n"]
    if feedback and feedback.strip():
        parts += [
            f"\n--- REVISION (round {round_no}). Claude reviewed your previous attempt and it "
            "did NOT pass. Address every point concretely:\n",
            feedback.strip(),
            "\n",
        ]
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Concurrency cap — a cross-process flock semaphore so a wide fan-out of
# `delegate` calls runs at most N codex runs at once (uncapped concurrency
# previously thrashed CPU/API and caused Codex timeouts).
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Codex usage / rate-limit awareness (read-only; vendored, mirrors ccc usage.py)
# --------------------------------------------------------------------------- #
def pinned_codex_home(cfg: dict | None = None, today: date | None = None) -> Path | None:
    """The account pin from the shared config, or ``None`` when there is none / it expired.

    ``codex_home`` names a second ``CODEX_HOME`` (another ChatGPT login, e.g.
    ``~/.codex-private``); ``codex_home_until`` is an ISO date, INCLUSIVE, after which the
    pin lapses on its own — "use the private seat until Monday" needs no follow-up edit.
    An unparsable date is treated as expired (fail towards the default seat).
    """
    cfg = load_config() if cfg is None else cfg
    raw = cfg.get("codex_home")
    if not raw:
        return None
    until = cfg.get("codex_home_until")
    if until:
        try:
            if date.fromisoformat(str(until)) < (today or date.today()):
                return None
        except ValueError:
            return None
    return Path(str(raw)).expanduser()


def _codex_home() -> Path:
    """Codex state dir: ``$CODEX_HOME`` → the quota selector → pin → ``~/.codex``.

    Every usage/headroom/refusal reader and ``delegate``'s exec env go through here, so
    the answer moves ALL Codex use to that seat at once. The selector
    (:func:`command_center.quota.select_codex_account`) honours administrative holds
    and blocked verdicts FIRST and the account pin only among eligible seats — the pin
    picks a seat, it does not override "do not use this seat". When NOTHING is
    eligible the pin-else-default order still answers (with a stderr warning): a
    debate the user explicitly requested must run somewhere. Any selector failure
    degrades to the pre-selector behaviour (pin-else-default) rather than raising —
    this function sits on render paths.
    """
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)
    pinned = pinned_codex_home()
    fallback = pinned if pinned is not None else Path.home() / ".codex"
    try:
        from . import quota  # local: quota imports this module's pin reader

        homes = quota._canonical_codex_homes()  # noqa: SLF001
        rows = quota._codex_quotas(  # noqa: SLF001
            int(time.time()), quota.read_cooldowns()
        )
        selected = quota.select_codex_account(rows, quota._codex_pin_label(homes))  # noqa: SLF001
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return fallback
    if not selected:
        print(
            f"⚠️  no Codex seat is eligible (holds/blocks) — falling back to {fallback}",
            file=sys.stderr,
        )
        return fallback
    label = selected.partition(":")[2] or "default"
    return homes.get(label, fallback)


def codex_exec_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (default ``os.environ``) plus ``CODEX_HOME`` pointing at :func:`_codex_home`.

    An explicit ``$CODEX_HOME`` in the caller's env always wins; otherwise the pinned
    home (or the default) is made explicit so the child ``codex`` bills the right seat.
    """
    env = dict(os.environ if base is None else base)
    env.setdefault("CODEX_HOME", str(_codex_home()))
    return env


@dataclass(frozen=True)
class _CodexRateWindow:
    """One duration-identified Codex quota window."""

    used_percent: float
    resets_at: int
    window_minutes: int


@dataclass(frozen=True)
class _CodexRateSnapshot:
    """The newest rollout rate-limit event and all duration-keyed windows it carries."""

    captured_at: int
    windows: dict[int, _CodexRateWindow]
    malformed: bool = False


def _parse_codex_window(raw: object) -> _CodexRateWindow | None:
    """Parse a window, retaining the duration that identifies its quota bucket."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percent")
    resets = raw.get("resets_at")
    minutes = raw.get("window_minutes")
    if pct is None or resets is None or minutes is None:
        return None
    try:
        parsed = _CodexRateWindow(float(pct), int(resets), int(minutes))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.window_minutes > 0 else None


def _dig_rate_limits(obj: object) -> dict | None:
    """Extract the ``rate_limits`` dict from a rollout line (top-level or under payload)."""
    if not isinstance(obj, dict):
        return None
    for cand in (obj.get("rate_limits"), (obj.get("payload") or {}).get("rate_limits")):
        if isinstance(cand, dict):
            return cand
    return None


def _event_timestamp(obj: object, fallback: int) -> int:
    """Return a rollout event's epoch timestamp, falling back to its file mtime."""
    if not isinstance(obj, dict):
        return fallback
    raw = obj.get("timestamp")
    try:
        if isinstance(raw, (int, float)):
            value = float(raw)
            return int(value / 1000 if value > 10**12 else value)
        if isinstance(raw, str):
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return int(parsed.timestamp())
    except (OverflowError, ValueError):
        pass
    return fallback


def _windows_by_duration(rate_limits: dict) -> tuple[dict[int, _CodexRateWindow], bool]:
    """Parse primary/secondary without assigning either one a semantic position."""
    windows: dict[int, _CodexRateWindow] = {}
    malformed = False
    for slot in ("primary", "secondary"):
        raw = rate_limits.get(slot)
        if raw is None:
            continue
        window = _parse_codex_window(raw)
        if window is None or window.window_minutes in windows:
            malformed = True
            continue
        windows[window.window_minutes] = window
    return windows, malformed


def _latest_rate_limits_event(path: Path) -> _CodexRateSnapshot | None:
    """Newest non-windowless ``rate_limits`` event in a rollout JSONL.

    Skips windowless ``premium`` blocks (both windows null) that short ``codex exec``
    runs log. A malformed non-null window is returned as such so safety decisions can
    fail closed instead of silently falling back to an older, apparently healthy event.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        fallback = int(path.stat().st_mtime)
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rate_limits = _dig_rate_limits(obj)
        if rate_limits is None:
            continue
        windows, malformed = _windows_by_duration(rate_limits)
        if windows or malformed:
            return _CodexRateSnapshot(
                captured_at=_event_timestamp(obj, fallback),
                windows=windows,
                malformed=malformed,
            )
    return None


@dataclass(frozen=True)
class _CodexRefusal:
    """A recorded refusal: Codex declined the call for a reason that is not a window."""

    captured_at: int
    reached_type: str  # raw ``rate_limit_reached_type``, e.g. workspace_owner_credits_depleted


# Human labels for the refusal codes seen in the wild; anything else is de-snaked as-is.
#
# ``workspace_owner_credits_depleted`` is OpenAI's wording and it misleads: the cause is
# almost never "you need to buy credits". Traced end-to-end on 2026-08-28/29 — the 5h
# window read 81% at 23:18, the next call exhausted it, and because the plan's INCLUDED
# allowance is what ran out, Codex tried the workspace credit pool as an overflow. That
# pool is empty (``has_credits: false``) on a normal seat that has never bought credits,
# so the refusal is reported in credit terms. Access came back by itself at 02:57, the
# moment the 5h window reset, with the credit balance still zero. So: this is a rate
# limit, and the fix is to wait for the reset, not to top up.
_REFUSAL_LABELS = {
    "workspace_owner_credits_depleted": "included usage limit reached (no credit overflow)",
}
# Card-sized wording for the same codes. The TUI's Codex card is 34 columns wide, so the
# explanatory label above wraps to three lines there and pushes the bars down; the CLI and
# `headroom`, which have a whole terminal width, keep the long form.
_REFUSAL_LABELS_SHORT = {
    "workspace_owner_credits_depleted": "usage limit reached",
}
# Snapshots persist the ALREADY-EXPANDED long label (there is no reached_type on the cached
# record), so the card shortens by looking the long form back up.
_SHORT_BY_LABEL = {
    _REFUSAL_LABELS[code]: short
    for code, short in _REFUSAL_LABELS_SHORT.items()
    if code in _REFUSAL_LABELS
}


def refusal_label(reached_type: str) -> str:
    """Human-readable form of a ``rate_limit_reached_type`` code."""
    return _REFUSAL_LABELS.get(reached_type, reached_type.replace("_", " "))


def short_refusal_label(label: str) -> str:
    """Card-sized form of an already-expanded refusal *label*; unknown ones pass through."""
    return _SHORT_BY_LABEL.get(label, label)


def _latest_limit_block(path: Path) -> _CodexRefusal | None:
    """Newest ``rate_limits`` block in a rollout JSONL — windowless ones INCLUDED.

    Deliberately the mirror of :func:`_latest_rate_limits_event`, which SKIPS the
    windowless ``premium`` blocks. Those are exactly where a hard refusal is recorded
    (``rate_limit_reached_type``), so a reader that only wants windows never sees it and
    happily reports the last healthy percentages while every call is being rejected.
    ``reached_type`` is empty when the newest block records no refusal.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        fallback = int(path.stat().st_mtime)
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        rate_limits = _dig_rate_limits(obj)
        if rate_limits is None:
            continue
        raw = rate_limits.get("rate_limit_reached_type")
        return _CodexRefusal(
            captured_at=_event_timestamp(obj, fallback),
            reached_type=raw.strip() if isinstance(raw, str) else "",
        )
    return None


def codex_refusal(home: Path | None = None) -> _CodexRefusal | None:
    """The live refusal recorded in *home*'s rollouts (default: the effective home).

    Only the NEWEST block across recent rollout files decides: an older refusal that a
    later successful call superseded must never keep the seat marked blocked.

    *home* matters for attribution: each seat's rollouts carry that seat's refusals,
    and reading "the effective home" while labelling the result "the team seat" is
    exactly the misattribution ``quota``'s per-seat rows exist to avoid — callers
    building per-seat rows MUST pass the row's own home.

    Deliberately NOT keyed on ``credits.has_credits``. That field is ``false`` on a
    perfectly healthy plan-covered seat — verified 2026-08-28 against sessions that
    completed normally minutes before the block appeared — so it says nothing about
    whether calls are being refused. ``rate_limit_reached_type`` is ``null`` while
    healthy and carries the reason once refusals start.
    """
    sessions = (home if home is not None else _codex_home()) / "sessions"
    try:
        files = sorted(
            sessions.glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    blocks = [
        block
        for path in files[:_CODEX_SCAN_LIMIT]
        if (block := _latest_limit_block(path)) is not None
    ]
    newest = max(blocks, key=lambda item: item.captured_at, default=None)
    return newest if newest is not None and newest.reached_type else None


def _codex_rate_snapshot() -> _CodexRateSnapshot | None:
    """Newest duration-keyed Codex quota event across recent rollout files."""
    sessions = _codex_home() / "sessions"
    try:
        files = sorted(
            sessions.glob("**/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    snapshots = [
        snapshot
        for path in files[:_CODEX_SCAN_LIMIT]
        if (snapshot := _latest_rate_limits_event(path)) is not None
    ]
    return max(snapshots, key=lambda item: item.captured_at, default=None)


def codex_cost_history_path() -> Path:
    """Per-run Codex cost log, beside this tool's existing config by default."""
    override = os.environ.get("CODEX_IN_CLAUDE_COST_LOG")
    if override:
        return Path(override).expanduser()
    return config_path().with_name("cost-history.jsonl")


def codex_cost_snapshot() -> dict[str, dict[str, float | int]]:
    """Serializable duration-keyed quota snapshot for cost instrumentation.

    This public seam lets an external debate helper capture ``before`` and ``after``
    around its own ``codex exec`` and pass both to :func:`record_codex_run`.
    """
    snapshot = _codex_rate_snapshot()
    if snapshot is None:
        return {}
    return {
        str(minutes): {
            "used_percent": window.used_percent,
            "resets_at": window.resets_at,
        }
        for minutes, window in sorted(snapshot.windows.items())
    }


def _history_row_timestamp(row: object) -> float | None:
    """Finite epoch timestamp from one cost-history row, or None."""
    if not isinstance(row, dict):
        return None
    try:
        value = float(row["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def record_codex_run(
    *,
    purpose: str,
    model: str,
    effort: str,
    before: dict[str, dict[str, float | int]],
    after: dict[str, dict[str, float | int]],
    ts: int | None = None,
    path: Path | None = None,
) -> bool:
    """Record one Codex run and prune history older than 90 days.

    Writes are serialized across processes and are deliberately best-effort: cost
    telemetry must never change a delegate run's result or stdout contract.
    """
    now = int(time.time()) if ts is None else ts
    target = path or codex_cost_history_path()
    lock_path = target.with_suffix(target.suffix + ".lock")
    row = {
        "ts": now,
        "purpose": purpose,
        "model": model,
        "effort": effort,
        "before": before,
        "after": after,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            retained: list[str] = []
            cutoff = now - _COST_HISTORY_SECONDS
            try:
                old_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            except FileNotFoundError:
                old_lines = []
            for line in old_lines:
                try:
                    old_row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                old_ts = _history_row_timestamp(old_row)
                if old_ts is not None and old_ts >= cutoff:
                    retained.append(json.dumps(old_row, separators=(",", ":")))
            retained.append(json.dumps(row, separators=(",", ":")))
            with tempfile.NamedTemporaryFile(
                "w",
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                handle.write("\n".join(retained) + "\n")
                tmp = Path(handle.name)
            tmp.replace(target)
        return True
    except OSError:
        return False


def _debate_cost_deltas(window_minutes: int, *, now: int | None = None) -> list[float]:
    """Valid debate-run usage deltas for one duration from the retained history."""
    now_ts = int(time.time()) if now is None else now
    try:
        lines = codex_cost_history_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    key = str(window_minutes)
    deltas: list[float] = []
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        row_ts = _history_row_timestamp(row)
        if (
            not isinstance(row, dict)
            or row.get("purpose") != "debate"
            or row_ts is None
            or row_ts < now_ts - _COST_HISTORY_SECONDS
        ):
            continue
        before = row.get("before")
        after = row.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        before_window = before.get(key)
        after_window = after.get(key)
        if not isinstance(before_window, dict) or not isinstance(after_window, dict):
            continue
        try:
            before_pct = float(before_window["used_percent"])
            after_pct = float(after_window["used_percent"])
            before_reset = int(before_window["resets_at"])
            after_reset = int(after_window["resets_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (before_pct, after_pct)):
            continue
        # A changed reset boundary means the run crossed a reset, even if the new
        # window happened to climb back above the old used percentage.
        if after_reset != before_reset or after_pct < before_pct:
            continue
        deltas.append(after_pct - before_pct)
    return deltas


def _codex_usage_windows(now: int | None = None) -> dict[str, tuple[float, int] | None]:
    """Live 5h/weekly windows from the newest usable rollout block.

    ``{"five_hour": (pct, resets)|None, "seven_day": (pct, resets)|None}``. A window
    whose ``resets_at <= now`` is STALE (its reset already passed) and reported None —
    else the gate would never reopen after a reset. All-None => usage unknown.
    """
    now_ts = int(time.time()) if now is None else now
    snapshot = _codex_rate_snapshot()
    if snapshot is None:
        return {"five_hour": None, "seven_day": None}

    def _live(minutes: int) -> tuple[float, int] | None:
        win = snapshot.windows.get(minutes)
        if win is None or win.resets_at <= now_ts:
            return None
        return (win.used_percent, win.resets_at)

    return {
        "five_hour": _live(_FIVE_HOUR_MINUTES),
        "seven_day": _live(_SEVEN_DAY_MINUTES),
    }


def read_codex_usage(now: int | None = None) -> tuple[float | None, int | None]:
    """``(used_percent, resets_at)`` of the most-consumed live window, or ``(None, None)``."""
    windows = _codex_usage_windows(now)
    live = [w for w in (windows["five_hour"], windows["seven_day"]) if w is not None]
    if not live:
        return (None, None)
    return max(live, key=lambda w: w[0])


def _format_reset(resets_at: int, now: int | None = None) -> str:
    """Relative reset, minute precision: ``in 4h 5m`` / ``in 3d 2h 4m`` / ``now``."""
    now = int(time.time()) if now is None else now
    secs = int(resets_at) - now
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return "in " + " ".join(parts)


def _headroom_reserve(window_minutes: int) -> tuple[float, str]:
    """Reserve percentage and its source for one duration."""
    deltas = sorted(_debate_cost_deltas(window_minutes))
    if len(deltas) < _HEADROOM_MIN_SAMPLES:
        return (_HEADROOM_BOOTSTRAP_RESERVE_PERCENT, "bootstrap")
    # Nearest-rank P95: the smallest observed value at or above the 95th percentile.
    p95 = deltas[math.ceil(0.95 * len(deltas)) - 1]
    learned = 3.0 * p95 * 1.10
    return (
        min(_HEADROOM_MAX_RESERVE_PERCENT, max(_HEADROOM_MIN_RESERVE_PERCENT, learned)),
        "learned",
    )


def headroom_reserve_percent(window_minutes: int) -> float:
    """Quota to hold back: learned debate cost after 10 samples, else 35%."""
    return _headroom_reserve(window_minutes)[0]


def _format_window_duration(window_minutes: int) -> str:
    """Compact human label for an arbitrary rate-limit duration."""
    if window_minutes % (24 * 60) == 0:
        return f"{window_minutes // (24 * 60)}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


def codex_headroom(now: int | None = None) -> dict[str, Any]:
    """Structured quota-reserve decision for optional Codex offloads.

    Missing, malformed, or older-than-six-hours quota data fails closed. This policy
    governs optional offload work only; debate callers intentionally remain always allowed.
    """
    now_ts = int(time.time()) if now is None else now
    snapshot = _codex_rate_snapshot()
    rows: list[dict[str, Any]] = []
    if snapshot is not None:
        for minutes, window in sorted(snapshot.windows.items()):
            reserve, reserve_source = _headroom_reserve(minutes)
            remaining = 100.0 - window.used_percent
            reset_fresh = window.resets_at <= now_ts + _HEADROOM_RESET_FRESH_SECONDS
            rows.append(
                {
                    "duration": _format_window_duration(minutes),
                    "window_minutes": minutes,
                    "used_percent": window.used_percent,
                    "remaining_percent": remaining,
                    "resets_at": window.resets_at,
                    "resets_in_seconds": window.resets_at - now_ts,
                    "reserve_percent": reserve,
                    "reserve_source": reserve_source,
                    "reset_fresh": reset_fresh,
                    "verdict": "allowed" if reset_fresh or remaining >= reserve else "reserve",
                }
            )

    state: str
    reason: str
    # A recorded refusal outranks every window reading: the windows can show ample
    # headroom (they are a *plan allowance*, orthogonal to the workspace credit balance)
    # while Codex rejects every call. Checked first so the gate cannot go ALLOWED the
    # moment a window happens to roll over.
    refusal = codex_refusal()
    if refusal is not None:
        state, reason = "blocked", f"Codex is refusing calls: {refusal_label(refusal.reached_type)}"
    elif snapshot is None:
        state, reason = "unknown", "no usable rate_limits event"
    elif snapshot.malformed:
        state, reason = "unknown", "rate_limits event contains a malformed window"
    elif not rows:
        state, reason = "unknown", "rate_limits event has no usable windows"
    elif now_ts - snapshot.captured_at > _HEADROOM_STALE_AFTER_SECONDS:
        state, reason = "unknown", "newest rate_limits event is older than 6h"
    elif any(row["verdict"] == "reserve" for row in rows):
        state, reason = "reserve", "at least one live window is inside its reserve"
    else:
        state, reason = "allowed", "every live window has sufficient headroom"

    if state in ("unknown", "blocked"):
        for row in rows:
            row["verdict"] = state
    return {
        "state": state,
        "offload_allowed": state == "allowed",
        "reason": reason,
        "refused_by": refusal.reached_type if refusal is not None else "",
        "refused_at": refusal.captured_at if refusal is not None else None,
        "captured_at": snapshot.captured_at if snapshot is not None else None,
        "stale_after_seconds": _HEADROOM_STALE_AFTER_SECONDS,
        "reset_fresh_within_seconds": _HEADROOM_RESET_FRESH_SECONDS,
        "windows": rows,
    }


def _usage_tier_cap(ceiling: int, now: int | None = None) -> int:
    """Usage-tapered concurrency cap within ``[1, ceiling]``.

    <50% used -> 3, 50-75% -> 2, >75% -> 1 (of whichever window is most consumed);
    unknown usage -> ceiling (fail open). Never 0 — the >=100% case is the caller's
    preflight (EX_QUOTA), not the slot loop.
    """
    pct, _ = read_codex_usage(now)
    if pct is None:
        return ceiling
    if pct < 50.0:
        tier = 3
    elif pct <= 75.0:
        tier = 2
    else:
        tier = 1
    return max(1, min(tier, ceiling))


def _max_concurrent(flag_value: int | None) -> int:
    """Resolve the cap: -j flag -> $CODEX_IN_CLAUDE_MAX_CONCURRENT -> default 3.

    A value <= 0 disables gating (unlimited).
    """
    if flag_value is not None:
        return flag_value
    env = os.environ.get("CODEX_IN_CLAUDE_MAX_CONCURRENT")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            print(
                f"WARN: ignoring non-integer CODEX_IN_CLAUDE_MAX_CONCURRENT={env!r}.",
                file=sys.stderr,
            )
    return DEFAULT_MAX_CONCURRENT


@contextlib.contextmanager
def _concurrency_slot(ceiling: int, poll: float = 3.0) -> Iterator[None]:
    """Hold a cross-process slot for the body, capping concurrent codex runs.

    The pool is *ceiling* flock files, but the effective cap is **usage-tapered**
    within ``[1, ceiling]`` and recomputed every poll: <50% used -> 3, 50-75% -> 2,
    >75% -> 1 (see :func:`_usage_tier_cap`); unknown usage -> ceiling. Only the first
    ``cap`` slots are ever tried, so as usage climbs fewer *new* runs are admitted
    while in-flight ones finish (fewer sessions to reload after a reset). A slot
    auto-releases if its holder dies (the OS drops the flock). ``ceiling <= 0``
    disables gating. Fail-open: any setup error runs ungated.
    """
    if ceiling <= 0:
        yield
        return
    try:
        SLOT_DIR.mkdir(parents=True, exist_ok=True)
        handles: list[TextIO] = [
            open(SLOT_DIR / f"slot{i}.lock", "w", encoding="utf-8") for i in range(ceiling)
        ]
    except OSError as exc:
        print(f"… concurrency gate disabled ({exc}); running ungated.", file=sys.stderr)
        yield
        return
    held: TextIO | None = None
    try:
        ticks = 0
        while held is None:
            cap = _usage_tier_cap(ceiling)  # usage-tapered, recomputed each poll
            for handle in handles[:cap]:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = handle
                    break
                except OSError:
                    continue
            if held is None:
                if ticks % 10 == 0:  # heartbeat every ~30s so a waiting task looks alive
                    print(
                        f"… waiting for a Codex slot (usage-tapered cap {cap}/{ceiling})…",
                        file=sys.stderr,
                        flush=True,
                    )
                ticks += 1
                time.sleep(poll)
        yield
    finally:
        if held is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        for handle in handles:
            handle.close()


def cmd_runs(args: argparse.Namespace) -> int:
    """List in-flight delegate runs from their heartbeat files (one cheap read each).

    The supervised runner refreshes a tiny JSON per run every ~5s and removes it
    on exit, so this shows elapsed/idle time, output volume, and the last output
    line of every live delegate WITHOUT reading any transcript. Heartbeats whose
    process is gone (crash, SIGKILL) are cleaned up on sight.
    """
    rows: list[dict[str, Any]] = []
    for path in sorted(RUNS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        try:
            os.kill(pid, 0)  # liveness probe only
        except ProcessLookupError:
            with contextlib.suppress(OSError):
                path.unlink()  # stale heartbeat from a killed run
            continue
        except PermissionError:
            pass
        rows.append(data)
    if args.json:
        print(json.dumps(rows))
        return EX_OK
    if not rows:
        print("no active delegate runs.")
        return EX_OK
    for data in rows:
        elapsed, idle = int(data.get("elapsed_s", 0)), int(data.get("idle_s", 0))
        print(
            f"pid {data['pid']} · {data.get('model', '?')}/{data.get('effort', '?')}"
            f"{' · write' if data.get('write') else ''} · {data.get('repo', '?')} · "
            f"elapsed {elapsed // 60}m{elapsed % 60:02d}s · idle {idle}s · "
            f"{data.get('lines', 0)} lines · last: {data.get('last_line', '')!r}"
        )
    return EX_OK


def cmd_usage(args: argparse.Namespace) -> int:
    """Print the current Codex rate-limit usage (5h + weekly windows)."""
    windows = _codex_usage_windows()
    if args.json:
        payload = {
            key: (
                {
                    "used_percent": win[0],
                    "resets_at": win[1],
                    "window_minutes": minutes,
                }
                if win is not None
                else None
            )
            for key, minutes, win in (
                ("five_hour", _FIVE_HOUR_MINUTES, windows["five_hour"]),
                ("seven_day", _SEVEN_DAY_MINUTES, windows["seven_day"]),
            )
        }
        print(json.dumps(payload))
        return EX_OK
    parts = []
    for label, key in (("5h", "five_hour"), ("weekly", "seven_day")):
        win = windows[key]
        parts.append(
            f"{label} —"
            if win is None
            else f"{label} {win[0]:.0f}% (resets {_format_reset(win[1])})"
        )
    print("codex usage: " + "  ·  ".join(parts))
    return EX_OK


def cmd_headroom(args: argparse.Namespace) -> int:
    """Report whether optional Codex offloads can spend quota beyond the reserve."""
    decision = codex_headroom()
    if args.json:
        print(json.dumps(decision))
    else:
        for window in decision["windows"]:
            print(
                f"{window['duration']}: {window['used_percent']:.0f}% used, "
                f"resets {_format_reset(window['resets_at'])}, "
                f"reserve {window['reserve_percent']:.0f}% "
                f"(reserve_source {window['reserve_source']}), "
                f"{str(window['verdict']).upper()}"
            )
        if decision["state"] == "allowed":
            print("offload: ALLOWED")
        elif decision["state"] == "blocked":
            print(f"offload: DENIED (blocked — {decision['reason']})")
        elif decision["state"] == "reserve":
            print(f"offload: DENIED (reserve zone — {decision['reason']})")
        else:
            print(f"offload: DENIED (unknown — {decision['reason']})")
    # "blocked" shares the reserve exit code: the documented contract is 0 = offload
    # allowed, non-zero = keep the work on Claude, and 3 already means "no usable data".
    return {"allowed": 0, "reserve": 1, "blocked": 1, "unknown": 3}[decision["state"]]


def cmd_delegate(args: argparse.Namespace) -> int:
    """Run one Codex round. First stdout line = the model; then Codex's reply."""
    if not args.prompt.strip():
        print("ERROR: empty prompt.", file=sys.stderr)
        return EX_USAGE
    model = args.model or resolve_model("delegate-review")
    if args.model and not valid_slug(args.model):
        print(f"ERROR: unknown model '{args.model}'.", file=sys.stderr)
        return EX_INVALID_MODEL
    effort = args.effort or resolve_effort()  # None -> let codex use the model's default
    shown_effort = effort or effort_of(model)
    # The guaranteed first line — captured/printed by us, never Claude preamble.
    print(f"model: {model} (effort {shown_effort})", flush=True)

    # Quota preflight: skip fast (never launch codex) when a live rate-limit window is
    # exhausted. Fail-open on unknown usage; bypass with -Q / $CODEX_IN_CLAUDE_IGNORE_QUOTA.
    ignore_quota = getattr(args, "ignore_quota", False)
    if not (ignore_quota or os.environ.get("CODEX_IN_CLAUDE_IGNORE_QUOTA") == "1"):
        used_pct, resets_at = read_codex_usage()
        if used_pct is not None and used_pct >= 100.0:
            when = f" (resets {_format_reset(resets_at)})" if resets_at else ""
            print(
                f"ERROR: Codex quota exhausted — {used_pct:.0f}% used{when}. "
                "Skipping; retry after reset.",
                file=sys.stderr,
            )
            return EX_QUOTA

    write = args.write and not args.scout  # scouting is always read-only (plan, no edits)
    resume = getattr(args, "resume", None)
    # ONE launch policy for every `codex exec` (see command_center.codex_launch): the
    # binary, the validated `-C` root, and the NAMED permission profile. Never `-s`/
    # `--sandbox` — that forces Codex's legacy sandbox and silently drops the profile's
    # deny rules (credential stores, workspace .env/*.pem, the network block).
    try:
        codex_bin = codex_launch.resolve_codex()
        resume_record = codex_launch.resolve_resume(str(resume), write=write) if resume else None
        workdir = codex_launch.resolve_workdir(
            resume_record.resolved_cwd if resume_record is not None else args.cwd,
            write=write,
        )
        perm_args = codex_launch.permission_args(write)
    except codex_launch.CodexMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EX_NO_CODEX
    except codex_launch.CodexLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EX_USAGE
    run_cwd = str(workdir)
    wall_timeout = _effective_timeout(args.timeout, shown_effort)
    idle_flag = getattr(args, "idle_timeout", None)
    if idle_flag is not None:
        idle_timeout = max(0, idle_flag)
    elif wall_timeout:
        idle_timeout = min(DEFAULT_IDLE_TIMEOUT, wall_timeout)
    else:
        idle_timeout = DEFAULT_IDLE_TIMEOUT  # unlimited wall still guards stalls
    repo_map = (
        None
        if (resume or getattr(args, "no_repo_map", False))
        else _repo_map(run_cwd, explicit=getattr(args, "repo_map", None))
    )
    # Advisory only: pointered tasks rarely need xhigh's exploration depth.
    if (
        args.effort is None
        and shown_effort == "xhigh"
        and re.search(r"\.\w{1,5}:\d+|\blines? \d+", args.prompt)
    ):
        print(
            "hint: the task contains file:line pointers — xhigh mostly buys exploration "
            "depth those pointers make unnecessary; consider -e high.",
            file=sys.stderr,
        )
    prompt = _build_delegate_prompt(
        args.prompt,
        write=write,
        feedback=args.feedback,
        round_no=args.round,
        scout=args.scout,
        budget_minutes=(max(1, wall_timeout // 60) if wall_timeout else None),
        idle_minutes=(max(1, idle_timeout // 60) if idle_timeout else None),
        repo_map=repo_map,
    )
    env = codex_exec_env()  # CODEX_HOME made explicit: env → config pin → ~/.codex
    env["CCC_NO_CODEX"] = "1"  # never re-trigger the plan/k8s automation
    env["CCC_INTERNAL"] = "1"
    env["AI_NO_AUTOCOMMIT"] = "1"  # never let a nested run auto-commit

    with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as handle:
        out_path = handle.name
    if resume_record is not None:
        # Resume a prior round's session: codex re-attaches with its discovered context
        # intact. `codex exec resume` has no -C, so the workspace root is INHERITED from
        # the original session — which is exactly why resolve_resume re-validated the
        # journalled root (and the read/write mode) before we got here. `last` was
        # already resolved to a concrete id, so the resumed session is unambiguous.
        cmd = [codex_bin, "exec", "resume", resume_record.session_id, "-o", out_path]
        cmd += [*perm_args, "-m", model]
    else:
        cmd = [codex_bin, "exec", *perm_args, "-o", out_path, "-m", model, "-C", run_cwd]
    if workdir.skip_git_check:
        cmd.append("--skip-git-repo-check")  # only where the root really is not a repo
    if effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]
    cmd.append(prompt)

    if getattr(args, "show_prompt", False):
        # Dry run: show exactly what codex WOULD receive, then stop. Lets a
        # caller sanity-check the assembled contract/budget/repo-map cheaply.
        with contextlib.suppress(OSError):
            os.unlink(out_path)
        print("### DRY RUN (no codex launched)")
        print("command: " + " ".join(cmd[:-1]) + " <PROMPT>")
        print("--- PROMPT ---")
        print(prompt)
        return EX_OK

    heartbeat = RUNS_DIR / f"{os.getpid()}.json"
    heartbeat_meta = {
        "model": model,
        "effort": shown_effort,
        "repo": run_cwd,
        "write": write,
    }
    purpose = getattr(args, "purpose", "delegate")
    try:
        # Take one concurrency slot before launching codex; the rest of a fan-out waits.
        with _concurrency_slot(_max_concurrent(args.max_concurrent)):
            # When Codex may write, snapshot the worktree so the caller reviews ONLY its diff.
            before = _git_status(run_cwd) if write else None
            cost_before = codex_cost_snapshot()
            try:
                try:
                    proc = _exec_codex(
                        cmd,
                        env=env,
                        timeout=wall_timeout,
                        idle_timeout=idle_timeout,
                        heartbeat_path=heartbeat,
                        heartbeat_meta=heartbeat_meta,
                    )
                finally:
                    record_codex_run(
                        purpose=purpose,
                        model=model,
                        effort=shown_effort,
                        before=cost_before,
                        after=codex_cost_snapshot(),
                    )
            except FileNotFoundError:
                print("ERROR: `codex` CLI not found on PATH.", file=sys.stderr)
                return EX_NO_CODEX
            except subprocess.TimeoutExpired as exc:
                stderr_text = str(exc.stderr or "")
                tail = _stderr_tail(stderr_text)
                session = _session_id_of(stderr_text)
                resume_hint = f" Resume its context with --resume {session}." if session else ""
                print(
                    f"ERROR: Codex hit the {wall_timeout}s wall timeout (whole process tree "
                    "killed). Retry with a larger -t (or -t 0), a tighter task, or lower "
                    "effort." + resume_hint + (f" Last output:\n{tail}" if tail else ""),
                    file=sys.stderr,
                )
                return EX_TIMEOUT
            except CodexStalledError as exc:
                tail = _stderr_tail(exc.stderr_text)
                session = _session_id_of(exc.stderr_text)
                resume_hint = f" Resume its context with --resume {session}." if session else ""
                print(
                    f"ERROR: Codex stalled — no output for {exc.idle_seconds}s (whole process "
                    "tree killed). Likely a hung network/CLI; retry, or tune --idle-timeout."
                    + resume_hint
                    + (f" Last output:\n{tail}" if tail else ""),
                    file=sys.stderr,
                )
                return EX_TIMEOUT
        try:
            reply = Path(out_path).read_text(encoding="utf-8").strip()
        except OSError:
            reply = ""
    finally:
        with contextlib.suppress(OSError):
            os.unlink(out_path)

    if not reply and proc.returncode != 0:
        print(f"ERROR: Codex exited {proc.returncode}:\n{proc.stderr.strip()}", file=sys.stderr)
        return EX_CODEX_FAIL
    print(reply or proc.stdout.strip())
    session = _session_id_of(proc.stderr or "")
    if session:
        # Journal the launch (CODEX_HOME/ccc-sessions.jsonl, 0600) BEFORE reporting it:
        # `--resume <id>` is only honoured for sessions this policy actually started, and
        # it re-checks the recorded root + read/write mode against today's rules.
        codex_launch.record_launch(session, workdir, write=write)
        print(f"\n### SESSION\n{session}")
    if write:
        after = _git_status(run_cwd)
        changed = sorted(set(after) - set(before or []))
        if changed:
            print("\n### CODEX-WROTE (review this diff)\n" + "\n".join(changed))
    return EX_OK


def _git_status(cwd: str | None) -> list[str]:
    """`git status --porcelain` lines for *cwd* (empty on any error)."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser (every flag has a short form)."""
    parser = argparse.ArgumentParser(
        prog="codex-in-claude.py",
        description="Manage Codex model choice for Claude Code commands; delegate tasks to Codex.",
        epilog=(
            "Examples:\n"
            "  codex-in-claude.py models --refresh\n"
            "  codex-in-claude.py pick                     # interactive picker (+ help sync)\n"
            "  codex-in-claude.py set-model gpt-5.6-sol --for all\n"
            "  codex-in-claude.py get-model --for debate\n"
            "  codex-in-claude.py sync-skills --check      # is /codex… help in sync?\n"
            "  codex-in-claude.py headroom --json          # optional-offload quota gate\n"
            "  codex-in-claude.py delegate --write -C . 'add retry to fetch()'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_models = sub.add_parser("models", help="list available Codex models")
    p_models.add_argument(
        "-r", "--refresh", action="store_true", help="refresh via `codex debug models`"
    )
    p_models.add_argument(
        "-H", "--include-hidden", action="store_true", help="include hidden models"
    )
    p_models.set_defaults(func=cmd_models)

    p_get = sub.add_parser("get-model", help="print the model resolved for a command")
    p_get.add_argument(
        "-f",
        "--for",
        dest="for_command",
        choices=COMMANDS,
        default=None,
        help="command (default: global)",
    )
    p_get.set_defaults(func=cmd_get_model)

    p_set = sub.add_parser("set-model", help="set the model for a command (or global default)")
    p_set.add_argument("slug", help="model slug, e.g. gpt-5.6-sol")
    p_set.add_argument(
        "-f",
        "--for",
        dest="for_command",
        choices=(*COMMANDS, "all"),
        default=None,
        help="command (default/all = global; 'all' also clears per-command pins)",
    )
    p_set.set_defaults(func=cmd_set_model)

    p_pick = sub.add_parser("pick", help="interactive model picker (terminal only)")
    p_pick.add_argument(
        "-f",
        "--for",
        dest="for_command",
        choices=(*COMMANDS, "all"),
        default=None,
        help="command to set (default: all)",
    )
    p_pick.add_argument(
        "-r", "--refresh", action="store_true", help="refresh via `codex debug models`"
    )
    p_pick.set_defaults(func=cmd_pick)

    p_sync = sub.add_parser(
        "sync-skills", help="stamp the model into the codex skill/command descriptions"
    )
    p_sync.add_argument(
        "-c",
        "--check",
        action="store_true",
        help="report drift without writing (exit 1 = stale, 2 = unparsable)",
    )
    p_sync.set_defaults(func=cmd_sync_skills)

    p_geteff = sub.add_parser("get-effort", help="print the configured reasoning effort")
    p_geteff.set_defaults(func=cmd_get_effort)

    p_seteff = sub.add_parser("set-effort", help="set the global reasoning effort")
    p_seteff.add_argument(
        "level", choices=(*EFFORTS, "default"), help="reasoning level (default = each model's own)"
    )
    p_seteff.set_defaults(func=cmd_set_effort)

    p_del = sub.add_parser("delegate", help="run one Codex round (prints model first)")
    p_del.add_argument("prompt", help="the task for Codex")
    p_del.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="let Codex edit files (the hardened-rw permission profile; refused when the "
        "active CODEX_HOME config.toml does not define one)",
    )
    p_del.add_argument(
        "-S",
        "--scout",
        action="store_true",
        help="read-only plan only (no diff) — for a pre-implementation scout round",
    )
    p_del.add_argument(
        "-C",
        "--cwd",
        default=None,
        help="repo dir Codex works in (codex -C). Refused for $HOME or any dir above it; "
        "omit it only inside a git work tree",
    )
    p_del.add_argument("-r", "--round", type=int, default=1, help="loop round (1-based)")
    p_del.add_argument(
        "-f", "--feedback", default=None, help="Claude's review feedback for a revision round"
    )
    p_del.add_argument(
        "-m", "--model", default=None, help="override model (else resolved from config)"
    )
    p_del.add_argument(
        "-p",
        "--purpose",
        default="delegate",
        help="cost-instrumentation purpose label (default: delegate; e.g. debate, review)",
    )
    p_del.add_argument(
        "-e",
        "--effort",
        choices=EFFORTS,
        default=None,
        help="override reasoning effort (else config/model default)",
    )
    p_del.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=None,
        help="wall-clock seconds before the run is killed (default scales with effort: "
        "low 600, medium 900, high 1500, xhigh 2700; 0 = no wall limit — the idle "
        "watchdog still guards stalls)",
    )
    p_del.add_argument(
        "-i",
        "--idle-timeout",
        type=int,
        default=None,
        metavar="SECS",
        help="kill the run after this long with NO codex output "
        "(default min(900, wall timeout), or 900 with -t 0; 0 disables the stall watchdog)",
    )
    p_del.add_argument(
        "-R",
        "--resume",
        default=None,
        metavar="SESSION",
        help="resume a previous round's codex session (UUID from the ### SESSION line, "
        "or 'last') — keeps its discovered context; permissions/cwd inherit from that "
        "session, so only ccc-launched sessions with a matching read/write mode are allowed",
    )
    p_del.add_argument(
        "-M",
        "--no-repo-map",
        action="store_true",
        help="do not inject the repo orientation map (repo_scope_short.md or a git "
        "top-level summary) into the prompt",
    )
    p_del.add_argument(
        "-P",
        "--repo-map",
        default=None,
        metavar="FILE",
        help="inject THIS file as the repo orientation map instead of the automatic "
        "repo_scope_short.md / git-summary choice (caller-curated context)",
    )
    p_del.add_argument(
        "-n",
        "--show-prompt",
        action="store_true",
        help="dry run: print the assembled codex command and prompt, launch nothing",
    )
    p_del.add_argument(
        "-j",
        "--max-concurrent",
        type=int,
        default=None,
        metavar="N",
        help="cap simultaneous Codex runs across all delegate processes "
        "(default 3, or $CODEX_IN_CLAUDE_MAX_CONCURRENT; 0 = unlimited)",
    )
    p_del.add_argument(
        "-Q",
        "--ignore-quota",
        action="store_true",
        help="skip the quota preflight (run even when a rate-limit window is exhausted)",
    )
    p_del.set_defaults(func=cmd_delegate)

    p_usage = sub.add_parser("usage", help="show current Codex rate-limit usage (5h + weekly)")
    p_usage.add_argument("-j", "--json", action="store_true", help="machine-readable JSON")
    p_usage.set_defaults(func=cmd_usage)

    p_headroom = sub.add_parser(
        "headroom",
        help="check quota reserve before optional Codex offloads",
        description=(
            "Check whether every live Codex quota window has enough remaining reserve "
            "for optional offload work. Missing/stale data fails closed; debates remain "
            "always allowed and should not use this gate."
        ),
    )
    p_headroom.add_argument("-j", "--json", action="store_true", help="machine-readable JSON")
    p_headroom.set_defaults(func=cmd_headroom)

    p_home = sub.add_parser(
        "home",
        help="show/set/clear the Codex account pin (which CODEX_HOME every Codex call bills)",
        description=(
            "Pin ALL Codex use (delegate, usage/headroom, codex-review.py debates) to a "
            "second ChatGPT login, e.g. ~/.codex-private, optionally until an ISO date "
            "(inclusive). An explicit $CODEX_HOME in the environment still wins."
        ),
    )
    p_home.add_argument("path", nargs="?", help="CODEX_HOME to pin (needs its auth.json)")
    p_home.add_argument("-u", "--until", help="pin expires after this ISO date (inclusive)")
    p_home.add_argument("-c", "--clear", action="store_true", help="remove the pin")
    p_home.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="machine-readable selection: {home, source, label, email, until}",
    )
    p_home.set_defaults(func=cmd_home)

    p_runs = sub.add_parser(
        "runs", help="list in-flight delegate runs (elapsed/idle/last output, one line each)"
    )
    p_runs.add_argument("-j", "--json", action="store_true", help="machine-readable JSON")
    p_runs.set_defaults(func=cmd_runs)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch to the chosen subcommand; returns its exit code."""
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
