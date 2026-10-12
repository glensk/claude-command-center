#!/usr/bin/env python3
"""The Codex run log: one inspectable directory per PHYSICAL ``codex exec`` attempt.

The run ledger (:mod:`command_center.codex_ledger`) answers *"what did this Mac spend,
and when?"* in one line per attempt. It cannot answer *"why did that round fail?"*: a
``codex exec --json`` failure reports its cause as a ``turn.failed`` EVENT on stdout
(``Selected model is at capacity``), its stderr is often empty, and the runner used to
quote only the stderr tail — so a debate round could fail with ``Codex exited 1:`` and
nothing after the colon, and with ``--ephemeral`` not even a rollout file survived to
look the cause up in.

Every attempt the runner launches therefore gets a directory under
:func:`runlog_root` (``<app_home>/codex-runlog/`` unless ``$CODEX_IN_CLAUDE_RUNLOG_DIR``
says otherwise), named ``<YYYYmmddTHHMMSS>-<purpose>[-<note>]-<session8>-<attempt8>``:

``run.json``
    The attempt's facts: argv (token-shaped strings redacted), seat + its
    ``CODEX_HOME``, model, effort, cwd, the Claude session, the caller's ``-N`` note,
    started/ended/duration, exit code, outcome, error kind, the one-line cause, the
    codex ``thread_id`` and the pointer to Codex's own session (rollout) file when the
    run was not ephemeral. Written when the attempt STARTS (``status: running``) and
    rewritten when it ends, so a runner that was itself killed still leaves a record.
``prompt.txt`` / ``events.jsonl`` / ``stderr.log`` / ``reply.txt``
    What codex was given, its ``--json`` event stream, its stderr and its ``-o`` reply.

Everything written passes :func:`scrub` (API keys, bearer tokens, JWTs, Slack/GitHub
tokens → ``[redacted]``); files are ``0600`` in ``0700`` directories. Retention runs on
every new record: at most :data:`DEFAULT_KEEP` directories, none older than
:data:`DEFAULT_DAYS` days, and at most :data:`DEFAULT_MAX_MB` MB in total (oldest go
first; the env knobs are ``$CODEX_IN_CLAUDE_RUNLOG_KEEP`` / ``_DAYS`` / ``_MAX_MB``).
``$CODEX_IN_CLAUDE_RUNLOG=0`` turns the whole thing off.

Best-effort throughout: a run log that cannot be written never fails or delays the
Codex call it describes — every public entrance swallows its own errors.

``codex-in-claude runlog`` lists and shows the records (:func:`render_list`,
:func:`render_show`).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, links

RUNLOG_NAME = "codex-runlog"
RUNLOG_DIR_ENV = "CODEX_IN_CLAUDE_RUNLOG_DIR"
RUNLOG_SWITCH_ENV = "CODEX_IN_CLAUDE_RUNLOG"
KEEP_ENV = "CODEX_IN_CLAUDE_RUNLOG_KEEP"
DAYS_ENV = "CODEX_IN_CLAUDE_RUNLOG_DAYS"
MAX_MB_ENV = "CODEX_IN_CLAUDE_RUNLOG_MAX_MB"

#: Retention defaults: a debate day is ~10-20 attempts, so 200 directories are weeks of
#: history; an xhigh round's event stream can reach a few MB, hence the size cap.
DEFAULT_KEEP = 200
DEFAULT_DAYS = 14
DEFAULT_MAX_MB = 500

SCHEMA_VERSION = 1
RECORD_NAME = "run.json"

#: One cause line is a diagnosis, never a log: capped where it is BUILT, so the ERROR line,
#: the ``-j`` envelope and the ledger all carry the same bounded text.
CAUSE_CHARS = 400

# Token shapes redacted from everything the run log writes or the runner quotes. Each
# pattern matches only token characters (never a quote or a backslash), so redacting
# inside a JSON line keeps the line valid JSON.
REDACTED = "[redacted]"
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bxox[abcdeoprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer )[A-Za-z0-9._~+/\-]{16,}=*"), r"\1" + REDACTED),
    (
        re.compile(
            r"(?i)\b(api_key|apikey|access_token|refresh_token|id_token|password|secret)="
            r"[A-Za-z0-9._~+/\-]{8,}"
        ),
        r"\1=" + REDACTED,
    ),
)
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def scrub(text: str) -> str:
    """*text* with every token-shaped substring replaced by ``[redacted]``."""
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def one_line(text: str, limit: int = CAUSE_CHARS) -> str:
    """*text* scrubbed, whitespace collapsed to single spaces, cut to *limit* chars."""
    flat = " ".join(scrub(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _env_int(name: str, default: int) -> int:
    """``$name`` as a non-negative int; *default* when unset or garbage."""
    raw = os.environ.get(name, "").strip()
    try:
        return max(0, int(raw)) if raw else default
    except ValueError:
        return default


def enabled() -> bool:
    """False only when ``$CODEX_IN_CLAUDE_RUNLOG`` is ``0``/``off``/``false``/``no``."""
    return os.environ.get(RUNLOG_SWITCH_ENV, "").strip().lower() not in ("0", "off", "false", "no")


def runlog_root() -> Path:
    """``$CODEX_IN_CLAUDE_RUNLOG_DIR``, else ``<app_home>/codex-runlog`` (next to the ledger)."""
    override = os.environ.get(RUNLOG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return config.app_home() / RUNLOG_NAME


def _slug(text: str, limit: int) -> str:
    """A path-safe, bounded piece of a directory name (``""`` for nothing)."""
    return _SLUG_RE.sub("-", text or "").strip("-.")[:limit].strip("-.")


def _iso(epoch: float) -> str:
    """Local ISO seconds with offset — the ledger's spelling."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _write_private(path: Path, text: str) -> None:
    """Write *text* to *path* atomically with mode 0600."""
    handle, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def find_rollout(home: Path | str, thread_id: str, started: float) -> str:
    """Codex's own session file for *thread_id*, or ``""``.

    Codex files a rollout as ``<home>/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl``
    under the LOCAL date it started; only the start day and today are searched (a round
    never spans more), so the lookup stays a couple of directory listings.
    """
    if not thread_id:
        return ""
    base = Path(home) / "sessions"
    days = {datetime.fromtimestamp(started).date(), datetime.now().date()}
    days |= {day - timedelta(days=1) for day in set(days)}
    for day in sorted(days, reverse=True):
        folder = base / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        with contextlib.suppress(OSError):
            for hit in folder.glob(f"rollout-*{thread_id}.jsonl"):
                return str(hit)
    return ""


