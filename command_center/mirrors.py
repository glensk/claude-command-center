#!/usr/bin/env python3
"""Export-only markdown mirrors of RUNNING and DONE sessions in the Obsidian vault.

Every tracked ccc session already surfaces as ONE markdown file in the vault: a
FUTURE draft is the bidirectional file owned by :mod:`command_center.futuresync`;
this module adds the other two lifecycle phases as **export-only read mirrors**:

* **RUNNING** — ``running_dir/<cat>/<repo>/<slug>-<hash>.md`` for every active
  (``draft=0 AND archived=0 AND done=0``) session — a live readout of its AIM,
  progress, next step, summary and transcript pointer.
* **DONE** — ``done_dir/<cat>/<repo>/<slug>-<hash>.md`` for every finished
  (``done=1 AND draft=0``) session — a final snapshot. A cancelled future job (a
  ``draft`` marked done) stays only in ``future/_archive/`` and is NEVER mirrored
  here.
* **SESSION** — ``sessions_dir/<cat>/<repo>/<slug>-<hash>.md`` for every RUNNING
  **or** DONE session: the full conversation (prompts, replies, ⏺/⎿ tool lines)
  rendered by :mod:`command_center.sessionmd` — the SAME segments the ``ccc peek``
  panel's session tab shows. ONE stable path across the session's whole life
  (running → done), so the ``full session`` wikilink embedded in the running/done
  mirrors' ``## Transcript`` section never breaks when a job finishes. Membership
  is derived directly from the store and is INDEPENDENT of the
  ``mirror_running``/``mirror_done`` kill-switches (own switch:
  ``mirror_sessions``).

Design invariants (do not regress; see ``PLAN_running-done-mirrors.md``):

* **Export-only.** The DB is the sole source of truth. A banner comment + the
  ``ccc_mirror`` frontmatter mark the files generated; user edits are overwritten.
* **Byte-stable.** Content is a pure function of the session's stable fields —
  the only timestamps embedded are ``created_at`` / ``last_response_at`` /
  ``done_at`` (rendered as ISO dates), never ``updated_at`` or a "generated at"
  stamp. Export = regenerate → compare bytes → atomic write ONLY on a real change,
  so a routine no-op pass writes nothing (no vault git churn).
* **``ccc_mirror``-guarded cleanup.** Cleanup scans ONLY the generated roots and
  removes/moves ONLY files whose frontmatter carries ``ccc_mirror`` (matching the
  root's kind). A stale/moved mirror (its session left the set, or its filename
  changed) is removed; a file WITHOUT the marker is NEVER touched.
* **Collision-safe filenames.** ``<slug>-<hash>.md`` with a 4-hex UUID prefix,
  deterministically extended to 8 hex when two sessions in the same directory
  share the 4-hex prefix. The full UUID in the frontmatter is the identity.
* **Scrubbed, fail closed.** These files embed prompts, replies and tool output
  VERBATIM, so every document passes through the external scrubber
  (:mod:`command_center.scrub`, ``mirror_scrub_cmd``) before it is written. No
  vouch → no write: the card on disk stays as it is (stale but safe) and the
  reason lands in ``report.withheld``. A withheld card also freezes cleanup for
  the whole pass, so the last readable copy is never removed to make room for
  bytes that were refused. ``mirror_allow_unscrubbed = true`` is the ONE opt-out.
* **Vouched.** Each write is receipted in the ``mirror_vouch`` table (regenerated
  document, written bytes, policy, timestamp — see
  :class:`command_center.models.MirrorVouch`), so a steady-state pass spawns NO
  scrubber at all: only a changed session, an edited/missing file, a changed
  ``mirror_scrub_cmd`` or an expired row (:data:`VOUCH_TTL_MS`) costs a call. The
  per-pass budgets keep a cold cache from monopolising a daemon pass — the cards
  that do not fit are ``report.deferred`` and keep their current file.
* **Own flock singleton.** ``app_home()/mirror_sync.lock`` serialises concurrent
  runs (the daemon backstop + the detached ``ccc sync-mirrors`` lifecycle spawns).
"""

# pylint: disable=too-many-lines  # the scrubber gate doubled the module; one concern, one file

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import contextlib
import fcntl
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import yaml

from . import config, repos, scrub
from .adapters import ClaudeAdapter
from .future_files import (
    cwd_to_repo,
    display_hash,
    rel_dir_for,
    slugify,
)
from .models import (
    DEFAULT_LLM,
    AimRevision,
    MirrorHealth,
    MirrorVouch,
    Session,
    TranscriptScan,
    iso_date,
    model_label,
    now_ms,
    synthesize_aim_revision,
)
from .peek import session_prompts
from .sessionmd import escape_fences, render_for
from .store import Store

# The three mirror kinds — the ``ccc_mirror`` frontmatter value AND the folder role.
RUNNING = "running"
DONE = "done"
SESSION = "session"

# Fixed "this file is generated" banner (byte-stable, sits right after the frontmatter).
_BANNER = (
    "<!-- GENERATED by ccc — export-only mirror of a command-center session. "
    "Edits are overwritten; change the session, not this file. -->"
)

# Leading YAML frontmatter fence (must be at the very start of the file) — a local copy
# so this module never imports future_files' private regex.
_FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)

# ``## Prompts`` caps (pathological-session guard): keep at most the last N prompts and
# cap the rendered body at N bytes. The most recent are kept; a note flags any trim.
# Individual prompt text is never truncated — whole older prompts are dropped instead.
_PROMPTS_MAX = 200
_PROMPTS_MAX_BYTES = 256 * 1024

# Per-session prompt cache: session id → (transcript mtime, prompts). Walking a transcript
# (JSON-parse every line) is the one expensive part of a mirror pass, and the DONE set is
# ~100 *frozen* transcripts — re-reading each every 300 s pass is pure waste. The long-lived
# daemon process persists this across passes, so an unchanged transcript (mtime unmoved) is
# read at most once; a growing RUNNING transcript (or any real edit) re-reads. Byte-stable:
# the same mtime yields the same prompts, hence the same bytes.
_PROMPT_CACHE: dict[str, tuple[float, list[str]]] = {}

