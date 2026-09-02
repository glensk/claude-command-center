#!/usr/bin/env python3
"""The two-sided reconciler between FUTURE-job draft rows and their Obsidian files.

A *future job* (a ``draft=1`` row in the store) is mirrored as exactly one markdown
file under ``future_root(cfg)/<cat>/<repo>/<slug>-<hash>.md`` — the human editing
surface (see ``PLAN_future-job-files.md``). :mod:`command_center.future_files` owns
the file *shape*; this module drives the disk ↔ DB sync using those primitives.

Design invariants (do not regress):

* **File wins** on the file-owned fields (aim, prompt, repo, start_when, deadline,
  job_type). A concurrent DB change is detected (``updated_at`` past
  ``future_synced_at`` by more than :data:`_SYNC_SLOP_MS`), still resolved file-wins,
  and the conflict recorded (report + a managed note in the file).
* **Echo suppression.** ``sessions.future_sync_hash`` is the sha256 at the last sync.
  A file whose hash still matches is *untouched*: we only re-export it if the DB row
  changed, and a second no-op pass writes **nothing** (kills the launchd retrigger loop).
* **Every file write is atomic** (temp file + :func:`os.replace`) and idempotent — the
  managed error block (:func:`~command_center.future_files.upsert_error_block`) and the
  conflict note are both fixed points.
* **Chokepoints.** File-originated AIM changes go through :meth:`Store.set_aim`
  (history + score + detached short-aim/score spawn); registration through
  :meth:`Store.create_draft`.
* **Flock singleton.** ``app_home()/future_sync.lock`` serialises concurrent runs
  (the launchd WatchPaths trigger, the daemon backstop, detached spawns).
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import dataclasses
import fcntl
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import IO

from . import config, deps, repos, scrub
from .future_files import (
    ParsedJob,
    content_hash,
    cwd_to_repo,
    delete_root,
    display_hash,
    future_root,
    job_filename,
    launch_requested,
    pad_path,
    parse_job_file,
    rel_dir_for,
    repo_to_cwd,
    serialize,
    upsert_error_block,
    validate,
)
from .models import DEFAULT_LLM, JOB_TYPES, LLM_CHOICES, Session, now_ms
from .store import Store

# A DB change is a "conflict" only if it happened more than this long after the last
# sync — the slop absorbs the sync's OWN trailing ``update_fields`` bump of ``updated_at``
# (which is always a few ms past the ``future_synced_at`` we write in the same pass) and
# the millisecond-boundary jitter, so a clean re-import never flags a phantom conflict. A
# real agent/user DB edit is seconds-to-minutes later, well past the slop.
_SYNC_SLOP_MS = 2000

# The managed conflict note — marker-delimited and FIXED (no timestamp) so it is a fixed
# point: a later untouched-export re-attaches the identical block, so it never churns.
_CN_START = "<!-- ccc-sync-conflict -->"
_CN_END = "<!-- /ccc-sync-conflict -->"
_CN_RE = re.compile(r"\n*" + re.escape(_CN_START) + r".*?" + re.escape(_CN_END) + r"\n*", re.DOTALL)
_CN_BODY = (
    "> [!warning] ccc-sync conflict: this file and its database row both changed since the\n"
    "> last sync. The file's values were kept (file wins) and the database was overwritten."
)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
@dataclass
class SyncReport:
    """What one :func:`run_sync` pass did (counts via ``len`` + a free-text detail log)."""

    exported: list[str] = field(default_factory=list)  # DB→file writes (bootstrap / DB change)
    imported: list[str] = field(default_factory=list)  # file→DB writes (user edited the file)
    registered: list[str] = field(default_factory=list)  # ready file/pad → new draft row
    errors: list[str] = field(default_factory=list)  # files flipped to status: error
    archived: list[str] = field(default_factory=list)  # drafts archived after the delete grace
    # ``launch: true`` toggles consumed this pass (spawn attempted). Not part of
    # ``total()`` — the flip itself already counts as imported/registered.
    launched: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)  # conflicts, relocations, duplicates …

    def total(self) -> int:
        """Number of sessions/files touched this pass (0 = a no-op pass)."""
        return sum(
            len(x)
            for x in (self.exported, self.imported, self.registered, self.errors, self.archived)
        )


# ---------------------------------------------------------------------------
# paths / vault mapping
# ---------------------------------------------------------------------------
def vault_root(cfg: config.Config) -> Path:
    """Expanded Obsidian vault root (``vault_root`` config key)."""
    return Path(cfg.vault_root).expanduser()


def _vault_relpath(cfg: config.Config, path: Path) -> str:
    """The vault-relative form of *path* stored in ``sessions.future_file``.

    Falls back to the absolute string when *path* is not under the vault (should not
    happen for canonical files, but never raise).
    """
    root = vault_root(cfg)
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return str(path)


def _abs_path(cfg: config.Config, relpath: str) -> Path:
    """Absolute path of a stored (vault-relative) ``future_file`` value."""
    path = Path(relpath)
    return path if path.is_absolute() else vault_root(cfg) / relpath


def _today_iso() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _same(a: Path, b: Path) -> bool:
    """Whether two paths point at the same file (resolve; degrade to string compare)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _first_existing(paths: list[Path]) -> Path | None:
    return next((p for p in paths if p.exists()), None)