class RunLog:  # pylint: disable=too-many-instance-attributes  # the record's identity
    """The run-log directory of ONE physical attempt (a no-op object when disabled).

    :meth:`begin` creates the directory and the ``running`` record, :meth:`started`
    adds the argv and the prompt, :meth:`finish` writes the outputs and the final record.
    Every method swallows its own errors: :attr:`path` is ``None`` when nothing could be
    written, and the attempt goes on either way.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self.record: dict[str, Any] = {}
        self._t0 = time.time()

    @classmethod
    def begin(  # pylint: disable=too-many-arguments  # the attempt's identity, all keywords
        cls,
        *,
        attempt_id: str,
        purpose: str,
        note: str = "",
        caller: str = "",
        seat: str,
        seat_id: str,
        home: Path | str,
        model: str,
        effort: str,
        cwd: str,
        write: bool,
    ) -> RunLog:
        """Open the record for one attempt; never raises."""
        log = cls()
        if not enabled():
            return log
        session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        stamp = datetime.fromtimestamp(log._t0).strftime("%Y%m%dT%H%M%S")
        parts = [stamp, _slug(purpose, 20) or "run"]
        if note:
            parts.append(_slug(note, 24))
        parts += [_slug(session, 8) or "nosession", attempt_id[:8]]
        name = "-".join(p for p in parts if p)
        log.record = {
            "schema_version": SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "status": "running",
            "started": _iso(log._t0),
            "ended": None,
            "duration_s": None,
            "runner_pid": os.getpid(),
            "purpose": purpose,
            "note": note,
            "caller": caller,
            "session": session,
            "cwd": cwd,
            "write": write,
            "seat": seat,
            "seat_id": seat_id,
            "codex_home": str(home),
            "model": model,
            "effort": effort,
        }
        try:
            root = runlog_root()
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            prune(root)
            path = root / name
            path.mkdir(mode=0o700)
            log.path = path
            log._save()
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            log.path = None
        return log

    def _save(self) -> None:
        if self.path is not None:
            _write_private(self.path / RECORD_NAME, json.dumps(self.record, indent=1) + "\n")

    def _file(self, name: str, text: str) -> None:
        if self.path is not None:
            _write_private(self.path / name, scrub(text))

    def started(self, argv: list[str], prompt: str) -> None:
        """Record the exact argv (redacted) and the prompt; never raises."""
        if self.path is None:
            return
        try:
            self.record["argv"] = [scrub(str(arg)) for arg in argv]
            self.record["ephemeral"] = "--ephemeral" in argv
            self.record["prompt_chars"] = len(prompt)
            self.record["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            self._file("prompt.txt", prompt)
            self._save()
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass

    def finish(  # pylint: disable=too-many-arguments  # the attempt's outcome, all keywords
        self,
        *,
        outcome: str,
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
        reply: str = "",
        error_kind: str = "",
        cause: str = "",
        message: str = "",
        thread_id: str = "",
        tokens: tuple[int, int] = (0, 0),
        elapsed_s: float | None = None,
    ) -> None:
        """Write the outputs and the final record; never raises."""
        if self.path is None:
            return
        try:
            now = time.time()
            events = [ln for ln in stdout.splitlines() if ln.strip().startswith("{")]
            last_type = ""
            if events:
                with contextlib.suppress(ValueError, AttributeError):
                    last_type = str(json.loads(events[-1]).get("type") or "")
            self._file("events.jsonl", stdout)
            self._file("stderr.log", stderr)
            if reply:
                self._file("reply.txt", reply)
            home = self.record.get("codex_home") or ""
            rollout = (
                "" if self.record.get("ephemeral") else find_rollout(home, thread_id, self._t0)
            )
            self.record.update(
                {
                    "status": "ok" if outcome == "ok" else "failed",
                    "ended": _iso(now),
                    "duration_s": round(now - self._t0 if elapsed_s is None else elapsed_s, 3),
                    "returncode": returncode,
                    "outcome": outcome,
                    "error_kind": error_kind,
                    "cause": one_line(cause),
                    "message": scrub(message),
                    "thread_id": thread_id,
                    "codex_session_file": rollout or None,
                    "events": len(events),
                    "last_event_type": last_type,
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stderr_bytes": len(stderr.encode("utf-8")),
                    "tokens_in": tokens[0],
                    "tokens_out": tokens[1],
                }
            )
            self._save()
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass

    @property
    def dir(self) -> str:
        """The directory as a string, ``""`` when there is none."""
        return "" if self.path is None else str(self.path)


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def _run_dirs(root: Path) -> list[Path]:
    """Every run directory under *root*, oldest first (names start with the stamp)."""
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and (p / RECORD_NAME).exists())
    except OSError:
        return []


def _dir_bytes(path: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for child in path.iterdir():
            with contextlib.suppress(OSError):
                total += child.stat().st_size
    return total


def prune(root: Path | None = None, now: float | None = None) -> list[Path]:
    """Apply the retention policy to *root*; the directories removed, oldest first.

    Keeps the newest :data:`DEFAULT_KEEP` (``$CODEX_IN_CLAUDE_RUNLOG_KEEP``), drops any
    older than :data:`DEFAULT_DAYS` days (``_DAYS``), then the oldest until the total is
    under :data:`DEFAULT_MAX_MB` MB (``_MAX_MB``). A knob set to ``0`` disables that rule.
    """
    root = runlog_root() if root is None else root
    now = time.time() if now is None else now
    keep = _env_int(KEEP_ENV, DEFAULT_KEEP)
    days = _env_int(DAYS_ENV, DEFAULT_DAYS)
    max_bytes = _env_int(MAX_MB_ENV, DEFAULT_MAX_MB) * 1024 * 1024
    dirs = _run_dirs(root)
    doomed: list[Path] = []
    if keep and len(dirs) > keep:
        doomed += dirs[: len(dirs) - keep]
    survivors = [d for d in dirs if d not in doomed]
    if days:
        cutoff = now - days * 86400
        for path in survivors:
            with contextlib.suppress(OSError):
                if (path / RECORD_NAME).stat().st_mtime < cutoff:
                    doomed.append(path)
        survivors = [d for d in survivors if d not in doomed]
    if max_bytes:
        sizes = {d: _dir_bytes(d) for d in survivors}
        total = sum(sizes.values())
        for path in survivors:
            if total <= max_bytes:
                break
            doomed.append(path)
            total -= sizes[path]
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    return sorted(doomed)


# --------------------------------------------------------------------------- #
# Reading: `codex-in-claude runlog`
# --------------------------------------------------------------------------- #
def read_record(path: Path) -> dict[str, Any] | None:
    """The ``run.json`` of one run directory, or None when unreadable."""
    try:
        data = json.loads((path / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["dir"] = str(path)
    return data


def list_runs(
    root: Path | None = None, *, limit: int = 20, failed_only: bool = False
) -> list[dict[str, Any]]:
    """The newest *limit* records (0 = all), newest first; *failed_only* drops successes."""
    rows: list[dict[str, Any]] = []
    for path in reversed(_run_dirs(runlog_root() if root is None else root)):
        record = read_record(path)
        if record is None:
            continue
        if failed_only and record.get("status") == "ok":
            continue
        rows.append(record)
        if limit and len(rows) >= limit:
            break
    return rows


def resolve(ref: str, root: Path | None = None) -> Path | None:
    """The run directory *ref* names: ``last``, ``last-failed``, a path, a directory
    name, a unique name prefix or a unique ``attempt_id`` prefix."""
    root = runlog_root() if root is None else root
    if ref in ("last", "last-failed"):
        rows = list_runs(root, limit=1, failed_only=ref == "last-failed")
        return Path(rows[0]["dir"]) if rows else None
    candidate = Path(ref).expanduser()
    if candidate.is_dir() and (candidate / RECORD_NAME).exists():
        return candidate
    dirs = _run_dirs(root)
    exact = [d for d in dirs if d.name == ref]
    if exact:
        return exact[0]
    hits = [d for d in dirs if d.name.startswith(ref)]
    if not hits:
        hits = [
            d for d in dirs if str((read_record(d) or {}).get("attempt_id") or "").startswith(ref)
        ]
    return hits[0] if len(hits) == 1 else None


def _mark(record: dict[str, Any]) -> str:
    status = record.get("status")
    return "✅" if status == "ok" else "⏳" if status == "running" else "❌"


def render_list(rows: list[dict[str, Any]]) -> list[str]:
    """One line per record: mark, start, seat, model, purpose/note, duration, kind, dir."""
    lines: list[str] = []
    for row in rows:
        started = str(row.get("started") or "")[:19].replace("T", " ")
        what = str(row.get("purpose") or "")
        if row.get("note"):
            what += f" [{row['note']}]"
        took = row.get("duration_s")
        took_s = f"{float(took):.0f}s" if isinstance(took, int | float) else "-"
        kind = str(row.get("error_kind") or row.get("outcome") or row.get("status") or "")
        path = Path(str(row.get("dir")))
        lines.append(
            f"{_mark(row)} {started}  {str(row.get('seat') or '-'):<8} "
            f"{str(row.get('model') or '-'):<14} {what:<24} {took_s:>6}  {kind:<16} "
            + links.local_link(path, path.name)
        )
        if row.get("status") != "ok" and row.get("cause"):
            lines.append(f"      cause: {row['cause']}")
    return lines


def _tail(path: Path, lines: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]


def render_show(path: Path, *, tail: int = 20) -> list[str]:
    """The full record of one run: the facts, the cause, the stderr and event tails."""
    record = read_record(path) or {}
    out = [f"{_mark(record)} run log {links.local_link(path)}"]
    for key in (
        "attempt_id",
        "started",
        "ended",
        "duration_s",
        "purpose",
        "note",
        "caller",
        "session",
        "cwd",
        "seat",
        "codex_home",
        "model",
        "effort",
        "returncode",
        "outcome",
        "error_kind",
        "cause",
        "thread_id",
        "events",
        "last_event_type",
        "tokens_in",
        "tokens_out",
    ):
        value = record.get(key)
        if value not in (None, ""):
            out.append(f"  {key + ':':<17} {value}")
    rollout = record.get("codex_session_file")
    if rollout:
        out.append(f"  {'codex session:':<17} {links.local_link(Path(str(rollout)))}")
    elif record.get("ephemeral"):
        out.append(f"  {'codex session:':<17} none (ephemeral run)")
    if record.get("argv"):
        out.append(f"  {'argv:':<17} {' '.join(str(a) for a in record['argv'])}")
    if record.get("message"):
        out.append(f"  {'message:':<17} {record['message']}")
    for name, label in (("stderr.log", "stderr"), ("events.jsonl", "events")):
        lines = _tail(path / name, tail)
        if lines:
            out.append(f"--- last {len(lines)} {label} lines ({links.local_link(path / name)})")
            out += [ln if len(ln) <= 400 else ln[:399] + "…" for ln in lines]
    return out