# How long one scrubber verdict speaks for a card. The vouch already pins the document,
# the bytes on disk and the policy, so the TTL only bounds how long a RULE-SET change the
# policy string did not capture (the broker's own patterns) can stay unnoticed: a full
# re-scrub of every unchanged card, spread over the revouch budget, once a day.
VOUCH_TTL_MS = 24 * 3600 * 1000
# Wall-clock budgets for the scrubber calls of ONE pass (``full=True`` lifts both). The
# REQUIRED budget covers cards with no usable vouch (new/changed/legacy) — those must be
# scrubbed before they can be written at all; the much smaller REVOUCH budget re-vouches
# expired-but-unchanged cards in the background, oldest first, so a daemon pass is never
# monopolised by the day's TTL sweep.
REQUIRED_BUDGET_S = 120.0
REVOUCH_BUDGET_S = 20.0
# Per-document scrub timeout, read at call time so a test can shorten it.
_SCRUB_TIMEOUT_S = scrub.SCRUB_TIMEOUT_S
# How long :func:`remove_mirror` waits for the flock before leaving the removal to the
# next sync pass (it is a synchronous lifecycle call — it must not hang a command).
_REMOVE_LOCK_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
@dataclass
class MirrorReport:  # pylint: disable=too-many-instance-attributes  # per-pass tally
    """What one :func:`run_mirrors` pass saw and did.

    ``running`` / ``done`` are the FULL current membership of each set (their mirrors
    exist after this pass); ``written`` / ``removed`` are the ACTUAL changes this pass
    made (a byte-stable no-op leaves both empty).

    The scrubber half is path-keyed (a session has up to three cards, each vouched on
    its own): ``vouched`` cost a scrubber call this pass, ``scrubbed`` are the ones it
    actually CHANGED (with the labels it reported — names only, never values),
    ``withheld`` were refused (fail closed: the file on disk is untouched) and
    ``deferred`` did not fit this pass's budget (also untouched, and re-tried next pass).
    """

    running: list[str] = field(default_factory=list)  # session ids currently in RUNNING
    done: list[str] = field(default_factory=list)  # session ids currently in DONE
    sessions: list[str] = field(default_factory=list)  # session ids with a SESSION mirror
    written: list[str] = field(default_factory=list)  # session ids whose file was (re)written
    removed: list[str] = field(default_factory=list)  # abs paths of stale/moved mirrors removed
    vouched: list[str] = field(default_factory=list)  # paths a scrubber vouched for this pass
    scrubbed: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)  # (path, labels)
    withheld: list[tuple[str, str]] = field(default_factory=list)  # (path, reason) — not written
    deferred: list[str] = field(default_factory=list)  # paths skipped by the pass budget
    details: list[str] = field(default_factory=list)  # free-text (relocations, collisions …)

    def changed(self) -> int:
        """Number of real disk changes this pass (0 = a byte-stable no-op)."""
        return len(self.written) + len(self.removed)


# ---------------------------------------------------------------------------
# roots + small helpers
# ---------------------------------------------------------------------------
def running_root(cfg: config.Config) -> Path:
    """Expanded root of the RUNNING session mirrors (``running_dir`` config key)."""
    return config.guard_vault_path(Path(cfg.running_dir).expanduser())


def done_root(cfg: config.Config) -> Path:
    """Expanded root of the DONE session mirrors (``done_dir`` config key)."""
    return config.guard_vault_path(Path(cfg.done_dir).expanduser())


def sessions_root(cfg: config.Config) -> Path:
    """Expanded root of the full-session mirrors (``sessions_dir`` config key)."""
    return config.guard_vault_path(Path(cfg.sessions_dir).expanduser())


def _hash(session_id: str, length: int) -> str:
    """First *length* hex chars of the session UUID (falls back to a padded slug hash)."""
    try:
        return uuid.UUID(str(session_id)).hex[:length]
    except (ValueError, AttributeError, TypeError):
        return display_hash(session_id)[:length].ljust(length, "0")


def _q(value: object) -> str:
    """A double-quoted, flat YAML scalar (escaping ``\\`` and ``"``) — round-trip safe."""
    text = "" if value is None else str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _observed_model_label(
    adapter: ClaudeAdapter, session: Session, scan: TranscriptScan | None = None
) -> str:
    """The session's OBSERVED model (from its transcript) as a ccc choice label, else "".

    *scan* is the session's transcript-scan row for THIS pass (already refreshed against
    the file by :func:`_sync_root`), so the label is read off the persisted fact and a
    frozen transcript costs one ``stat()`` instead of a tail read per mirror. Without one
    (a stub adapter with no ``scan_transcript``) it falls back to the live probe.

    ``observed_model`` is a concrete-adapter capability (like ``has_background_task`` /
    ``uses_codex_workflow``), NOT part of the ``Adapter`` protocol — probe it defensively
    so a stub adapter degrades to "". Any read error also degrades to "" (including the
    ``OSError`` the transcript readers raise — a read failure is not the fact "no model";
    see ``scan_transcript`` § Property P): a mirror write must never crash on a malformed
    transcript. Byte-stable: the observed model is fixed for a frozen transcript and
    changes only when the session actually switches models.
    """
    if scan is not None:
        return model_label(scan.model)
    fn = getattr(adapter, "observed_model", None)
    if fn is None:
        return ""
    try:
        return model_label(fn(session.cwd, session.session_id))
    except Exception:  # pylint: disable=broad-exception-caught
        # Contains the OSError the transcript readers raise: a read failure is not the
        # fact "no model" (see scan_transcript § Property P), and a mirror write must
        # never crash on it.
        return ""