def _unique_path(path: Path) -> Path:
    """*path* if free, else ``<stem>-1``, ``<stem>-2`` … (collision-safe archive names)."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file in the same dir + :func:`os.replace`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _repo_options() -> list[str]:
    """Injected Meta Bind ``repo`` options: every ``<cat>/<repo>`` under ``$GIT_BASE``.

    Stale options are harmless (validation is the source of truth), so this is a plain
    on-disk listing — the same one the TUI picker uses.
    """
    options: list[str] = []
    for category in repos.categories():
        options.extend(f"{category}/{repo}" for repo in repos.repos_in(category))
    return options


def _fresh_uuid(taken: set[str]) -> str:
    """A new UUID whose 4-hex :func:`display_hash` is not already in *taken* (mutates it)."""
    while True:
        candidate = str(uuid.uuid4())
        short = display_hash(candidate)
        if short not in taken:
            taken.add(short)
            return candidate


def _spawn_aim_jobs(cfg: config.Config, session_id: str) -> None:
    """Mirror ``cmd_set_aim``: refine the AIM score + short-AIM label detached (never block).

    Skipped inside a ``claude -p`` (``CCC_INTERNAL``) to avoid recursion, and gated by the
    same config flags the interactive path uses.
    """
    if os.environ.get("CCC_INTERNAL"):
        return
    from .spawn import spawn_ccc  # pylint: disable=import-outside-toplevel

    if cfg.aim_score_on_set:
        spawn_ccc(["score-aim", "--session", session_id])
    if cfg.short_aim:
        spawn_ccc(["short-aim", "--session", session_id])


# ---------------------------------------------------------------------------
# conflict note (managed, idempotent)
# ---------------------------------------------------------------------------
def _spawn_launch(session_id: str) -> bool:
    """Start a job whose file's ``launch`` toggle was flipped (phone-friendly path).

    Opens a terminal (iTerm tab, or a tmux window under the ``launcher = "tmux"``
    trip config) running ``ccc start-job --force <id>`` — the same command the
    TUI's resume path uses. ``--force`` because flipping the toggle IS the explicit
    "start now" (an interactive premature-start question in an unwatched window
    would hang forever). Fail-open: any error just reports False.
    """
    try:
        from . import terminal  # pylint: disable=import-outside-toplevel

        return terminal.start_job_in_new_tab(session_id, force=True)
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def _credential_tripwire(cfg: config.Config, session: Session) -> scrub.CheckVerdict | None:
    """Check a draft's AIM + prompt for a live credential; ``None`` when not armed.

    **Armed** iff a mirror switch (``mirror_running`` / ``mirror_done`` /
    ``mirror_sessions``) is on and ``mirror_allow_unscrubbed`` is off. Rationale: without
    the mirrors the transcript never leaves the machine, so a fresh install with no broker
    must still be able to launch jobs; WITH them the launched prompt is echoed verbatim
    into the vault (the running/session cards), and those roots already require a
    scrubber — so demanding one here costs nothing extra and closes the hole at its
    source, before the value is ever spoken to a session.

    Runs the scrubber's ``check`` verb (a verdict, never a rewrite). An unresolvable
    scrubber is reported as DEGRADED with the resolution reason — fail closed, exactly
    like the mirror writes.
    """
    if not (cfg.mirror_running or cfg.mirror_done or cfg.mirror_sessions):
        return None
    if cfg.mirror_allow_unscrubbed:
        return None
    text = f"{session.aim or ''}\n{session.prompt or ''}"
    resolution = scrub.resolve_scrubber(cfg.mirror_scrub_cmd, verb="check")
    if resolution.scrubber is None:
        return scrub.CheckVerdict(scrub.DEGRADED, reason=resolution.reason)
    return scrub.check(resolution.scrubber, text)


def _refuse_launch(
    cfg: config.Config, session: Session, verdict: scrub.CheckVerdict, report: SyncReport
) -> None:
    """Record a refused launch: the managed ``ccc-sync-error`` callout + a report line.

    The callout names the credential's LABEL only (the checker never returns the value,
    and neither do we). Nothing is spawned and nothing is appended to ``report.launched``.
    """
    if verdict.state == scrub.LEAK:
        labels = ", ".join(verdict.labels) or "unlabelled"
        error = (
            f"launch refused: live credential in prompt/AIM ({labels}) — "
            "remove it and re-tick launch"
        )
        detail = f"launch refused (credential {labels}): {session.session_id}"
    else:
        error = (
            f"launch refused: credential scrubber degraded ({verdict.reason}) — "
            "fix the scrubber and re-tick launch"
        )
        detail = f"launch refused (scrubber degraded): {session.session_id}"
    report.details.append(detail)
    if not session.future_file:
        return
    path = _abs_path(cfg, session.future_file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        _write_error(path, text, [error], report)


def _consume_launch(store: Store, cfg: config.Config, session_id: str, report: SyncReport) -> None:
    """Act on a consumed ``launch: true`` flip: spawn iff the row is a live, unblocked draft.

    Called AFTER the canonical rewrite reset the file to ``launch: false`` (and
    stored its hash), so a failed spawn can never retrigger on the next pass —
    the user re-flips the toggle instead. A **dependency preflight** runs BEFORE
    the spawn: an unsatisfied ``depends_on`` (see :func:`deps.launch_blocker`) means
    do NOT spawn — instead write the managed ``ccc-sync-error`` callout onto the file
    ("blocked: depends on <4-hex> — <state>"). No retrigger loop (launch was already
    reset to false); the user re-flips once the dependency completes.

    Then the **credential tripwire** (:func:`_credential_tripwire`): with the vault
    mirrors on, a prompt/AIM carrying a live credential value is never launched — the
    session would echo it straight into the running/session mirrors. A leak or a degraded
    checker refuses the launch the same way a blocked dependency does (callout + report
    line, no spawn); the user scrubs the prompt (or fixes the scrubber) and re-ticks.
    """
    session = store.get(session_id)
    if session is None or not session.draft or session.archived or session.done:
        report.details.append(f"launch ignored (not a live draft): {session_id}")
        return
    blocker = deps.launch_blocker(store, session)
    if blocker is not None:
        report.details.append(
            f"launch blocked (depends on {blocker.parent_hash} — {blocker.state}): {session_id}"
        )
        if session.future_file:
            path = _abs_path(cfg, session.future_file)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text:
                _write_error(
                    path,
                    text,
                    [f"blocked: depends on {blocker.parent_hash} — {blocker.state}"],
                    report,
                )
        return
    verdict = _credential_tripwire(cfg, session)
    if verdict is not None and verdict.state != scrub.CLEAN:
        _refuse_launch(cfg, session, verdict, report)
        return
    ok = _spawn_launch(session_id)
    report.launched.append(session_id)
    report.details.append(f"launch {'spawned' if ok else 'FAILED (no terminal?)'}: {session_id}")


def _has_conflict_note(text: str) -> bool:
    return _CN_START in text


def _strip_conflict_note(text: str) -> str:
    return _CN_RE.sub("\n", text)


def _with_conflict_note(text: str) -> str:
    body = _strip_conflict_note(text).rstrip("\n")
    block = f"{_CN_START}\n{_CN_BODY}\n{_CN_END}"
    return f"{body}\n\n{block}\n"


# ---------------------------------------------------------------------------
# serialisation from a DB row
# ---------------------------------------------------------------------------
def _account_label(config_dir: str) -> str:
    """The job file's ``account`` frontmatter label for a *config_dir* path.

    In multi-account mode EVERY exported file carries a concrete label — including the
    default account's (``accounts.account_label("")`` resolves it) — so the Obsidian
    **account** select always shows a value. On a single-account machine the default
    account maps to "" (the key is then omitted by :func:`future_files.serialize`),
    keeping those files byte-identical. A non-default dir always maps to its label.
    """
    from . import accounts

    if accounts.is_multi_account():
        return accounts.account_label(config_dir)
    if not config_dir or accounts.is_default_config_dir(config_dir):
        return ""
    return accounts.account_label(config_dir)


def _account_config_dir(label: str) -> str:
    """Resolve a frontmatter account *label* back to its absolute config_dir ("" = default)."""
    from . import accounts

    return accounts.account_config_dir(label) if label.strip() else ""


def _serialize_registered(
    session: object, repo_label: str, repo_options: list[str], *, created: str, status: str
) -> str:
    """Canonical file content for a draft row (the DB is the source for these fields)."""
    return serialize(
        session_id=session.session_id,  # type: ignore[attr-defined]
        aim=session.aim or "",  # type: ignore[attr-defined]
        status=status,
        repo=repo_label,
        job_type=session.job_type or "claude",  # type: ignore[attr-defined]
        no_codex=bool(session.no_codex),  # type: ignore[attr-defined]
        llm_overseer=session.llm_overseer or DEFAULT_LLM,  # type: ignore[attr-defined]
        llm_exec=session.llm_exec or DEFAULT_LLM,  # type: ignore[attr-defined]
        start_when=session.start_when or "",  # type: ignore[attr-defined]
        start_date=session.start_date or "",  # type: ignore[attr-defined]
        depends_on=session.depends_on or "",  # type: ignore[attr-defined]
        deadline=session.deadline or "",  # type: ignore[attr-defined]
        created=created or "",
        account=_account_label(session.config_dir or ""),  # type: ignore[attr-defined]
        prompt=session.prompt,  # type: ignore[attr-defined]
        repo_options=repo_options,
    )


def _write_error(file: Path, text: str, errors: list[str], report: SyncReport) -> None:
    """Flip a file to ``status: error`` with the managed callout (idempotent; no DB write)."""
    new_text = upsert_error_block(text, errors)
    if new_text != text:
        _atomic_write(file, new_text)
    if file.name not in report.errors:
        report.errors.append(file.name)


# ---------------------------------------------------------------------------
# lock + sidecar
# ---------------------------------------------------------------------------
def _lock_path() -> Path:
    return config.app_home() / "future_sync.lock"


def _acquire_lock() -> IO[str] | None:
    """Take the flock singleton non-blocking; ``None`` when another sync already holds it."""
    config.app_home().mkdir(parents=True, exist_ok=True)
    # The handle is held for the whole run and released in the caller's finally.
    handle = open(_lock_path(), "w", encoding="utf-8")  # noqa: SIM115  # pylint: disable=consider-using-with
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _release_lock(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _sidecar_path() -> Path:
    return config.app_home() / "future_sync_state.json"


def _load_sidecar() -> dict[str, str]:
    try:
        data = json.loads(_sidecar_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _save_sidecar(state: dict[str, str]) -> None:
    path = _sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _prune_sidecar(state: dict[str, str]) -> None:
    for key in list(state):
        if not Path(key).exists():
            del state[key]


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def _scan_files(cfg: config.Config) -> list[Path]:
    """Canonical files under the future root, skipping any ``_``/``.`` path component.

    That excludes ``_archive/``, ``_dashboard.md`` and hidden/temp dirs; the pad lives
    outside the future root, so it is handled separately (:func:`_handle_pad`).
    """
    root = future_root(cfg)
    if not root.exists():
        return []
    out: list[Path] = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part.startswith(("_", ".")) for part in rel.parts):
            continue
        out.append(path)
    return sorted(out)


def _migrate_legacy_names(
    store: Store,
    cfg: config.Config,
    parsed: list[tuple[Path, str, ParsedJob]],
    db_by_id: dict[str, object],
    canonical_abs: dict[str, Path],
    report: SyncReport,
) -> None:
    """Rename old ``<hash>-<slug>.md`` files to the new ``<slug>-<hash>.md`` in place.

    Idempotent, automatic: a scanned file whose NAME starts with its own 4-hex
    :func:`display_hash` + ``-`` is renamed, REUSING the frozen slug part verbatim
    (never re-slugged from the current AIM), and *parsed*/``canonical_abs`` plus the
    DB ``future_file`` are repointed at the new path. Files already in the new format
    (or non-matching names) are untouched, so a second pass is a no-op. The content is
    byte-identical, so the stored sync hash stays valid (echo suppression preserved).
    """
    for index, (path, text, job) in enumerate(parsed):
        sid = job.session_id.strip()
        if not _is_uuid(sid):
            continue
        prefix = f"{display_hash(sid)}-"
        if not path.stem.startswith(prefix):
            continue  # new format or non-matching name — untouched
        slug_part = path.stem[len(prefix) :]
        new_name = f"{slug_part}-{display_hash(sid)}{path.suffix}"
        if new_name == path.name:  # pathological (slug itself starts with the hash)
            continue
        # Decide DB repointing BEFORE the rename, while *path* still resolves.
        row = db_by_id.get(sid)
        row_file = getattr(row, "future_file", None) if row is not None else None
        db_matches = bool(row is not None and row_file and _same(_abs_path(cfg, row_file), path))
        new_path = _unique_path(path.parent / new_name)
        try:
            os.replace(path, new_path)
        except OSError:
            continue
        parsed[index] = (new_path, text, job)
        if db_matches:
            store.update_fields(
                sid,
                future_file=_vault_relpath(cfg, new_path),
                future_sync_hash=content_hash(text),
                future_synced_at=now_ms(),
                future_missing_since=0,
            )
            canonical_abs[sid] = new_path
        report.details.append(f"migrated {display_hash(sid)}: {path.name} → {new_name}")


# ---------------------------------------------------------------------------
# public lifecycle helper
# ---------------------------------------------------------------------------
def archive_file(store: Store, cfg: config.Config, session: object, final_status: str) -> None:
    """Move a job's file to ``future_root/_archive/`` and stamp *final_status* on it.

    Used by the launch / mark-done lifecycle (Phase 3a): a launched or finished draft's
    file leaves the live scan (``_archive`` is skipped) with a terminal status. Clears
    ``future_missing_since`` and re-points ``future_file`` at the archived copy so the
    ``obsidian://`` link still resolves. Collision-safe filename; atomic.
    """
    session_id: str = session.session_id  # type: ignore[attr-defined]
    future_file: str | None = session.future_file  # type: ignore[attr-defined]
    if not future_file:
        store.update_fields(session_id, future_missing_since=0)
        return
    src = _abs_path(cfg, future_file)
    if not src.exists():
        store.update_fields(session_id, future_missing_since=0)
        return
    archive_dir = future_root(cfg) / "_archive"
    dest = _unique_path(archive_dir / src.name)
    try:
        text = src.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text:
        git_base = repos.git_base()
        job = parse_job_file(text)
        cwd: str = session.cwd  # type: ignore[attr-defined]
        session_jt: str = session.job_type or "claude"  # type: ignore[attr-defined]
        session_nc: bool = bool(session.no_codex)  # type: ignore[attr-defined]
        session_ov: str = session.llm_overseer or DEFAULT_LLM  # type: ignore[attr-defined]
        session_ex: str = session.llm_exec or DEFAULT_LLM  # type: ignore[attr-defined]
        session_aim: str = session.aim or ""  # type: ignore[attr-defined]
        _atomic_write(
            src,
            serialize(
                session_id=job.session_id or session_id,
                aim=job.aim or session_aim,
                status=final_status,
                repo=job.repo or cwd_to_repo(cwd, git_base),
                job_type=(job.job_type if job.job_type in JOB_TYPES else session_jt),
                no_codex=job.no_codex or session_nc,
                llm_overseer=(job.llm_overseer if job.llm_overseer in LLM_CHOICES else session_ov),
                llm_exec=(job.llm_exec if job.llm_exec in LLM_CHOICES else session_ex),
                start_when=job.start_when,
                start_date=job.start_date,
                depends_on=job.depends_on,
                deadline=job.deadline,
                created=job.created or _today_iso(),
                account=job.account,
                prompt=job.prompt,
                repo_options=_repo_options(),
            ),
        )
    archive_dir.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    store.update_fields(session_id, future_file=_vault_relpath(cfg, dest), future_missing_since=0)


def unarchive_file(store: Store, cfg: config.Config, session: object) -> None:
    """Inverse of :func:`archive_file`: move a job's archived file back to its live path.

    Used by ``start-job``'s ``execvp`` OSError recovery (Phase 3a) to undo a premature
    archive when the launch itself fails. Re-fetches the row (``future_file`` points into
    ``_archive/`` after :func:`archive_file`), regenerates the canonical content with
    ``status: registered`` from the DB, writes it to the canonical future-root path,
    removes the archived copy, and re-points ``future_file`` (updating the sync hash so the
    next pass is a no-op). No-op when the row has no file. Never raises for a missing file.
    """
    session_id: str = session.session_id  # type: ignore[attr-defined]
    row = store.get(session_id) or session
    future_file: str | None = row.future_file  # type: ignore[attr-defined]
    if not future_file:
        store.update_fields(session_id, future_missing_since=0)
        return
    git_base = repos.git_base()
    repo_label = cwd_to_repo(row.cwd, git_base)  # type: ignore[attr-defined]
    aim: str = row.aim or ""  # type: ignore[attr-defined]
    archived = _abs_path(cfg, future_file)
    created = _today_iso()
    if archived.exists():
        try:
            created = parse_job_file(archived.read_text(encoding="utf-8")).created or created
        except OSError:
            pass
    directory = future_root(cfg) / rel_dir_for(repo_label, git_base)
    dest = _unique_path(directory / job_filename(session_id, aim))
    canonical = _serialize_registered(
        row, repo_label, _repo_options(), created=created, status="registered"
    )
    _atomic_write(dest, canonical)
    if archived.exists() and not _same(archived, dest):
        try:
            archived.unlink()
        except OSError:
            pass
    store.update_fields(
        session_id,
        future_file=_vault_relpath(cfg, dest),
        future_sync_hash=content_hash(canonical),
        future_synced_at=now_ms(),
        future_missing_since=0,
    )


def delete_file(store: Store, cfg: config.Config, session: object) -> Path:
    """Move a job's file to the ``delete_root`` trash with ``status: deleted``.

    The trashed copy keeps the ``<cat>/<repo>`` substructure and full frontmatter
    (plus a ``deleted: <ISO date>`` stamp) and swaps the action buttons for the
    single "↩ Stage job back in" restore button — so the delete dashboard can list
    it and ``ccc restore-job --file`` can re-register it even if the DB row is
    later pruned. A fileless draft is serialized fresh from its row. Re-points
    ``future_file`` at the trashed copy (the caller flips ``archived``); returns
    the destination path.
    """
    session_id: str = session.session_id  # type: ignore[attr-defined]
    future_file: str | None = session.future_file  # type: ignore[attr-defined]
    git_base = repos.git_base()
    cwd: str = session.cwd  # type: ignore[attr-defined]
    aim: str = session.aim or ""  # type: ignore[attr-defined]
    src: Path | None = _abs_path(cfg, future_file) if future_file else None
    text = ""
    if src is not None and src.exists():
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            text = ""
    job = parse_job_file(text) if text else None
    repo_label = (job.repo if job and job.repo else "") or cwd_to_repo(cwd, git_base)
    directory = delete_root(cfg) / rel_dir_for(repo_label, git_base)
    name = src.name if src is not None and src.exists() else job_filename(session_id, aim)
    dest = _unique_path(directory / name)
    session_jt: str = session.job_type or "claude"  # type: ignore[attr-defined]
    session_nc: bool = bool(session.no_codex)  # type: ignore[attr-defined]
    session_ov: str = session.llm_overseer or DEFAULT_LLM  # type: ignore[attr-defined]
    session_ex: str = session.llm_exec or DEFAULT_LLM  # type: ignore[attr-defined]
    _atomic_write(
        dest,
        serialize(
            session_id=session_id,
            aim=(job.aim if job else "") or aim,
            status="deleted",
            repo=repo_label,
            job_type=(job.job_type if job and job.job_type in JOB_TYPES else session_jt),
            no_codex=(job.no_codex if job else False) or session_nc,
            llm_overseer=(
                job.llm_overseer if job and job.llm_overseer in LLM_CHOICES else session_ov
            ),
            llm_exec=(job.llm_exec if job and job.llm_exec in LLM_CHOICES else session_ex),
            start_when=(job.start_when if job else "") or (session.start_when or ""),  # type: ignore[attr-defined]
            start_date=(job.start_date if job else "") or (session.start_date or ""),  # type: ignore[attr-defined]
            depends_on=(job.depends_on if job else "") or (session.depends_on or ""),  # type: ignore[attr-defined]
            deadline=(job.deadline if job else "") or (session.deadline or ""),  # type: ignore[attr-defined]
            created=(job.created if job else "") or _today_iso(),
            deleted=_today_iso(),
            account=(job.account if job else "") or _account_label(session.config_dir or ""),  # type: ignore[attr-defined]
            prompt=(job.prompt if job else None) or session.prompt,  # type: ignore[attr-defined]
            repo_options=_repo_options(),
        ),
    )
    if src is not None and src.exists() and not _same(src, dest):
        try:
            src.unlink()
        except OSError:
            pass
    store.update_fields(session_id, future_file=_vault_relpath(cfg, dest), future_missing_since=0)
    return dest


# ---------------------------------------------------------------------------
# pad
# ---------------------------------------------------------------------------
def pad_template(cfg: config.Config) -> str:  # pylint: disable=unused-argument
    """Blank capture-pad content: no aim/prompt, ``status: draft``, empty session_id.

    Takes *cfg* for API symmetry with :func:`reset_pad` (and Phase 3a's ``ccc new-prompt``);
    the template itself needs only the live repo options and today's date.
    """
    return serialize(
        session_id="",
        aim="",
        status="draft",
        repo="",
        job_type="claude",
        start_when="",
        start_date="",
        deadline="",
        created=_today_iso(),
        prompt=None,
        repo_options=_repo_options(),
    )


def reset_pad(cfg: config.Config) -> None:
    """Rewrite the capture pad from the blank template (fresh injected repo options)."""
    _atomic_write(pad_path(cfg), pad_template(cfg))


def _handle_pad(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    cfg: config.Config,
    git_base: Path,
    repo_options: list[str],
    taken: set[str],
    report: SyncReport,
) -> None:
    """Register the pad's content on ``status: ready`` (fresh UUID), then reset the pad.

    Self-heals a missing pad: a deleted capture pad is recreated blank from
    :func:`pad_template` (never an error), so the manual capture path always exists.
    """
    pad = pad_path(cfg)
    if not pad.exists():
        reset_pad(cfg)  # self-heal: recreate the blank pad, then leave it for next pass
        return
    try:
        text = pad.read_text(encoding="utf-8")
    except OSError:
        return
    job = parse_job_file(text)
    # A ticked launch toggle on a draft pad implies "ready": the phone flow is
    # write AIM/prompt + tap the launch checkbox — no status edit needed. An
    # "error" pad with launch still ticked retries the same way, so fixing the
    # offending field is enough — no status reset. Retrigger-safe: a still-
    # invalid pad rewrites an identical error block (idempotent, no mtime churn).
    if job.status != "ready" and not (job.status in ("draft", "error") and launch_requested(job)):
        return  # draft / blank pad — nothing to register
    # Pad-only default: an empty repo means "run in $HOME" — the phone flow is
    # often just an AIM + the launch tick. A blank repo in a copied job FILE
    # still errors (there it is more likely a mistake than an intent).
    padjob = dataclasses.replace(
        job,
        session_id=_fresh_uuid(taken),
        status="ready",
        repo=(job.repo.strip() or str(Path.home())),
    )
    errors = validate(padjob, git_base)
    if errors:
        _write_error(pad, text, errors, report)
        return
    _register(store, cfg, padjob, git_base, repo_options, report, source=None)
    reset_pad(cfg)


# ---------------------------------------------------------------------------
# registration (ready file / pad → new draft row)
# ---------------------------------------------------------------------------
def _register(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    store: Store,
    cfg: config.Config,
    job: ParsedJob,
    git_base: Path,
    repo_options: list[str],
    report: SyncReport,
    *,
    source: Path | None,
) -> Path | None:
    """Create the draft row for a valid ready *job*, write its canonical file, freeze the slug.

    *source* (the ready file being registered) is removed once the canonical file exists;
    ``None`` for the pad (which is reset separately). An invalid job writes the managed
    error block on *source* (never on the pad) and makes no DB change.
    """
    errors = validate(job, git_base)
    if errors:
        if source is not None:
            _write_error(source, source.read_text(encoding="utf-8"), errors, report)
        return None
    session_id = job.session_id
    cwd = repo_to_cwd(job.repo, git_base)
    from . import routing  # pylint: disable=import-outside-toplevel

    store.create_draft(
        session_id,
        str(cwd),
        job.aim,
        prompt=job.prompt,
        deadline=(job.deadline.strip() or None),
        start_when=(job.start_when.strip() or None),
        start_date=(job.start_date.strip() or None),
        depends_on=(job.depends_on.strip() or None),
        job_type=job.job_type,
        no_codex=job.no_codex,
        llm_overseer=job.llm_overseer,
        llm_exec=job.llm_exec,
        # No `account:` in the file ⇒ route this NEW job per the job_account policy.
        config_dir=_account_config_dir(job.account) or routing.pick_job_account()[1],
    )
    _spawn_aim_jobs(cfg, session_id)
    directory = future_root(cfg) / rel_dir_for(job.repo, git_base)
    dest = directory / job_filename(session_id, job.aim)
    session = store.get(session_id)
    canonical = _serialize_registered(
        session,
        cwd_to_repo(str(cwd), git_base),
        repo_options,
        created=(job.created or _today_iso()),
        status="registered",
    )
    _atomic_write(dest, canonical)
    if source is not None and not _same(source, dest):
        try:
            source.unlink()
        except OSError:
            pass
    store.update_fields(
        session_id,
        future_file=_vault_relpath(cfg, dest),
        future_sync_hash=content_hash(canonical),
        future_synced_at=now_ms(),
        future_missing_since=0,
    )
    report.registered.append(session_id)
    if launch_requested(job):
        _consume_launch(store, cfg, session_id, report)
    return dest


# ---------------------------------------------------------------------------
# duplicate (copied job file → a NEW job)
# ---------------------------------------------------------------------------
def _handle_duplicate(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    cfg: config.Config,
    file: Path,
    job: ParsedJob,
    git_base: Path,
    repo_options: list[str],
    taken: set[str],
    report: SyncReport,
) -> None:
    """A file whose UUID duplicates another file/row is a copy → assign a fresh UUID.

    Rewrite its frontmatter (id + session_id), rename to the new canonical filename, and —
    if it was ``ready``/``registered`` — register it as a brand-new job; else leave it a draft.
    """
    new_id = _fresh_uuid(taken)
    new_status = "ready" if job.status in ("ready", "registered") else "draft"
    new_text = serialize(
        session_id=new_id,
        aim=job.aim,
        status=new_status,
        repo=job.repo,
        job_type=(job.job_type if job.job_type in JOB_TYPES else "claude"),
        no_codex=job.no_codex,
        llm_overseer=(job.llm_overseer if job.llm_overseer in LLM_CHOICES else DEFAULT_LLM),
        llm_exec=(job.llm_exec if job.llm_exec in LLM_CHOICES else DEFAULT_LLM),
        start_when=job.start_when,
        start_date=job.start_date,
        depends_on=job.depends_on,
        deadline=job.deadline,
        created=(job.created or _today_iso()),
        account=job.account,
        prompt=job.prompt,
        repo_options=repo_options,
    )
    dest = file.parent / job_filename(new_id, job.aim)
    _atomic_write(dest, new_text)
    if not _same(dest, file):
        try:
            file.unlink()
        except OSError:
            pass
    report.details.append(
        f"duplicate {display_hash(job.session_id)} → fresh {display_hash(new_id)}"
    )
    if new_status == "ready":
        _register(store, cfg, parse_job_file(new_text), git_base, repo_options, report, source=dest)


# ---------------------------------------------------------------------------
# registered file: export (DB→file) or import (file→DB)
# ---------------------------------------------------------------------------
def _handle_registered(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    cfg: config.Config,
    file: Path,
    text: str,
    hsh: str,
    job: ParsedJob,
    git_base: Path,
    repo_options: list[str],
    report: SyncReport,
) -> None:
    """A file that belongs to a known draft row: export a DB change, or import a file edit."""
    session = store.get(job.session_id)
    if session is None:  # row vanished mid-run — leave the file for a later pass
        return
    if hsh == (session.future_sync_hash or ""):
        # Untouched: regenerate canonical from the DB. If a DB-side change makes it differ,
        # export it; else write nothing (echo suppression). Preserve any conflict note so a
        # DB-driven re-export stays a fixed point.
        canonical = _serialize_registered(
            session,
            cwd_to_repo(session.cwd, git_base),
            repo_options,
            created=job.created,
            status="registered",
        )
        if _has_conflict_note(text):
            canonical = _with_conflict_note(canonical)
        if canonical != text:
            _atomic_write(file, canonical)
            store.update_fields(
                session.session_id,
                future_sync_hash=content_hash(canonical),
                future_synced_at=now_ms(),
                future_missing_since=0,
            )
            report.exported.append(session.session_id)
        elif session.future_missing_since:
            store.update_fields(session.session_id, future_missing_since=0)
        return
    # Hash differs → the user edited the file → import (file wins).
    errors = validate(job, git_base)
    if errors:
        _write_error(file, text, errors, report)  # no DB write until fixed + re-flipped
        return
    _import_file_wins(store, cfg, file, job, session, git_base, repo_options, report)


def _import_file_wins(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches
    store: Store,
    cfg: config.Config,
    file: Path,
    job: ParsedJob,
    session: object,
    git_base: Path,
    repo_options: list[str],
    report: SyncReport,
) -> None:
    """Apply the file's values to the DB (only where they differ), then rewrite canonical."""
    session_id: str = session.session_id  # type: ignore[attr-defined]
    updated_at: int = session.updated_at  # type: ignore[attr-defined]
    synced_at: int = session.future_synced_at  # type: ignore[attr-defined]
    conflict = (updated_at - synced_at) > _SYNC_SLOP_MS
    changed: list[str] = []

    # Cycle guard (file wins EXCEPT a cyclic dependency): a file edit making this job depend
    # on itself (directly or transitively) is rejected wholesale — write the managed
    # sync-error callout and import NOTHING (validate can't see cycles; it needs the DB).
    new_depends = job.depends_on.strip()
    cur_depends: str = session.depends_on or ""  # type: ignore[attr-defined]
    if new_depends != cur_depends and deps.would_create_cycle(store.get, session_id, new_depends):
        current = file.read_text(encoding="utf-8")
        _write_error(
            file,
            current,
            [f"depends_on {new_depends} would create a dependency cycle — not imported"],
            report,
        )
        return

    new_aim = job.aim.strip()
    if new_aim != (session.aim or ""):  # type: ignore[attr-defined]
        store.set_aim(session_id, new_aim)  # history + score chokepoint
        _spawn_aim_jobs(cfg, session_id)
        changed.append("aim")

    new_prompt = job.prompt.strip() if job.prompt else None
    if new_prompt != (session.prompt or None):  # type: ignore[attr-defined]
        store.update_fields(session_id, prompt=new_prompt)
        changed.append("prompt")

    new_start = job.start_when.strip() or None
    if new_start != (session.start_when or None):  # type: ignore[attr-defined]
        store.update_fields(session_id, start_when=new_start)
        changed.append("start_when")

    new_start_date = job.start_date.strip() or None
    if new_start_date != (session.start_date or None):  # type: ignore[attr-defined]
        store.update_fields(session_id, start_date=new_start_date)
        changed.append("start_date")

    # Dependency (cyclic edits already rejected above): import a differing well-formed value.
    new_depends_val = new_depends or None
    if new_depends_val != (session.depends_on or None):  # type: ignore[attr-defined]
        store.update_fields(session_id, depends_on=new_depends_val)
        changed.append("depends_on")

    new_deadline = job.deadline.strip() or None
    if new_deadline != (session.deadline or None):  # type: ignore[attr-defined]
        store.update_fields(session_id, deadline=new_deadline)
        changed.append("deadline")

    new_jobtype = job.job_type if job.job_type in JOB_TYPES else "claude"
    if new_jobtype != (session.job_type or "claude"):  # type: ignore[attr-defined]
        store.update_fields(session_id, job_type=new_jobtype)
        changed.append("job_type")

    # validate() already refused a no_codex + codex-job-type file, so the pair below
    # can never land in the DB in a conflicting combination.
    if job.no_codex != bool(session.no_codex):  # type: ignore[attr-defined]
        store.update_fields(session_id, no_codex=job.no_codex)
        changed.append("no_codex")

    new_overseer = job.llm_overseer if job.llm_overseer in LLM_CHOICES else DEFAULT_LLM
    if new_overseer != (session.llm_overseer or DEFAULT_LLM):  # type: ignore[attr-defined]
        store.update_fields(session_id, llm_overseer=new_overseer)
        changed.append("llm_overseer")

    new_exec = job.llm_exec if job.llm_exec in LLM_CHOICES else DEFAULT_LLM
    if new_exec != (session.llm_exec or DEFAULT_LLM):  # type: ignore[attr-defined]
        store.update_fields(session_id, llm_exec=new_exec)
        changed.append("llm_exec")

    # Account (config_dir): the file's `account:` label wins. `validate` already ran, so
    # `job.account` is empty or a configured label; empty ⇒ the default account (same
    # `or claude_home()` idiom create_draft uses). same_config_dir absorbs any spelling
    # difference so a no-op edit never churns. Note: an empty account at EDIT deliberately
    # stays the default account (NOT re-routed via routing.pick_job_account) — job_account
    # routing is a creation-time decision, and re-evaluating it on every sync would churn
    # the stamp of a job the user is merely editing.
    from . import accounts  # pylint: disable=import-outside-toplevel

    new_account_dir = _account_config_dir(job.account) or str(config.claude_home())
    cur_dir: str = session.config_dir or ""  # type: ignore[attr-defined]
    if not accounts.same_config_dir(new_account_dir, cur_dir):
        store.update_fields(session_id, config_dir=new_account_dir)
        changed.append("account")

    # Repo change is the only edit that moves the file (slug frozen; AIM edits never rename).
    new_cwd = repo_to_cwd(job.repo, git_base)
    cur_cwd: str = session.cwd  # type: ignore[attr-defined]
    if job.repo.strip() and os.path.normpath(str(new_cwd)) != os.path.normpath(cur_cwd):
        store.update_fields(session_id, cwd=str(new_cwd))
        new_dir = future_root(cfg) / rel_dir_for(job.repo, git_base)
        new_dir.mkdir(parents=True, exist_ok=True)
        new_path = new_dir / file.name
        if not _same(new_path, file):
            os.replace(file, new_path)
            file = new_path
        store.update_fields(session_id, future_file=_vault_relpath(cfg, file))
        changed.append("repo")

    fresh = store.get(session_id)
    assert fresh is not None
    canonical = _serialize_registered(
        fresh,
        cwd_to_repo(fresh.cwd, git_base),
        repo_options,
        created=job.created,
        status="registered",
    )
    canonical = _with_conflict_note(canonical) if conflict else _strip_conflict_note(canonical)
    _atomic_write(file, canonical)
    store.update_fields(
        session_id,
        future_sync_hash=content_hash(canonical),
        future_synced_at=now_ms(),
        future_missing_since=0,
    )
    report.imported.append(session_id)
    if conflict:
        report.details.append(f"conflict (file wins) {session_id}: {','.join(changed) or 'no-op'}")
    if launch_requested(job):
        _consume_launch(store, cfg, session_id, report)


# ---------------------------------------------------------------------------
# per-file dispatch
# ---------------------------------------------------------------------------
def _owner(owners: list[Path], canon: Path | None) -> Path:
    """The canonical owner among files sharing a UUID: the DB's file, else the first path."""
    if canon is not None:
        for path in owners:
            if _same(path, canon):
                return path
    return sorted(owners, key=str)[0]


def _process_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-return-statements
    store: Store,
    cfg: config.Config,
    file: Path,
    text: str,
    job: ParsedJob,
    ctx: _Context,
    report: SyncReport,
) -> None:
    """Classify a scanned file and route it (registered / adopt / duplicate / register / ignore)."""
    hsh = content_hash(text)
    sid = job.session_id.strip()

    if not _is_uuid(sid):
        if job.status == "ready":
            _register(store, cfg, job, ctx.git_base, ctx.repo_options, report, source=file)
        else:
            ctx.sidecar[str(file)] = hsh
        return

    row = ctx.db_by_id.get(sid)
    canon = ctx.canonical_abs.get(sid)

    is_dup = row is not None and canon is not None and canon.exists() and not _same(canon, file)
    owners = ctx.scanned_uuids.get(sid, [])
    if len(owners) > 1 and not _same(_owner(owners, canon), file):
        is_dup = True
    if is_dup:
        _handle_duplicate(store, cfg, file, job, ctx.git_base, ctx.repo_options, ctx.taken, report)
        return

    if row is not None:
        if canon is None or _same(canon, file):
            _handle_registered(
                store, cfg, file, text, hsh, job, ctx.git_base, ctx.repo_options, report
            )
        else:  # canonical path is missing → this file carries the UUID (rename detected here)
            store.update_fields(sid, future_file=_vault_relpath(cfg, file), future_missing_since=0)
            _handle_registered(
                store, cfg, file, text, hsh, job, ctx.git_base, ctx.repo_options, report
            )
        return

    # Unknown session id: a ready file registers as a new job; a draft is ignored (sidecar).
    if job.status == "ready":
        _register(store, cfg, job, ctx.git_base, ctx.repo_options, report, source=file)
    else:
        ctx.sidecar[str(file)] = hsh


