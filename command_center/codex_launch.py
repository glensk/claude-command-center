#!/usr/bin/env python3
"""The ONE launch policy for every ``codex exec`` ccc starts (delegate, scout, llm).

Codex ≥ 0.150 replaced the legacy ``sandbox_mode`` key with **named permission
profiles** (``default_permissions = "<name>"`` plus a ``[permissions.<name>]`` table).
The two must never be mixed: passing ``-s/--sandbox`` forces the LEGACY sandbox and
silently DROPS the profile's deny rules (credential stores, ``.env``/``*.pem`` inside
the workspace, the network block). Every ``codex exec`` ccc builds therefore goes
through this module, and this module never emits ``-s``/``--sandbox``.

What it decides, in one place:

* :func:`resolve_codex` — where the ``codex`` binary is (clear error if absent).
* :func:`resolve_workdir` — the ``-C`` workspace root. Always absolute and
  symlink-resolved; **refuses** ``$HOME`` itself and any ancestor of ``$HOME`` (a
  write run rooted there could rewrite the whole account); accepts an IMPLICIT cwd
  (no ``-C``) only when it is inside a git work tree, and an EXPLICIT non-git dir
  only with ``skip_git_check`` set (so ``--skip-git-repo-check`` is passed exactly
  when it is actually needed, instead of unconditionally).
* :func:`permission_args` — ``-c default_permissions="hardened-ro"`` for a read run,
  ``…="hardened-rw"`` for a write run **only when that profile really exists** in the
  active ``$CODEX_HOME/config.toml``. No profile ⇒ refuse, rather than fall back to
  the legacy ``workspace-write`` sandbox that has no deny rules.
* :func:`mcp_disable_args` — ``-c mcp_servers.<name>.enabled=false`` per configured
  server, for the internal calls that must not spin up MCP servers. (Codex's ``-c``
  deep-MERGES tables, so the tempting ``-c mcp_servers={}`` is a silent no-op —
  verified against codex-cli 0.150.1; each server has to be disabled by name.)
* The **session journal** ``$CODEX_HOME/ccc-sessions.jsonl`` (mode 0600): one line per
  successful launch, so ``--resume <id|last>`` can re-validate the workspace root and
  the read/write mode of the session it re-attaches to instead of blindly inheriting
  whatever the old session ran under.

Refusals raise :class:`CodexLaunchError` (the CLI maps it to exit 2);
:class:`CodexMissing` is the "no ``codex`` on PATH" special case.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import json
import os
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The two named permission profiles ccc launches under. Both are user config
# (`$CODEX_HOME/config.toml`); see docs/reference.md for the exact tables.
READ_PROFILE = "hardened-ro"
WRITE_PROFILE = "hardened-rw"

# One JSON line per successful ccc-started codex session, inside the CODEX_HOME that
# billed it (so a seat switch never resumes another seat's session).
JOURNAL_NAME = "ccc-sessions.jsonl"

_GIT_TIMEOUT = 15  # seconds for the `git rev-parse` work-tree probe


class CodexLaunchError(RuntimeError):
    """A codex launch this policy refuses (the CLI turns it into exit 2)."""


class CodexMissing(CodexLaunchError):
    """The ``codex`` CLI is not on ``PATH`` (its own exit code at the CLI boundary)."""


# --------------------------------------------------------------------------- #
# Binary + workspace root
# --------------------------------------------------------------------------- #
def resolve_codex() -> str:
    """Absolute path of the ``codex`` CLI, or raise :class:`CodexMissing`.

    Never guesses a location: a missing binary is an error the caller reports, not
    something to paper over with a bare ``"codex"`` argv[0] that fails later inside
    the supervised subprocess.
    """
    found = shutil.which("codex")
    if not found:
        raise CodexMissing(
            "`codex` CLI not found on PATH — install the Codex CLI (or add it to PATH) "
            "before delegating to Codex."
        )
    return found


@dataclass(frozen=True)
class Workdir:
    """The validated ``-C`` workspace root for one codex run.

    ``path`` is absolute and symlink-resolved; ``skip_git_check`` says whether the run
    needs ``--skip-git-repo-check`` (true only for an EXPLICIT non-git directory);
    ``write`` records the mode it was validated for, which is what the resume guard
    re-checks. Usable directly as a path (``os.fspath`` / ``str``).
    """

    path: Path
    skip_git_check: bool
    write: bool = False

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)


def _is_git_worktree(path: Path) -> bool:
    """Whether *path* sits inside a git work tree (``git rev-parse --show-toplevel``)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def resolve_workdir(cwd_arg: str | None, *, write: bool) -> Workdir:
    """Validate the workspace root for a codex run and return it as a :class:`Workdir`.

    *cwd_arg* is the caller's ``-C`` value (``None``/blank ⇒ the process cwd, an
    IMPLICIT root). The rules, in order:

    1. resolve strictly (symlinks included) — a non-existent root is a refusal, never a
       silently-created one;
    2. refuse ``$HOME`` itself, and refuse any directory that CONTAINS ``$HOME`` — a
       write run rooted there has the whole account as its workspace, and even a read
       run would hand Codex an unbounded tree to walk;
    3. an IMPLICIT root must be inside a git work tree (the "I forgot ``-C``" trap: the
       run would otherwise use whatever directory the caller happened to be in);
    4. an EXPLICIT non-git root is fine, but marks ``skip_git_check`` so the caller adds
       ``--skip-git-repo-check`` only where it is genuinely needed.

    Raises :class:`CodexLaunchError` (exit 2) on every refusal, with the reason.
    """
    mode = "write" if write else "read-only"
    explicit = bool((cwd_arg or "").strip())
    raw = Path(cwd_arg or "").expanduser() if explicit else Path(os.getcwd())
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CodexLaunchError(
            f"refusing a {mode} codex run: workspace root {raw} does not resolve ({exc})"
        ) from exc
    if not resolved.is_dir():
        raise CodexLaunchError(
            f"refusing a {mode} codex run: workspace root {resolved} is not a directory"
        )
    home = Path.home().resolve()
    if resolved == home:
        raise CodexLaunchError(
            f"refusing a {mode} codex run rooted at $HOME ({home}) — point -C at the repo "
            "you actually want Codex to work in."
        )
    if home.is_relative_to(resolved):
        raise CodexLaunchError(
            f"refusing a {mode} codex run rooted at {resolved}: it CONTAINS $HOME ({home}), "
            "so the whole account would be the workspace. Point -C at a repo."
        )
    in_git = _is_git_worktree(resolved)
    if not in_git and not explicit:
        raise CodexLaunchError(
            f"refusing a {mode} codex run: the implicit working directory {resolved} is not "
            "inside a git work tree. Pass -C <dir> explicitly if that is really the workspace."
        )
    return Workdir(path=resolved, skip_git_check=not in_git, write=write)