def _read_frontmatter(text: str) -> dict[str, str]:
    """The frontmatter mapping of a mirror file as flat strings (``{}`` when absent/bad)."""
    match = _FM_RE.match(text)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): ("" if val is None else str(val)) for key, val in data.items()}


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file in the same dir + :func:`os.replace`).

    Only VOUCHED bytes ever reach this function (see :class:`_ScrubPass`), and the temp
    file is unique per call and **dot-prefixed** — so a crash between the two steps can
    never leave a half-written document where a mirror belongs, two concurrent writers
    cannot share a temp name, and :func:`_iter_mirror_files` (which skips ``.``-prefixed
    components) never treats a leftover as a mirror. Any failure unlinks the temp file
    and re-raises: the caller's next pass rewrites the card.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    done = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.replace(tmp, path)
        done = True
    finally:
        if not done:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _read_disk(path: str) -> str | None:
    """Current text of the mirror at *path*, or ``None`` when it is absent/unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# prompts + AIM history sections (shared source, byte-stable)
# ---------------------------------------------------------------------------
def _prompts_for(adapter: ClaudeAdapter, session: Session) -> list[str]:
    """The session's prompts (:func:`command_center.peek.session_prompts`), mtime-cached.

    Reads the transcript only when it is new to this process or its mtime moved since the
    last read (see :data:`_PROMPT_CACHE`) — so a RUNNING transcript that grows every turn
    re-reads, while a frozen DONE transcript is read once. Returns the exact list the
    ``ccc peek`` panel shows (same helper), so the panel and the mirror never diverge.
    """
    tpath = adapter.transcript_path(session.cwd, session.session_id)
    if tpath is None:
        return []
    try:
        mtime = tpath.stat().st_mtime
    except OSError:
        return []
    cached = _PROMPT_CACHE.get(session.session_id)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    prompts = session_prompts(adapter, session)
    _PROMPT_CACHE[session.session_id] = (mtime, prompts)
    return prompts


def _numbered_item(index: int, text: str, *, tag: str = "") -> list[str]:
    """One markdown ordered-list item (``N. text``) as a list of lines.

    Multi-line *text* stays one valid list item: continuation lines are indented to the
    width of the ``N. `` marker (a blank line is emitted truly blank — a valid loose-item
    paragraph break). An optional *tag* becomes a parenthesised prefix (``N. (tag) text``),
    used by the AIM-history metadata.
    """
    marker = f"{index}. "
    indent = " " * len(marker)
    head = f"{marker}({tag}) " if tag else marker
    parts = text.split("\n")
    lines = [head + parts[0]]
    lines.extend(indent + part if part.strip() else "" for part in parts[1:])
    return lines


def _render_prompts(prompts: list[str]) -> str:
    """Numbered markdown list of the session's prompts, oldest first (matches ``ccc peek``).

    Guarded for a pathological session by two caps — at most the last :data:`_PROMPTS_MAX`
    prompts, and :data:`_PROMPTS_MAX_BYTES` of rendered body — keeping the most recent and
    prefixing a ``showing last N of M`` note when either trims. Individual prompt text is
    never truncated (whole older prompts are dropped instead).
    """
    total = len(prompts)
    if total == 0:
        return "(no prompts in this session yet)"
    window = prompts[-_PROMPTS_MAX:] if total > _PROMPTS_MAX else list(prompts)

    def _body(items: list[str]) -> str:
        lines: list[str] = []
        for index, text in enumerate(items, 1):
            # A pasted terminal snippet can open a code fence it never closes at
            # line start — which would swallow the whole rest of the file (headings,
            # the full-session wikilink …). Escape line-leading fence runs.
            lines.extend(_numbered_item(index, escape_fences(text)))
        return "\n".join(lines)

    body = _body(window)
    while len(window) > 1 and len(body.encode("utf-8")) > _PROMPTS_MAX_BYTES:
        window = window[1:]
        body = _body(window)
    if len(window) < total:
        body = f"_showing last {len(window)} of {total} prompts_\n\n{body}"
    return body


def _revisions_for(session: Session, revisions: list[AimRevision]) -> list[AimRevision]:
    """AIM history with the same pre-tracking fallback ``ccc peek`` / ``aim-history`` use.

    A session whose AIM predates history tracking has no rows; synthesise one from its live
    AIM so the ``## AIM history`` section (and the first-aim slug) are never empty.
    """
    if not revisions and session.aim:
        return [synthesize_aim_revision(session)]
    return revisions


def _render_aim_history(revisions: list[AimRevision]) -> str:
    """Numbered markdown list of the AIM progression — ``1.`` oldest → ``N.`` current.

    Mirrors ``ccc aim-history``'s content choices (score, current marker, short label) but
    embeds NO per-revision timestamp, so the file's only timestamps stay ``created`` /
    ``last_response`` / ``done_at`` (the byte-stability contract).
    """
    if not revisions:
        return "(no AIM set for this session yet)"
    total = len(revisions)
    lines: list[str] = []
    for index, rev in enumerate(revisions, 1):
        tag = f"{rev.score}%" if rev.score >= 0 else "—"
        if index == total:
            tag += " · current"
        lines.extend(_numbered_item(index, rev.aim, tag=tag))
        if rev.short_aim:
            lines.append(f"{' ' * len(f'{index}. ')}↳ short: {rev.short_aim}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# serialisation (canonical, byte-stable, export-only)
# ---------------------------------------------------------------------------
def _transcript_path(session: Session) -> Path:
    """The cwd-scoped Claude Code transcript path of *session* (may not exist)."""
    encoded = session.cwd.replace("/", "-")
    return config.claude_home() / "projects" / encoded / f"{session.session_id}.jsonl"


def _serialize_session(
    session: Session, git_base: Path, adapter: ClaudeAdapter, scan: TranscriptScan | None = None
) -> str:
    """Canonical SESSION-mirror content — the full rendered conversation.

    Same byte-stability contract as :func:`_serialize`: pure function of the session's
    stable fields plus the transcript (mtime-cached render in ``sessionmd``); the only
    timestamps are the ISO dates of ``created_at`` / ``last_response_at``. *scan* is this
    pass's transcript-scan row, threaded to :func:`_observed_model_label`.
    """
    fm_lines = [
        "---",
        f"ccc_mirror: {_q(SESSION)}",
        f"id: {_q(display_hash(session.session_id))}",
        f"session_id: {_q(session.session_id)}",
        f"status: {_q(session.status)}",
        f"repo: {_q(cwd_to_repo(session.cwd, git_base))}",
        # OBSERVED model from the transcript (see _observed_model_label); "" until a real
        # model is recorded. Same field as the running/done mirrors, for consistency.
        f"model: {_q(_observed_model_label(adapter, session, scan))}",
        # OBSERVED reasoning effort (the persisted session.effort observation, no probe).
        f"effort: {_q(session.effort or '')}",
        f"transcript: {_q(str(_transcript_path(session)))}",
        f"created: {_q(iso_date(session.created_at))}",
        f"last_response: {_q(iso_date(session.last_response_at))}",
    ]
    # Dependency (emitted only when set — byte-stable for dependency-less sessions).
    if session.depends_on:
        fm_lines.append(f"depends_on: {_q(session.depends_on)}")
    fm_lines.append("---")
    body = render_for(adapter, session).rstrip("\n")
    frontmatter = "\n".join(fm_lines)
    return f"{frontmatter}\n\n{_BANNER}\n\n{body}\n"


def _serialize(  # noqa: PLR0913  # pylint: disable=too-many-locals,too-many-arguments,too-many-positional-arguments
    store: Store,
    session: Session,
    kind: str,
    git_base: Path,
    adapter: ClaudeAdapter,
    revisions: list[AimRevision],
    session_link: str = "",
    scan: TranscriptScan | None = None,
) -> str:
    """Canonical mirror content for *session* (``kind`` = :data:`RUNNING` / :data:`DONE`).

    Pure function of the session's stable fields plus its transcript prompts — the only
    timestamps are the ISO dates of ``created_at`` / ``last_response_at`` / ``done_at`` —
    so two calls for an unchanged session (same fields, same transcript mtime) are
    byte-identical. *revisions* is the session's pre-fetched AIM history (shared with the
    slug computation); *adapter* reads the prompt transcript. *session_link* is the
    session mirror's vault-relative path (no extension) — when set, ``## Transcript``
    opens with a ``[[…|full session]]`` wikilink to the full-conversation file. *scan* is
    this pass's transcript-scan row, threaded to :func:`_observed_model_label`.
    """
    repo_label = cwd_to_repo(session.cwd, git_base)
    checked, total = store.progress(session.session_id)
    history = _revisions_for(session, revisions)
    subgoals = store.list_subgoals(session.session_id)
    transcript = _transcript_path(session)

    fm_lines = [
        "---",
        f"ccc_mirror: {_q(kind)}",
        f"id: {_q(display_hash(session.session_id))}",
        f"session_id: {_q(session.session_id)}",
        f"status: {_q(session.status)}",
        f"repo: {_q(repo_label)}",
        # Both links as Obsidian PROPERTIES so they sit at the very top of the note:
        # the full-session wikilink renders as a clickable chip in the properties UI.
        f"session: {_q(f'[[{session_link}|full session]]' if session_link else '')}",
        f"transcript: {_q(str(transcript))}",
        f"job_type: {_q(session.job_type or 'claude')}",
        f"llm_overseer: {_q(session.llm_overseer or DEFAULT_LLM)}",
        f"llm_exec: {_q(session.llm_exec or DEFAULT_LLM)}",
        # The OBSERVED model the session actually ran on (from the transcript) — unlike
        # llm_overseer/llm_exec, which are job-config defaults for a non-ccc-launched
        # session. "" when no real model has been recorded yet (mirrors deadline: "").
        f"model: {_q(_observed_model_label(adapter, session, scan))}",
        # The OBSERVED reasoning effort captured onto the session (session.effort); "" until
        # observed. Sits right after model:, both the runtime observations.
        f"effort: {_q(session.effort or '')}",
        f"importance: {_q(session.importance)}",
        f"deadline: {_q(session.deadline or '')}",
        f"created: {_q(iso_date(session.created_at))}",
        f"last_response: {_q(iso_date(session.last_response_at))}",
        f"progress: {_q(f'{checked}/{total}')}",
        f"drift: {_q(session.drift_severity or '')}",
    ]
    # Dependency (the job this one waits on): emitted only when set, so a dependency-less
    # mirror stays byte-identical.
    if session.depends_on:
        fm_lines.append(f"depends_on: {_q(session.depends_on)}")
    if kind == DONE:
        fm_lines.append(f"done_at: {_q(iso_date(session.done_at))}")
    fm_lines.append("---")
    frontmatter = "\n".join(fm_lines)

    # The top section always shows the FIRST recorded AIM (the user's original
    # done-condition) — never the running index — plus the CURRENT revision's short
    # label once the out-of-band generator has backfilled it. The full progression
    # stays in ## AIM history.
    aim_heading = "## AIM (1)" if history else "## AIM"
    aim_body = (history[0].aim if history else session.aim or "").strip()
    current_short = history[-1].short_aim if history else ""
    if current_short:
        aim_body = f"{aim_body}\n\n↳ short (current): {current_short}"
    next_body = (session.next_step or "").strip()
    summary_body = (session.summary or "").strip()
    blocked_body = (session.blocked_on or "").strip()
    sg_body = (
        "\n".join(f"- [{'x' if sg.checked else ' '}] {sg.text}" for sg in subgoals)
        if subgoals
        else ""
    )

    aim_history_body = _render_aim_history(history)
    prompts_body = _render_prompts(_prompts_for(adapter, session))

    transcript_body = f"`{transcript}`\n\nResume: `c --resume {session.session_id}`"
    if session_link:
        transcript_body = f"[[{session_link}|full session]]\n\n{transcript_body}"

    return (
        f"{frontmatter}\n\n"
        f"{_BANNER}\n\n"
        f"{aim_heading}\n\n{aim_body}\n\n"
        f"## AIM history\n\n{aim_history_body}\n\n"
        f"## Next step\n\n{next_body}\n\n"
        f"## Sub-goals\n\n{sg_body}\n\n"
        f"## Summary\n\n{summary_body}\n\n"
        f"## Blocked on\n\n{blocked_body}\n\n"
        f"## Prompts\n\n{prompts_body}\n\n"
        f"## Transcript\n\n{transcript_body}\n"
    )


# ---------------------------------------------------------------------------
# filenames (collision-safe: 4-hex prefix, extended to 8 on a same-dir clash)
# ---------------------------------------------------------------------------
def _target_paths(
    sessions: list[Session], root: Path, git_base: Path, first_aim: dict[str, str]
) -> dict[str, Session]:
    """Map each session to its canonical mirror path, extending clashing hashes 4→8.

    The slug comes from *first_aim* (the session's FIRST recorded AIM, else its current
    one) — NOT the current AIM — so the filename never churns when the AIM is sharpened
    mid-session (the engine's desired-path cleanup renames any stale current-aim-slugged
    mirror on the next pass). Two sessions whose 4-hex UUID prefixes collide **in the same
    directory** both get an 8-hex hash (decision 4); everyone else keeps the 4-hex prefix.
    Keyed by ``str(path)`` so cleanup can compare membership without path-object surprises.
    """
    by_dir: dict[Path, list[Session]] = {}
    for session in sessions:
        directory = root / rel_dir_for(cwd_to_repo(session.cwd, git_base), git_base)
        by_dir.setdefault(directory, []).append(session)
    out: dict[str, Session] = {}
    for directory, group in by_dir.items():
        prefixes: dict[str, list[Session]] = {}
        for session in group:
            prefixes.setdefault(_hash(session.session_id, 4), []).append(session)
        for session in group:
            length = 8 if len(prefixes[_hash(session.session_id, 4)]) > 1 else 4
            slug = slugify(first_aim.get(session.session_id, "") or "")
            name = f"{slug}-{_hash(session.session_id, length)}.md"
            out[str(directory / name)] = session
    return out


# ---------------------------------------------------------------------------
# write + cleanup
# ---------------------------------------------------------------------------
@dataclass
class _Card:
    """One desired mirror document: where it belongs and what it should say.

    ``raw`` is the regenerated (UNSCRUBBED) content — it never reaches the disk until a
    scrubber vouches for it (or ``mirror_allow_unscrubbed`` is set).
    """

    path: str  # absolute target path
    session_id: str
    kind: str  # RUNNING | DONE | SESSION (the ``ccc_mirror`` frontmatter value)
    raw: str


@dataclass
class _Pending:
    """A card that needs a scrubber verdict, plus the identities the vouch keys on."""

    card: _Card
    raw_sha: str  # sha256 of ``card.raw``
    disk: str | None  # what is on disk right now (None = no file)
    since: int = 0  # ``vouched_at`` of the expired row (revouch ordering)


@dataclass
class _Write:
    """A queued write: vouched bytes for one card (phase 2 performs it)."""

    path: str
    text: str
    session_id: str


@dataclass
class _Verdict:
    """What phase 1 decided: the writes to make, the receipts, the sessions to protect."""

    writes: list[_Write] = field(default_factory=list)
    vouches: list[MirrorVouch] = field(default_factory=list)
    protect: set[str] = field(default_factory=set)  # session ids of DEFERRED cards


@dataclass
class _Plan:
    """The desired state of every enabled root, before any scrubber has spoken."""

    cards: list[_Card] = field(default_factory=list)  # sessions root first, then running, done
    roots: list[tuple[Path, str, set[str]]] = field(default_factory=list)  # (root, kind, desired)
    dirty: list[TranscriptScan] = field(default_factory=list)  # refreshed scan rows to persist


def _iter_mirror_files(root: Path) -> list[Path]:
    """Canonical ``.md`` files under *root*, skipping any ``_``/``.`` path component."""
    if not root.exists():
        return []
    out: list[Path] = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root)
        if any(part.startswith(("_", ".")) for part in rel.parts):
            continue
        out.append(path)
    return out


def _cleanup_root(
    root: Path, kind: str, desired: set[str], report: MirrorReport, protect: set[str]
) -> None:
    """Remove ``ccc_mirror: <kind>`` files under *root* not in *desired* (stale or moved).

    Files without a matching ``ccc_mirror`` marker are never touched (decision 3), and
    neither is a file belonging to a session in *protect* — the sessions whose card was
    DEFERRED this pass. Their desired file may not exist yet (a rename, a running→done
    move), so removing the predecessor would leave the session with no readable mirror
    at all until the deferred card finally gets its scrubber call.
    """
    for path in _iter_mirror_files(root):
        if str(path) in desired:
            continue
        try:
            fm = _read_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if fm.get("ccc_mirror") != kind:
            continue  # foreign / unmarked file — leave it alone
        if fm.get("session_id") in protect:
            continue  # its replacement was deferred — keep the predecessor
        try:
            path.unlink()
            report.removed.append(str(path))
        except OSError:
            pass


def _scan_for(
    adapter: ClaudeAdapter,
    session: Session,
    scans: dict[str, TranscriptScan],
    dirty: list[TranscriptScan],
) -> TranscriptScan | None:
    """This pass's transcript-scan row for *session*, refreshed against the file.

    *scans* is the persisted table (one read per ``run_mirrors``) updated in place;
    every row whose transcript actually changed is appended to *dirty* for ONE batched
    write at the end of the run. A frozen transcript costs one ``stat()`` (the adapter
    hands back the very same object); a changed one a tail read. ``None`` means "ask the
    live probe instead" — either the adapter has no ``scan_transcript`` (stubs in tests)
    or no transcript resolves, which :func:`_observed_model_label` renders as "" anyway.
    """
    scan_fn = getattr(adapter, "scan_transcript", None)
    if scan_fn is None:
        return None
    prior = scans.get(session.session_id)
    try:
        new = scan_fn(session.cwd, session.session_id, prior)
    except Exception:  # pylint: disable=broad-exception-caught
        # A read failure is not a fact (see scan_transcript § Property P): persist
        # nothing, fall back to the probe, and let the next run retry.
        return None
    if new is None:
        return None
    if new is not prior:  # identity: the same object means the file was untouched
        scans[session.session_id] = new
        dirty.append(new)
    return new


@dataclass
class _Ctx:
    """Everything a card serialisation needs, gathered ONCE per pass.

    *hist_map* is the pre-fetched AIM history (one query, shared by all three roots)
    threading into BOTH the first-aim slug (:func:`_target_paths`) and the running/done
    body (:func:`_serialize`); *link_map* (session id → vault-relative SESSION mirror
    path) feeds the ``full session`` wikilink, which is why the sessions root is planned
    first. *scans* / *dirty* are the run's shared transcript-scan table and its write
    batch (see :func:`_scan_for`): each session's row is REFRESHED while its card is
    built, so a standalone ``ccc sync-mirrors`` right after a model switch writes the new
    model — it does not rely on the daemon having reconciled first.
    """

    store: Store
    git_base: Path
    adapter: ClaudeAdapter
    hist_map: dict[str, list[AimRevision]]
    scans: dict[str, TranscriptScan]
    dirty: list[TranscriptScan]
    link_map: dict[str, str] = field(default_factory=dict)


def _desired_paths(
    sessions: list[Session],
    root: Path,
    git_base: Path,
    hist_map: dict[str, list[AimRevision]],
) -> dict[str, Session]:
    """Canonical mirror path → session for every session of one root (see _target_paths).

    Content-free on purpose: the SESSION root's map is needed BEFORE any card is
    serialised, because the running/done bodies embed its paths as wikilinks.
    """
    first_aim = {
        s.session_id: (hist_map[s.session_id][0].aim if hist_map[s.session_id] else (s.aim or ""))
        for s in sessions
    }
    return _target_paths(sessions, root, git_base, first_aim)


def _cards_for(ctx: _Ctx, desired: dict[str, Session], kind: str) -> list[_Card]:
    """Serialise every desired card of one root (byte-stable, still UNSCRUBBED)."""
    cards: list[_Card] = []
    for path_str, session in desired.items():
        scan = _scan_for(ctx.adapter, session, ctx.scans, ctx.dirty)
        if kind == SESSION:
            content = _serialize_session(session, ctx.git_base, ctx.adapter, scan)
        else:
            content = _serialize(
                ctx.store,
                session,
                kind,
                ctx.git_base,
                ctx.adapter,
                ctx.hist_map[session.session_id],
                ctx.link_map.get(session.session_id, ""),
                scan,
            )
        cards.append(_Card(path=path_str, session_id=session.session_id, kind=kind, raw=content))
    return cards


def _link_map(desired: dict[str, Session], vault: Path) -> dict[str, str]:
    """Session id → vault-relative SESSION-mirror path (extension dropped, wikilink-ready).

    A mirror outside the vault root (custom config) yields no entry — the running/done
    mirrors then simply carry no link rather than a broken one.
    """
    out: dict[str, str] = {}
    for path_str, session in desired.items():
        try:
            rel = Path(path_str).relative_to(vault)
        except ValueError:
            continue
        out[session.session_id] = str(rel.with_suffix(""))
    return out


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------
def _lock_path() -> Path:
    return config.app_home() / "mirror_sync.lock"


def _acquire_lock() -> IO[str] | None:
    """Take the flock singleton non-blocking; ``None`` when another mirror run holds it."""
    config.app_home().mkdir(parents=True, exist_ok=True)
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


def _wait_for_lock(timeout: float) -> IO[str] | None:
    """Take the flock singleton, retrying for up to *timeout* seconds; ``None`` on give-up.

    A polled retry rather than a blocking ``flock``: the caller is a synchronous
    lifecycle command, so it must come back either way — bounded wait, never a hang.
    """
    deadline = time.monotonic() + timeout
    while True:
        handle = _acquire_lock()
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# the scrubber gate (phase 1)
# ---------------------------------------------------------------------------
class _ScrubPass:  # pylint: disable=too-few-public-methods  # one entry point: run()
    """The scrubber gate for ONE mirror pass: lazy resolution, degradation, budgets.

    Three properties the mirror invariant rests on:

    * **Lazy.** :func:`command_center.scrub.resolve_scrubber` runs a ``<exe> -h`` probe,
      so it is called only when a card actually needs a scrubber call — a steady-state
      pass (every card vouched) spawns NOTHING at all.
    * **Degrading is sticky.** A broker that fails once (exit 3, a timeout, a mangled
      document) is not asked again this pass: the first reason is recorded and every
      remaining card is withheld with it. A flapping broker never turns into a partial
      export where half the cards are stale and half are fresh.
    * **Fail closed at every branch.** Unresolvable, withheld, or vouched output that no
      longer carries the card's own frontmatter identity — all three mean the file on
      disk is left exactly as it is, and the reason (never content) goes to the report.
    """

    def __init__(self, policy: str, report: MirrorReport, verdict: _Verdict, now: int) -> None:
        self.policy = policy
        self.report = report
        self.verdict = verdict
        self.now = now
        self._resolution: scrub.Resolution | None = None
        self._degraded = ""  # first hard failure of this pass ("" while healthy)

    def _blocked(self) -> str:
        """Why no further card may be scrubbed ("" = go ahead); resolves on first use."""
        if self._degraded:
            return f"scrubber degraded earlier this pass: {self._degraded}"
        if self._resolution is None:
            self._resolution = scrub.resolve_scrubber(self.policy)
        return "" if self._resolution.ok else self._resolution.reason

    def _withhold(self, path: str, reason: str) -> None:
        """Refuse this write and latch the scrubber DEGRADED for the rest of the pass."""
        self.report.withheld.append((path, reason))
        if not self._degraded:
            self._degraded = reason

    def _accept(self, pending: _Pending, result: scrub.ScrubResult) -> None:
        """Turn one scrub result into a queued write + a vouch, or into a withheld card."""
        card = pending.card
        if result.withheld:
            self._withhold(card.path, result.reason)
            return
        # Identity check — the caller's half of the contract (see scrub.scrub): a scrubber
        # may only REPLACE spans, so the document must still be this card's own.
        fm = _read_frontmatter(result.text)
        if fm.get("ccc_mirror") != card.kind or fm.get("session_id") != card.session_id:
            self._withhold(card.path, "scrubbed output lost its frontmatter identity")
            return
        out = result.text
        self.report.vouched.append(card.path)
        if out != card.raw:
            self.report.scrubbed.append((card.path, result.labels))
        if pending.disk != out:  # byte-stable: write only on a real change
            self.verdict.writes.append(_Write(card.path, out, card.session_id))
        self.verdict.vouches.append(
            MirrorVouch(
                path=card.path,
                session_id=card.session_id,
                raw_sha=pending.raw_sha,
                out_sha=scrub.sha256(out),
                policy=self.policy,
                vouched_at=self.now,
            )
        )

    def run(self, queue: list[_Pending], budget: float, *, full: bool) -> None:
        """Scrub *queue* in order until *budget* seconds of calls are spent.

        The budget is wall time spent in THIS queue's scrub calls; a card that no longer
        fits is deferred (its file is kept as-is and its expired vouch, if any, stays —
        the next pass retries it). ``full=True`` (``ccc sync-mirrors --full``) lifts the
        budget entirely, which is what the one-off migration of an existing vault needs.
        """
        spent = 0.0
        for pending in queue:
            if not full and spent >= budget:
                self.report.deferred.append(pending.card.path)
                self.verdict.protect.add(pending.card.session_id)
                continue
            blocked = self._blocked()
            if blocked:  # no scrubber (or a degraded one) — refuse without a spawn
                self.report.withheld.append((pending.card.path, blocked))
                continue
            assert self._resolution is not None and self._resolution.scrubber is not None
            started = time.monotonic()
            result = scrub.scrub(
                self._resolution.scrubber, pending.card.raw, timeout=_SCRUB_TIMEOUT_S
            )
            spent += time.monotonic() - started
            self._accept(pending, result)


def _vouched_by(row: MirrorVouch, pending: _Pending, policy: str) -> bool:
    """Whether *row* still speaks for this card: same document, same bytes, same policy.

    All four must hold. ``disk is None`` (the file was deleted) fails it, so a removed
    mirror is re-scrubbed rather than silently re-created from a stale receipt.
    """
    return (
        row.raw_sha == pending.raw_sha
        and pending.disk is not None
        and row.out_sha == scrub.sha256(pending.disk)
        and row.policy == policy
    )


def _classify(
    store: Store, cfg: config.Config, plan: _Plan, report: MirrorReport, *, full: bool
) -> _Verdict:
    """Phase 1: decide, per card, whether it may be written — and at what cost.

    ``mirror_allow_unscrubbed`` is the one passthrough (raw bytes, no scrubber, no
    vouch rows — the receipts would be lies). Otherwise every card is either already
    vouched (skipped: no call, no write), REQUIRED (no usable vouch — new, changed,
    edited on disk, a changed policy, or a legacy file written before this feature) or a
    REVOUCH candidate (vouched but past :data:`VOUCH_TTL_MS`). REQUIRED cards run first,
    in desired order; revouches follow oldest-first on their own smaller budget.
    """
    verdict = _Verdict()
    if cfg.mirror_allow_unscrubbed:
        for card in plan.cards:
            if _read_disk(card.path) != card.raw:
                verdict.writes.append(_Write(card.path, card.raw, card.session_id))
        return verdict
    policy = (cfg.mirror_scrub_cmd or "").strip()
    vouches = store.mirror_vouches()
    now = now_ms()
    required: list[_Pending] = []
    revouch: list[_Pending] = []
    for card in plan.cards:
        pending = _Pending(card=card, raw_sha=scrub.sha256(card.raw), disk=_read_disk(card.path))
        row = vouches.get(card.path)
        if row is None or not _vouched_by(row, pending, policy):
            required.append(pending)
            continue
        if now - row.vouched_at < VOUCH_TTL_MS:
            continue  # vouched and fresh — the whole point: no call, no write
        pending.since = row.vouched_at
        revouch.append(pending)
    revouch.sort(key=lambda pending: pending.since)  # oldest vouch first
    gate = _ScrubPass(policy, report, verdict, now)
    gate.run(required, REQUIRED_BUDGET_S, full=full)
    gate.run(revouch, REVOUCH_BUDGET_S, full=full)
    return verdict


def _commit(store: Store, plan: _Plan, verdict: _Verdict, report: MirrorReport) -> None:
    """Phase 2: write the vouched bytes, persist the receipts, then clean up (or not).

    Cleanup is skipped ENTIRELY while anything was withheld: a card whose new bytes were
    refused keeps its old file, and the file cleanup would remove (the predecessor of a
    rename, the running mirror of a session that just finished) is then the only readable
    copy there is. One stale mirror beats no mirror.
    """
    for write in verdict.writes:
        _atomic_write(Path(write.path), write.text)
        report.written.append(write.session_id)
    store.put_mirror_vouches(verdict.vouches)
    if report.withheld:
        report.details.append(f"cleanup skipped: {len(report.withheld)} write(s) withheld")
        return
    for root, kind, desired in plan.roots:
        _cleanup_root(root, kind, desired, report, verdict.protect)
    store.drop_mirror_vouches(report.removed)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def _plan_roots(store: Store, cfg: config.Config, report: MirrorReport) -> _Plan:
    """Serialise the desired card of every session in every ENABLED root (phase 0).

    The SESSION root is planned first (membership = running ∪ done, INDEPENDENT of the
    running/done kill-switches) because its desired paths are the ``full session``
    wikilink the other two roots embed. Card order is the scrub order: sessions, running,
    done.
    """
    plan = _Plan()
    ctx = _Ctx(
        store=store,
        git_base=repos.git_base(),
        adapter=ClaudeAdapter(),
        hist_map={},
        scans=store.transcript_scans(),
        dirty=plan.dirty,
    )
    sessions = store.list_sessions(include_archived=True)
    running = [s for s in sessions if not s.draft and not s.archived and not s.done]
    done = [s for s in sessions if s.done and not s.draft]
    union = running + done
    ctx.hist_map = {s.session_id: store.list_aim_history(s.session_id) for s in union}
    if cfg.mirror_sessions:
        report.sessions = [s.session_id for s in union]
        root = sessions_root(cfg)
        desired = _desired_paths(union, root, ctx.git_base, ctx.hist_map)
        ctx.link_map = _link_map(desired, Path(cfg.vault_root).expanduser())
        plan.roots.append((root, SESSION, set(desired)))
        plan.cards.extend(_cards_for(ctx, desired, SESSION))
    if cfg.mirror_running:
        report.running = [s.session_id for s in running]
        root = running_root(cfg)
        desired = _desired_paths(running, root, ctx.git_base, ctx.hist_map)
        plan.roots.append((root, RUNNING, set(desired)))
        plan.cards.extend(_cards_for(ctx, desired, RUNNING))
    if cfg.mirror_done:
        report.done = [s.session_id for s in done]
        root = done_root(cfg)
        desired = _desired_paths(done, root, ctx.git_base, ctx.hist_map)
        plan.roots.append((root, DONE, set(desired)))
        plan.cards.extend(_cards_for(ctx, desired, DONE))
    return plan


def run_mirrors(
    store: Store, cfg: config.Config, *, full: bool = False, rescrub: bool = False
) -> MirrorReport:
    """Reconcile the RUNNING/DONE/SESSION mirrors with the store (flock-guarded).

    Export-only: the DB is the sole source of truth. A pass runs in two phases — plan +
    classify every card (which needs a scrubber call, which is already vouched, which
    does not fit the budget), then commit: write ONLY vouched bytes, persist the
    receipts, and clean up stale/moved files unless anything was withheld. A concurrent
    invocation returns an empty report immediately and touches nothing.

    *full* lifts the per-pass scrub budgets (the one-off migration of an existing vault);
    *rescrub* forgets every vouch first, so the whole tree is scrubbed again (a new rule
    set). Both are ``ccc sync-mirrors`` flags, never the daemon's.

    The persisted transcript scans are loaded ONCE and shared by all three roots, and each
    session's row is refreshed against its file as its card is built (see
    :func:`_scan_for`): a frozen transcript costs one ``stat()``, a changed one a tail
    read, and every changed row is written back in a single batch at the end. That is
    what lets a standalone ``ccc sync-mirrors`` after a model switch emit the NEW model
    without waiting for a daemon reconcile pass.
    """
    report = MirrorReport()
    lock = _acquire_lock()
    if lock is None:  # another mirror run holds the singleton — leave it to that one
        return report
    try:
        if rescrub:
            store.drop_mirror_vouches()
        plan = _plan_roots(store, cfg, report)
        verdict = _classify(store, cfg, plan, report, full=full)
        _commit(store, plan, verdict, report)
        # ONE batched write for every transcript that actually changed this run; an
        # all-frozen run writes (and commits) nothing at all.
        store.put_transcript_scans(plan.dirty)
        store.put_mirror_health(
            MirrorHealth(
                at=now_ms(),
                vouched=len(report.vouched),
                scrubbed=len(report.scrubbed),
                withheld=len(report.withheld),
                deferred=len(report.deferred),
                reason=report.withheld[0][1] if report.withheld else "",
            )
        )
        return report
    finally:
        _release_lock(lock)


@dataclass
class SessionFileHit:
    """The located SESSION-mirror file of one session (absolute + vault-relative)."""

    abs_path: Path
    vault_relpath: str  # relative to ``vault_root`` (extension kept — obsidian_uri-ready)


def session_file_path(cfg: config.Config, session_id: str) -> SessionFileHit | None:
    """Locate *session_id*'s SESSION-mirror file under ``sessions_dir`` (else ``None``).

    Scans ONLY the sessions root and matches ONLY files whose frontmatter carries
    ``ccc_mirror: session`` AND the exact ``session_id`` — robust against slug/hash
    naming drift (the frontmatter UUID is the identity). Used by the TUI ``os`` chord.
    """
    vault = Path(cfg.vault_root).expanduser()
    for path in _iter_mirror_files(sessions_root(cfg)):
        try:
            fm = _read_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if fm.get("ccc_mirror") != SESSION or fm.get("session_id") != session_id:
            continue
        try:
            rel = str(path.relative_to(vault))
        except ValueError:
            rel = path.name
        return SessionFileHit(abs_path=path, vault_relpath=rel)
    return None


def remove_mirror(cfg: config.Config, session_id: str) -> list[str]:
    """Remove any ``ccc_mirror`` file (any of the three roots) belonging to *session_id*.

    Used by ``ccc unlaunch`` to drop a session's running mirror synchronously when it
    returns to FUTURE. Only touches files whose frontmatter carries ``ccc_mirror`` AND the
    matching ``session_id`` — never a foreign file.

    Takes the SAME flock a mirror pass holds, waiting up to
    :data:`_REMOVE_LOCK_TIMEOUT_S` for it: unlinking a file a concurrent pass is about to
    ``os.replace`` into place would resurrect it. Unlike the pass this call blocks (it is
    a user-facing lifecycle step, not a backstop), and on a busy lock it says so and
    leaves the removal to the next pass — whose cleanup drops exactly these files anyway.
    """
    lock = _wait_for_lock(_REMOVE_LOCK_TIMEOUT_S)
    if lock is None:
        print(
            "mirrors: remove_mirror skipped — mirror sync lock busy (the next sync pass cleans up)",
            file=sys.stderr,
        )
        return []
    removed: list[str] = []
    try:
        for root in (running_root(cfg), done_root(cfg), sessions_root(cfg)):
            for path in _iter_mirror_files(root):
                try:
                    fm = _read_frontmatter(path.read_text(encoding="utf-8"))
                except OSError:
                    continue
                if not fm.get("ccc_mirror") or fm.get("session_id") != session_id:
                    continue
                try:
                    path.unlink()
                    removed.append(str(path))
                except OSError:
                    pass
        return removed
    finally:
        _release_lock(lock)