# ---------------------------------------------------------------------------
# DB side
# ---------------------------------------------------------------------------
def _bootstrap_export(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: Store,
    cfg: config.Config,
    session: object,
    git_base: Path,
    repo_options: list[str],
    report: SyncReport,
) -> None:
    """Export a draft row that has no file yet (bootstraps every pre-existing FUTURE row)."""
    cwd: str = session.cwd  # type: ignore[attr-defined]
    aim: str = session.aim or ""  # type: ignore[attr-defined]
    session_id: str = session.session_id  # type: ignore[attr-defined]
    repo_label = cwd_to_repo(cwd, git_base)
    directory = future_root(cfg) / rel_dir_for(repo_label, git_base)
    dest = directory / job_filename(session_id, aim)
    canonical = _serialize_registered(
        session, repo_label, repo_options, created=_today_iso(), status="registered"
    )
    _atomic_write(dest, canonical)
    store.update_fields(
        session_id,
        future_file=_vault_relpath(cfg, dest),
        future_sync_hash=content_hash(canonical),
        future_synced_at=now_ms(),
        future_missing_since=0,
    )
    report.exported.append(session_id)


def _db_side(store: Store, cfg: config.Config, ctx: _Context, report: SyncReport) -> None:
    """For each live draft: export a fileless row; grace/archive a missing one (rename-aware)."""
    now = now_ms()
    grace_ms = cfg.future_delete_grace_sec * 1000
    for session in store.list_sessions():  # non-archived only
        if not session.draft:
            continue
        # A done-but-not-archived draft must leave the FUTURE view (the TUI hides done
        # rows by default): mirror what cmd_mark_done now does — archive its file and the
        # row — so the folder stays 1:1 with the visible FUTURE list.
        if session.done and not session.archived:
            if session.future_file:
                archive_file(store, cfg, session, "archived")
            store.update_fields(session.session_id, archived=True)
            report.archived.append(session.session_id)
            continue
        if not session.future_file:
            _bootstrap_export(store, cfg, session, ctx.git_base, ctx.repo_options, report)
            continue
        abs_path = _abs_path(cfg, session.future_file)
        if abs_path.exists():
            if session.future_missing_since:  # reappeared → clear the grace clock
                store.update_fields(session.session_id, future_missing_since=0)
            continue
        relocated = _first_existing(ctx.scanned_uuids.get(session.session_id, []))
        if relocated is not None:  # rename/move detected → repoint, do not archive
            store.update_fields(
                session.session_id,
                future_file=_vault_relpath(cfg, relocated),
                future_missing_since=0,
            )
            report.details.append(f"relocated {session.session_id}")
            continue
        if not session.future_missing_since:  # start the grace clock
            store.update_fields(session.session_id, future_missing_since=now)
        elif now - session.future_missing_since > grace_ms:  # past grace → archive the draft
            store.update_fields(session.session_id, archived=True)
            report.archived.append(session.session_id)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