# --------------------------------------------------------------------------- #
# CODEX_HOME + permission profiles
# --------------------------------------------------------------------------- #
def active_codex_home() -> Path:
    """The ``CODEX_HOME`` this launch bills to (env → ccc's seat selector → ``~/.codex``).

    Routed through :func:`command_center.codex_in_claude.codex_exec_env` so the profile
    lookup and the journal land in the very home the child ``codex`` will use — a pinned
    or selector-chosen seat included.
    """
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    try:
        from .codex_in_claude import codex_exec_env  # local: avoid an import cycle

        return Path(codex_exec_env()["CODEX_HOME"]).expanduser()
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return Path.home() / ".codex"


def config_toml_path(codex_home: Path | None = None) -> Path:
    """``<CODEX_HOME>/config.toml`` (the file the profiles are declared in)."""
    return (codex_home if codex_home is not None else active_codex_home()) / "config.toml"


def _config_data(codex_home: Path | None = None) -> dict[str, object]:
    """Parsed ``config.toml`` for *codex_home* (empty dict on any read/parse failure)."""
    try:
        return tomllib.loads(config_toml_path(codex_home).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def permission_profiles(codex_home: Path | None = None) -> set[str]:
    """Names of the ``[permissions.<name>]`` tables declared in the active config."""
    table = _config_data(codex_home).get("permissions")
    if not isinstance(table, dict):
        return set()
    return {str(name) for name, body in table.items() if isinstance(body, dict)}


def permission_args(write: bool, *, codex_home: Path | None = None) -> list[str]:
    """The ``-c default_permissions=…`` argv for a read (or write) run.

    Never ``-s``/``--sandbox``: that flag forces Codex's legacy sandbox and drops the
    profile's deny rules. A write run is allowed ONLY when the active config really
    declares ``[permissions.hardened-rw]`` — otherwise this refuses (exit 2) instead of
    degrading to a rule-free ``workspace-write``.
    """
    if write and WRITE_PROFILE not in permission_profiles(codex_home):
        path = config_toml_path(codex_home)
        raise CodexLaunchError(
            f"no {WRITE_PROFILE} profile configured; refusing legacy workspace-write. "
            f"Add a [permissions.{WRITE_PROFILE}] table to {path} (see docs/reference.md "
            "§ 'Codex permission profiles'), or run read-only."
        )
    return ["-c", f'default_permissions="{WRITE_PROFILE if write else READ_PROFILE}"']


def mcp_disable_args(codex_home: Path | None = None) -> list[str]:
    """``-c mcp_servers.<name>.enabled=false`` for every server the active config declares.

    For ccc's own short internal calls, which must not pay for (or hang on) an MCP
    server handshake. Per-server by name on purpose: codex's ``-c`` deep-merges tables,
    so ``-c mcp_servers={}`` parses fine and changes NOTHING (verified against
    codex-cli 0.150.1) — there is no wholesale "no MCP" switch short of
    ``--ignore-user-config``, which would also drop the permission profiles.
    Empty list when no servers are configured.
    """
    table = _config_data(codex_home).get("mcp_servers")
    if not isinstance(table, dict):
        return []
    args: list[str] = []
    for name in sorted(str(key) for key in table):
        args += ["-c", f"mcp_servers.{name}.enabled=false"]
    return args


# --------------------------------------------------------------------------- #
# Session journal
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LaunchRecord:
    """One journalled codex launch (the unit ``--resume`` re-validates against)."""

    ts: int
    session_id: str
    resolved_cwd: str
    permission_profile: str
    write: bool


def journal_path(codex_home: Path | None = None) -> Path:
    """``<CODEX_HOME>/ccc-sessions.jsonl`` — the per-seat launch journal."""
    return (codex_home if codex_home is not None else active_codex_home()) / JOURNAL_NAME


def record_launch(
    session_id: str,
    workdir: Workdir | str | os.PathLike[str],
    *,
    write: bool,
    codex_home: Path | None = None,
    now: int | None = None,
) -> LaunchRecord | None:
    """Append one launch to the journal (mode 0600); return the record, or ``None``.

    Best-effort: a blank *session_id* (codex printed no banner) or any I/O error simply
    journals nothing — a launch must never fail because bookkeeping did. The file is
    created 0600 and re-chmodded on every append, so an inherited wider mode is fixed.
    """
    if not (session_id or "").strip():
        return None
    record = LaunchRecord(
        ts=int(time.time()) if now is None else int(now),
        session_id=session_id.strip(),
        resolved_cwd=str(os.fspath(workdir)),
        permission_profile=WRITE_PROFILE if write else READ_PROFILE,
        write=bool(write),
    )
    path = journal_path(codex_home)
    line = json.dumps(
        {
            "ts": record.ts,
            "session_id": record.session_id,
            "resolved_cwd": record.resolved_cwd,
            "permission_profile": record.permission_profile,
            "write": record.write,
        },
        sort_keys=True,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    except OSError:
        return None
    return record


def read_journal(codex_home: Path | None = None) -> list[LaunchRecord]:
    """Every parsable journal record, oldest first (a corrupt line is skipped)."""
    try:
        text = journal_path(codex_home).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    records: list[LaunchRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or not data.get("session_id"):
            continue
        records.append(
            LaunchRecord(
                ts=int(data.get("ts") or 0),
                session_id=str(data["session_id"]),
                resolved_cwd=str(data.get("resolved_cwd") or ""),
                permission_profile=str(data.get("permission_profile") or ""),
                write=bool(data.get("write")),
            )
        )
    return records


def resolve_resume_any(
    ref: str, *, write: bool, homes: dict[str, Path] | None = None
) -> tuple[LaunchRecord, Path]:
    """Resolve ``--resume`` *ref* across EVERY seat's journal → ``(record, its home)``.

    A codex session lives in exactly ONE ``CODEX_HOME``, and since the runner picks the
    seat at run time the session id a previous round reported may well sit in a journal
    the CURRENT selection would never look at. Searching every *homes* entry (plus the
    effective one, for an unregistered ``$CODEX_HOME``) is what makes ``--resume <id>``
    keep working across a seat hop; the returned home is then BOUND for the run — a
    resume never falls back to another seat, because there is nothing to resume there.

    *homes* is the caller's seat registry (``codex_in_claude.canonical_codex_homes()``);
    this module deliberately does not reach for it itself — the launch policy owns argv,
    not the account list, and importing the registry here would add an import cycle.
    ``None`` searches only the effective home.

    ``last`` means the newest record across all searched homes, not the newest of
    whichever home happens to be selected. Every other guard of :func:`resolve_resume`
    still applies (ccc-journalled, same read/write mode, root still acceptable).
    """
    wanted = (ref or "").strip()
    if not wanted:
        raise CodexLaunchError("--resume needs a codex session id (or `last`)")
    homes = dict(homes or {})
    active = active_codex_home()
    if not any(str(home) == str(active) for home in homes.values()):
        homes["explicit"] = active
    found: list[tuple[LaunchRecord, Path]] = []
    for home in homes.values():
        for record in read_journal(home):
            if wanted in ("last", record.session_id):
                found.append((record, home))
    if not found:
        raise CodexLaunchError(
            f"refusing --resume {wanted}: no such session in any seat's {JOURNAL_NAME} — "
            "only codex sessions ccc launched can be resumed."
        )
    record, home = max(found, key=lambda pair: pair[0].ts)
    if record.write != write:
        had = "write" if record.write else "read-only"
        want = "write" if write else "read-only"
        raise CodexLaunchError(
            f"refusing --resume {record.session_id}: it was launched {had}, but this round "
            f"asks for {want}. `codex exec resume` inherits the original permissions — "
            "start a fresh session for the other mode."
        )
    try:
        resolve_workdir(record.resolved_cwd, write=write)
    except CodexLaunchError as exc:
        raise CodexLaunchError(
            f"refusing --resume {record.session_id}: its workspace root is no longer "
            f"acceptable — {exc}"
        ) from exc
    return record, home


def resolve_resume(ref: str, *, write: bool, codex_home: Path | None = None) -> LaunchRecord:
    """The journal record ``--resume`` *ref* names, or refuse with the reason.

    ``codex exec resume`` inherits the OLD session's permissions and working root while
    ``--write`` is recomputed from the NEW command line, so an unchecked resume can
    quietly run a write round inside a root the current policy would reject. Three
    conditions, all required: the session was launched by ccc (it is journalled under
    the active ``CODEX_HOME``), its mode matches the requested one, and its workspace
    root still passes :func:`resolve_workdir` today.
    """
    wanted = (ref or "").strip()
    if not wanted:
        raise CodexLaunchError("--resume needs a codex session id (or `last`)")
    records = read_journal(codex_home)
    if wanted == "last":
        record = records[-1] if records else None
    else:
        record = next((r for r in reversed(records) if r.session_id == wanted), None)
    if record is None:
        raise CodexLaunchError(
            f"refusing --resume {wanted}: no such session in {journal_path(codex_home)} — "
            "only codex sessions ccc launched (on this seat) can be resumed."
        )
    if record.write != write:
        had = "write" if record.write else "read-only"
        want = "write" if write else "read-only"
        raise CodexLaunchError(
            f"refusing --resume {record.session_id}: it was launched {had}, but this round "
            f"asks for {want}. `codex exec resume` inherits the original permissions — "
            "start a fresh session for the other mode."
        )
    try:
        resolve_workdir(record.resolved_cwd, write=write)
    except CodexLaunchError as exc:
        raise CodexLaunchError(
            f"refusing --resume {record.session_id}: its workspace root is no longer "
            f"acceptable — {exc}"
        ) from exc
    return record