@dataclass
class _Context:
    """Shared per-run lookups threaded through the handlers."""

    git_base: Path
    repo_options: list[str]
    db_by_id: dict[str, object]
    canonical_abs: dict[str, Path]
    scanned_uuids: dict[str, list[Path]]
    taken: set[str]
    sidecar: dict[str, str]


def run_sync(  # pylint: disable=too-many-locals
    store: Store, cfg: config.Config, only_file: Path | None = None
) -> SyncReport:
    """Reconcile the FUTURE-job draft rows with their Obsidian files (idempotent, flock-guarded).

    *only_file* runs a targeted single-file import (Phase 3a's ``start-job`` uses it to pick
    up last-minute edits) — the pad and the DB-side bootstrap/grace passes are skipped. The
    flock singleton makes a concurrent invocation return an empty report immediately.
    """
    report = SyncReport()
    lock = _acquire_lock()
    if lock is None:  # another sync run holds the singleton — leave it to that one
        return report
    try:
        git_base = repos.git_base()
        repo_options = _repo_options()
        sidecar = _load_sidecar()

        sessions = store.list_sessions()
        db_by_id: dict[str, object] = {s.session_id: s for s in sessions if s.draft}
        canonical_abs = {
            sid: _abs_path(cfg, s.future_file)  # type: ignore[attr-defined]
            for sid, s in db_by_id.items()
            if s.future_file  # type: ignore[attr-defined]
        }
        taken = {display_hash(s.session_id) for s in sessions}

        if only_file is not None:
            files = [only_file] if only_file.exists() else []
        else:
            files = _scan_files(cfg)

        parsed: list[tuple[Path, str, ParsedJob]] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed.append((path, text, parse_job_file(text)))

        # Migrate any legacy ``<hash>-<slug>.md`` files to ``<slug>-<hash>.md`` in place
        # (mutates *parsed* + canonical_abs + DB future_file) before classification.
        _migrate_legacy_names(store, cfg, parsed, db_by_id, canonical_abs, report)

        scanned_uuids: dict[str, list[Path]] = {}
        for path, _text, job in parsed:
            sid = job.session_id.strip()
            if _is_uuid(sid):
                scanned_uuids.setdefault(sid, []).append(path)
                taken.add(display_hash(sid))

        ctx = _Context(
            git_base=git_base,
            repo_options=repo_options,
            db_by_id=db_by_id,
            canonical_abs=canonical_abs,
            scanned_uuids=scanned_uuids,
            taken=taken,
            sidecar=sidecar,
        )

        for path, text, job in parsed:
            _process_file(store, cfg, path, text, job, ctx, report)

        if only_file is None:
            _handle_pad(store, cfg, git_base, repo_options, taken, report)
            _db_side(store, cfg, ctx, report)

        _prune_sidecar(sidecar)
        _save_sidecar(sidecar)
        return report
    finally:
        _release_lock(lock)
