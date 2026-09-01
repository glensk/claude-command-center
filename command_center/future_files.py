#!/usr/bin/env python3
"""The single source of truth for the *future-job* markdown file format.

A future job (a ``draft=1`` row in the store) is mirrored as exactly one markdown
file under ``~/obsidian/01-llm-tasks/future/<cat>/<repo>/<slug>-<hash>.md`` — the
human editing surface (see ``PLAN_future-job-files.md``). This module owns the
file *shape* only: parse, serialize (canonical form), validate, the slug/hash
derivation, the ``obsidian://`` URI, and the managed ``> [!error]`` callout. The
two-sided reconciler (:mod:`command_center.futuresync`, a later phase) drives the
actual disk ↔ DB sync using these primitives.

Design invariants (do not regress):

* **Full UUID is the identity.** ``session_id`` in the frontmatter joins the file
  to its DB row; the 4-hex ``hash`` is only a display / filename prefix.
* **Round-trip stability.** ``parse_job_file(serialize(...))`` reproduces the
  synced fields exactly, so an unchanged file re-exports byte-identically (the
  content-hash echo-suppression relies on this).
* **PyYAML for the frontmatter block only** (``yaml.safe_load``), restricted to
  flat string scalars — non-string scalars are coerced to ``str``; the body is
  split on the literal ``# AIM`` / ``# Prompt`` headings.
* **Idempotent error block.** :func:`upsert_error_block` is a fixed point, so the
  launchd retrigger can never loop on an error file.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import datetime as dt
import hashlib
import re
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config, models

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of hex chars of the UUID shown as the display / filename prefix.
DISPLAY_HASH_LEN = 4

#: The lifecycle values a job file's ``status`` can hold (frontmatter + widget).
STATUSES: tuple[str, ...] = ("draft", "ready", "registered", "error", "launched")

# The three canonical body headings; a section runs until the next of these
# (so a user prompt may contain arbitrary other sub-headings without truncation,
# but the reserved ``## Controls`` block is never swallowed into the prompt).
# Canonical level is H2 (a second H1 trips markdownlint MD025 in the vault);
# the parser accepts the legacy H1 form so pre-normalization files round-trip.
_AIM_HEADING = "## AIM"
_PROMPT_HEADING = "## Prompt"
_CONTROLS_HEADING = "## Controls"
_SECTION_RE = re.compile(r"^#{1,2} +(AIM|Prompt|Controls)\s*$")


def _section_name(line: str) -> str | None:
    """Canonical section a line opens (``AIM``/``Prompt``/``Controls``), else ``None``."""
    match = _SECTION_RE.match(line.strip())
    return match.group(1) if match else None


# Obsidian command ids of the Shell Commands entries the in-note Meta Bind buttons
# trigger. Obsidian namespaces a plugin command as ``<plugin-id>:<command-id>``;
# obsidian-shellcommands registers each entry as ``shell-command-<entry-id>``
# (verified from its source ``generateObsidianCommandId``), so e.g. the entry
# ``ccc-start-job-from-file`` (in the vault's shellcommands ``data.json``) surfaces
# at the id below. Each entry runs the matching ``ccc <cmd> --file
# {{file_path:absolute}}`` against the active (job) file.
_START_JOB_COMMAND_ID = "obsidian-shellcommands:shell-command-ccc-start-job-from-file"
_DONE_JOB_COMMAND_ID = "obsidian-shellcommands:shell-command-ccc-done-job-from-file"
_DELETE_JOB_COMMAND_ID = "obsidian-shellcommands:shell-command-ccc-delete-job-from-file"
_RESTORE_JOB_COMMAND_ID = "obsidian-shellcommands:shell-command-ccc-restore-job-from-file"

# Markers delimiting the ONE managed error callout upsert/clear touch.
_ERR_START = "<!-- ccc-sync-error -->"
_ERR_END = "<!-- /ccc-sync-error -->"
# The block plus any blank-line padding around it — stripped as a unit so the
# reinsert can re-normalise whitespace to a fixed point.
_ERR_BLOCK_RE = re.compile(
    r"\n*" + re.escape(_ERR_START) + r".*?" + re.escape(_ERR_END) + r"\n*",
    re.DOTALL,
)
# Leading YAML frontmatter fence (must be at the very start of the file).
_FRONTMATTER_RE = re.compile(r"(---\r?\n)(.*?)(\r?\n---[ \t]*\r?\n?)", re.DOTALL)


# ---------------------------------------------------------------------------
# Identity, slug, filename
# ---------------------------------------------------------------------------
def display_hash(session_id: str) -> str:
    """First :data:`DISPLAY_HASH_LEN` hex chars of the session UUID.

    This is the user-locked display prefix (shown in the id column) and the
    filename prefix. Falls back to leading hex chars of the raw string (padded)
    for a non-UUID input so it never raises.
    """
    try:
        return uuid.UUID(str(session_id)).hex[:DISPLAY_HASH_LEN]
    except (ValueError, AttributeError, TypeError):
        hexchars = "".join(c for c in str(session_id).lower() if c in "0123456789abcdef")
        return (hexchars[:DISPLAY_HASH_LEN] or "0000").ljust(DISPLAY_HASH_LEN, "0")


def slugify(aim: str, max_words: int = 6) -> str:
    """Kebab-case slug of the first ``max_words`` AIM words (``[a-z0-9-]`` only).

    Accents are transliterated (``café`` → ``cafe``), other non-ASCII and
    punctuation dropped. Empty / all-symbol input falls back to ``"job"`` so the
    filename is always non-empty.
    """
    text = unicodedata.normalize("NFKD", aim or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z0-9]+", text)[:max_words]
    return "-".join(words) or "job"


def job_filename(session_id: str, aim: str) -> str:
    """Canonical ``<slug>-<hash>.md`` filename for a job (slug frozen at creation)."""
    return f"{slugify(aim)}-{display_hash(session_id)}.md"


# ---------------------------------------------------------------------------
# Parsed representation
# ---------------------------------------------------------------------------
@dataclass
class ParsedJob:  # pylint: disable=too-many-instance-attributes
    """A job file decoded into its fields.

    ``prompt`` is ``None`` when the ``# Prompt`` section is empty — the "defaults
    to the AIM at launch" semantics. ``errors`` is empty when the file parsed
    structurally; semantic problems (bad repo, unknown ``job_type``, …) are
    surfaced separately by :func:`validate`, which needs the git base.
    """

    session_id: str
    status: str
    repo: str
    job_type: str
    llm_overseer: str
    llm_exec: str
    start_when: str
    deadline: str
    created: str
    aim: str
    prompt: str | None
    # Fixed start date (ISO YYYY-MM-DD, "" = none) — defaulted (unlike the fields
    # above) so pre-existing keyword construction sites/tests stay valid.
    start_date: str = ""
    # Full session UUID of another job this one depends on finishing first ("" = none).
    # Emitted only when non-empty (byte-stable for existing files); an unknown-but-
    # well-formed UUID is allowed (registration order must not matter — dangling degrades).
    depends_on: str = ""
    # Ban every Codex integration for the launched session (``CCC_NO_CODEX=1``).
    # A bare-bool frontmatter key, emitted ONLY when true so every existing job file
    # stays byte-identical; refused together with a codex/codex-write job type.
    no_codex: bool = False
    # ISO date the job was moved to the delete/ trash ("" = not deleted).
    deleted: str = ""
    # Claude account LABEL the job launches (bills) under ("" = the default account).
    # Round-trips to the store's config_dir path via ``accounts`` at import/export.
    account: str = ""
    # The phone-friendly launch toggle: a momentary command, not state. Canonical
    # files always carry ``launch: false`` (a bare bool so Obsidian — including
    # mobile — renders a checkbox); flipping it true asks the next sync pass to
    # start the job, which consumes the flag back to false.
    launch: str = ""
    errors: list[str] = field(default_factory=list)


def _scalar(value: object) -> str:
    """Coerce a YAML scalar to a flat string (dates → ISO, bools → true/false)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.date):  # covers datetime.datetime too
        return value.isoformat()
    return str(value)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(inner_frontmatter, body)``; empty frontmatter when there is none."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(2), text[match.end() :]


def _extract_section(body: str, heading: str) -> str:
    """Text of the ``heading`` section, up to the next canonical heading (stripped).

    ``heading`` is the canonical H2 form; the legacy H1 form matches too.
    """
    target = heading.lstrip("#").strip()
    lines = body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if _section_name(line) == target:
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if _section_name(lines[j]) is not None:
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def parse_job_file(text: str) -> ParsedJob:
    """Decode a job file into a :class:`ParsedJob` (never raises).

    Frontmatter is parsed with ``yaml.safe_load`` on the block between the first
    two ``---`` fences only, restricted to flat string scalars. The body is split
    on the literal ``# AIM`` / ``# Prompt`` headings; the ``## Controls`` widget
    section and anything else are ignored. An empty ``# Prompt`` section yields
    ``prompt=None``.
    """
    errors: list[str] = []
    fm_text, body = _split_frontmatter(text)
    data: dict[str, str] = {}
    if not fm_text.strip():
        errors.append("missing YAML frontmatter (--- block at the top of the file)")
    else:
        try:
            raw = yaml.safe_load(fm_text)
        except yaml.YAMLError as exc:
            raw = None
            errors.append(f"invalid YAML frontmatter: {exc}")
        if isinstance(raw, dict):
            data = {str(key): _scalar(val) for key, val in raw.items()}
        elif raw is not None:
            errors.append("frontmatter is not a key: value mapping")

    prompt_text = _extract_section(body, _PROMPT_HEADING)
    return ParsedJob(
        session_id=data.get("session_id", ""),
        status=data.get("status", ""),
        repo=data.get("repo", ""),
        job_type=data.get("job_type", ""),
        llm_overseer=data.get("llm_overseer") or models.DEFAULT_LLM,
        llm_exec=data.get("llm_exec") or models.DEFAULT_LLM,
        start_when=data.get("start_when", ""),
        start_date=data.get("start_date", ""),
        depends_on=data.get("depends_on", ""),
        deadline=data.get("deadline", ""),
        created=data.get("created", ""),
        deleted=data.get("deleted", ""),
        no_codex=_truthy(data.get("no_codex", "")),
        account=data.get("account", ""),
        aim=_extract_section(body, _AIM_HEADING),
        prompt=prompt_text or None,
        # Normalized to "" unless truthy: the toggle is a momentary command, so
        # "false"/absent are the same thing — and round-trip equality holds
        # (serialize always emits ``launch: false``).
        launch=data.get("launch", "") if _launch_truthy(data.get("launch", "")) else "",
        errors=errors,
    )


def _truthy(value: str) -> bool:
    """Whether a raw boolean-ish frontmatter scalar reads as true."""
    return value.strip().lower() in {"true", "yes", "1", "on"}


def _launch_truthy(value: str) -> bool:
    """Whether a raw ``launch`` frontmatter scalar means "start this job"."""
    return _truthy(value)


def launch_requested(job: ParsedJob) -> bool:
    """Whether the file's ``launch`` toggle is flipped on (truthy scalar)."""
    return _launch_truthy(job.launch)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(job: ParsedJob, git_base: Path) -> list[str]:
    """Semantic errors for a parsed job (empty list = valid, ready to register).

    Checks: ``session_id`` parses as a UUID; ``aim`` is non-empty; ``job_type`` is
    one of :data:`models.JOB_TYPES`; and ``repo`` resolves to an existing directory
    — either ``<cat>/<repo>`` under *git_base*, or an absolute path to a directory
    (the fallback for drafts whose cwd sits outside ``$GIT_BASE/<cat>/<repo>``).
    """
    errors: list[str] = []
    try:
        uuid.UUID(job.session_id)
    except (ValueError, AttributeError, TypeError):
        errors.append(f"session_id is not a valid UUID: {job.session_id!r}")
    if not job.aim.strip():
        errors.append("AIM is empty")
    if job.job_type not in models.JOB_TYPES:
        errors.append(f"job_type {job.job_type!r} is not one of {', '.join(models.JOB_TYPES)}")
    if job.llm_overseer not in models.LLM_CHOICES:
        errors.append(
            f"llm_overseer {job.llm_overseer!r} is not one of {', '.join(models.LLM_CHOICES)}"
        )
    if job.llm_exec not in models.LLM_CHOICES:
        errors.append(f"llm_exec {job.llm_exec!r} is not one of {', '.join(models.LLM_CHOICES)}")
    if not _repo_dir(job.repo, git_base):
        errors.append(f"repo {job.repo!r} is not an existing <cat>/<repo> or absolute directory")
    if job.start_date.strip() and models.parse_iso_date(job.start_date) is None:
        errors.append(f"start_date {job.start_date!r} is not a valid ISO date (YYYY-MM-DD)")
    # A dependency must be a well-formed UUID; an UNKNOWN but well-formed UUID is ALLOWED
    # (registration order must not matter — a dangling reference degrades in the views).
    if job.depends_on.strip():
        try:
            uuid.UUID(job.depends_on.strip())
        except (ValueError, AttributeError, TypeError):
            errors.append(f"depends_on {job.depends_on!r} is not a valid UUID")
    # An empty account = the default account (always valid); a non-empty label must be
    # configured, else launching would silently bill the default seat — fail loud instead.
    account_labels = config.claude_config_dirs()
    if job.account.strip() and job.account not in account_labels:
        configured = ", ".join(account_labels)
        errors.append(f"account '{job.account}' is not a configured account label ({configured})")
    # The no_codex ⇄ codex-job-type rule, same wording as the CLI and the launch guard.
    conflict = models.no_codex_conflict(job.job_type, job.no_codex)
    if conflict:
        errors.append(conflict)
    return errors


def _repo_dir(repo: str, git_base: Path) -> Path | None:
    """The existing working directory *repo* names, or ``None`` when invalid.

    Valid = ``<cat>/<repo>`` (exactly two components) resolving to a directory
    under *git_base*, OR an absolute path to an existing directory.
    """
    repo = (repo or "").strip()
    if not repo:
        return None
    path = Path(repo)
    if path.is_absolute():
        return path if path.is_dir() else None
    parts = [p for p in repo.split("/") if p]
    if len(parts) == 2:
        candidate = git_base / parts[0] / parts[1]
        return candidate if candidate.is_dir() else None
    return None


# ---------------------------------------------------------------------------
# repo <-> cwd mapping
# ---------------------------------------------------------------------------
def repo_to_cwd(repo: str, git_base: Path) -> Path:
    """Working directory a ``repo`` value points at (inverse of :func:`cwd_to_repo`)."""
    path = Path(repo)
    if path.is_absolute():
        return path
    return git_base / repo


def cwd_to_repo(cwd: str, git_base: Path) -> str:
    """The ``repo`` label for a working directory.

    A cwd exactly two levels under *git_base* becomes ``<cat>/<repo>``; anything
    else (git_base itself, one level deep, or outside the tree) becomes the
    absolute path string (the fallback drafts use).
    """
    try:
        rel = Path(cwd).resolve().relative_to(git_base.resolve())
    except (ValueError, OSError):
        return str(Path(cwd))
    if len(rel.parts) == 2:
        return "/".join(rel.parts)
    return str(Path(cwd))


def rel_dir_for(repo: str, git_base: Path) -> str:  # pylint: disable=unused-argument
    """Vault-relative directory (under the future root) a job for *repo* lives in.

    ``<cat>/<repo>`` for repo-shaped values; otherwise ``other/<final-component>``
    — deliberately NOT underscore-prefixed, since the sync scan skips ``_`` dirs.
    """
    path = Path(repo)
    if not path.is_absolute():
        parts = [p for p in repo.split("/") if p]
        if len(parts) == 2:
            return "/".join(parts)
    return f"other/{Path(repo).name}"


# ---------------------------------------------------------------------------
# Serialization (canonical form)
# ---------------------------------------------------------------------------
def _yaml_str(value: str) -> str:
    """A double-quoted YAML scalar, escaping ``\\`` and ``"`` (round-trip safe)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _button_block(label: str, command_id: str, *, style: str, button_id: str = "") -> str:
    """One Meta Bind ``meta-bind-button`` fenced block firing a ``command`` action.

    Meta Bind button schema (renders on plugin 1.4.x+): a ``command`` action is
    ``{type: command, command: <obsidian-command-id>}``; ``style: primary`` maps to the
    ``mod-cta`` (accented) button, ``destructive`` to ``mod-warning``. A non-empty
    *button_id* adds ``id`` + ``hidden: true`` so the block itself renders nothing and an
    inline ``BUTTON[id, ...]`` row references it.
    """
    ident = f"id: {button_id}\nhidden: true\n" if button_id else ""
    return (
        "```meta-bind-button\n"
        f'label: "{label}"\n'
        f"{ident}"
        f"style: {style}\n"
        "actions:\n"
        "  - type: command\n"
        f"    command: {command_id}\n"
        "```"
    )


def _action_buttons() -> str:
    """The live job file's button row: ▶ Start · ✓ Mark done · 🗑 Delete.

    Three hidden button definitions plus ONE inline ``BUTTON[...]`` row so the buttons sit
    side by side. Everything sits between the frontmatter and ``## AIM`` so
    :func:`parse_job_file` (which only reads ``## AIM``/``## Prompt``) ignores it entirely —
    round-trip stable. Start launches the job in a new iTerm tab (``ccc open-job --file``);
    Mark done finishes the job into the DONE list/mirror (``ccc done-job --file``); Delete
    moves the job to the vault's ``delete/`` trash (``ccc delete-job --file``, restorable).
    """
    start = _button_block(
        "▶ Start this job", _START_JOB_COMMAND_ID, style="primary", button_id="start-job"
    )
    done = _button_block(
        "✓ Mark job as done", _DONE_JOB_COMMAND_ID, style="default", button_id="done-job"
    )
    delete = _button_block(
        "🗑 Delete job", _DELETE_JOB_COMMAND_ID, style="destructive", button_id="delete-job"
    )
    return f"{start}\n\n{done}\n\n{delete}\n\n`BUTTON[start-job, done-job, delete-job]`"


def _restore_button() -> str:
    """The deleted job file's single "↩ Stage job back in" button (``ccc restore-job``)."""
    return _button_block("↩ Stage job back in", _RESTORE_JOB_COMMAND_ID, style="primary")


def _controls_block(repo: str, repo_options: list[str]) -> str:
    """The Meta Bind ``## Controls`` section — labelled inline selects over the frontmatter.

    ``status`` and ``job_type`` options are the static enums; the ``repo`` options
    are injected by the caller (current repos from ``repos.py``) with the file's
    own repo listed first. The widgets are sugar over plain-text frontmatter, so a
    stale option list is harmless — validation is the source of truth.
    """
    status_opts = ", ".join(f"option({s})" for s in STATUSES)
    jobtype_opts = ", ".join(f"option({j})" for j in models.JOB_TYPES)
    llm_opts = ", ".join(f"option({m})" for m in models.LLM_CHOICES)
    seen: list[str] = []
    for opt in [repo, *repo_options]:
        opt = (opt or "").strip()
        if opt and opt not in seen:
            seen.append(opt)
    repo_opts = ", ".join(f"option({o})" for o in seen) if seen else "option()"
    # The account select appears ONLY when >1 Claude account is configured — a
    # single-account machine emits no account line (byte-identical output, no churn).
    account_labels = list(config.claude_config_dirs())
    account_line = ""
    if len(account_labels) > 1:
        account_opts = ", ".join(f"option({label})" for label in account_labels)
        account_line = f"- **account** `INPUT[inlineSelect({account_opts}):account]`\n"
    # One control per list bullet: markdown guarantees a hard line break per item,
    # so the labels + boxes stack cleanly on narrow screens (mobile Obsidian) instead
    # of soft-wrapping label/box pairs into a jumble.
    return (
        f"{_CONTROLS_HEADING}\n\n"
        f"- **status** `INPUT[inlineSelect({status_opts}):status]`\n"
        f"- **job type** `INPUT[inlineSelect({jobtype_opts}):job_type]`\n"
        f"- **overseer** `INPUT[inlineSelect({llm_opts}):llm_overseer]`\n"
        f"- **executor** `INPUT[inlineSelect({llm_opts}):llm_exec]`\n"
        f"{account_line}"
        f"- **repo** `INPUT[inlineSelect({repo_opts}):repo]`"
    )


def serialize(  # pylint: disable=too-many-arguments
    *,
    session_id: str,
    aim: str,
    status: str = "draft",
    repo: str = "",
    job_type: str = "claude",
    llm_overseer: str = models.DEFAULT_LLM,
    llm_exec: str = models.DEFAULT_LLM,
    start_when: str = "",
    start_date: str = "",
    depends_on: str = "",
    deadline: str = "",
    created: str = "",
    deleted: str = "",
    no_codex: bool = False,
    account: str = "",
    prompt: str | None = None,
    repo_options: list[str] | None = None,
) -> str:
    """Canonical job-file content from the job fields.

    Deterministic and round-trip stable: ``parse_job_file(serialize(x))`` recovers
    the synced fields. The ``id`` frontmatter key is derived from ``session_id``;
    the ``## Controls`` widgets are injected with ``repo_options``. The ``deleted``
    key (ISO date the job was trashed) is emitted only when non-empty — a
    ``status: deleted`` file gets the single restore button instead of the
    start/done/delete action row.
    """
    keys = [
        "---",
        f"id: {_yaml_str(display_hash(session_id))}",
        f"session_id: {_yaml_str(session_id)}",
        f"status: {_yaml_str(status)}",
        f"repo: {_yaml_str(repo)}",
        f"job_type: {_yaml_str(job_type)}",
        f"llm_overseer: {_yaml_str(llm_overseer)}",
        f"llm_exec: {_yaml_str(llm_exec)}",
        f"start_when: {_yaml_str(start_when)}",
        f"start_date: {_yaml_str(start_date)}",
        f"deadline: {_yaml_str(deadline)}",
        # Bare bool (never quoted): Obsidian's properties UI type-infers a checkbox,
        # which is the whole point — a one-tap launch toggle on mobile. Canonical is
        # always false; sync consumes a true back to false after spawning the launch.
        "launch: false",
        f"created: {_yaml_str(created)}",
    ]
    # Emitted only when non-empty (like ``deleted``/``account``) so every existing
    # dependency-less job file stays byte-identical (no churn on the canonical rewrite).
    if depends_on.strip():
        keys.append(f"depends_on: {_yaml_str(depends_on)}")
    if deleted.strip():
        keys.append(f"deleted: {_yaml_str(deleted)}")
    # Bare bool (Obsidian renders a checkbox), and emitted ONLY when set so a job file
    # without the opt-out keeps its exact current bytes.
    if no_codex:
        keys.append("no_codex: true")
    # In multi-account mode the label is always emitted (the caller passes the
    # concrete default account's label there, so the Obsidian select always shows a
    # value); on single-account machines it stays "" and no key is written, keeping
    # those job files byte-identical (no churn).
    if account.strip():
        keys.append(f"account: {_yaml_str(account)}")
    keys.append("---")
    frontmatter = "\n".join(keys)
    aim_body = aim.strip()
    prompt_body = prompt.strip() if (prompt and prompt.strip()) else ""
    controls = _controls_block(repo, repo_options or [])
    # The buttons need a real session_id to target; the button-less pad template
    # (empty session_id) stays button-less. A trashed file swaps the action row
    # for the restore button.
    button = ""
    if (session_id or "").strip():
        button = f"{_restore_button()}\n\n" if status == "deleted" else f"{_action_buttons()}\n\n"
    return (
        f"{frontmatter}\n\n"
        f"{button}"
        f"{_AIM_HEADING}\n\n{aim_body}\n\n"
        f"{_PROMPT_HEADING}\n\n{prompt_body}\n\n"
        f"{controls}\n"
    )


# ---------------------------------------------------------------------------
# Hashing, URIs, paths
# ---------------------------------------------------------------------------
def content_hash(text: str) -> str:
    """SHA-256 hexdigest of the file content (the echo-suppression sync hash)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def vault_name(cfg: config.Config | None = None) -> str:
    """The Obsidian vault name for ``obsidian://`` URIs.

    The ``vault_name`` config key, else the basename of ``vault_root``, else
    ``"obsidian"``.
    """
    cfg = cfg or config.load_config()
    if cfg.vault_name:
        return cfg.vault_name
    return Path(cfg.vault_root).expanduser().name or "obsidian"


def obsidian_uri(vault_relpath: str, vault: str | None = None) -> str:
    """``obsidian://open`` URI for a vault-relative path (``/`` kept, rest quoted).

    *vault* names the Obsidian vault; when omitted it is resolved from config via
    :func:`vault_name`.
    """
    encoded = urllib.parse.quote(vault_relpath, safe="/")
    name = vault if vault is not None else vault_name()
    return f"obsidian://open?vault={urllib.parse.quote(name)}&file={encoded}"


def future_root(cfg: config.Config) -> Path:
    """Expanded root of the future-job files (``future_dir`` config key)."""
    return config.guard_vault_path(Path(cfg.future_dir).expanduser())


def delete_root(cfg: config.Config) -> Path:
    """Expanded root of the deleted-job trash (``delete_dir`` config key)."""
    return config.guard_vault_path(Path(cfg.delete_dir).expanduser())


def pad_path(cfg: config.Config) -> Path:
    """Expanded path of the persistent capture pad (``future_pad`` config key)."""
    return config.guard_vault_path(Path(cfg.future_pad).expanduser())


# ---------------------------------------------------------------------------
# Managed error callout (idempotent)
# ---------------------------------------------------------------------------
def _set_status(text: str, status: str) -> str:
    """Set the frontmatter ``status`` (adding the key if absent); preserve the rest."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text
    open_fence, inner, close_fence = match.group(1), match.group(2), match.group(3)
    rest = text[match.end() :]

    def _repl(mm: re.Match[str]) -> str:
        return f"{mm.group(1)}{_yaml_str(status)}"

    new_inner, count = re.subn(r"(?m)^(status:[ \t]*).*$", _repl, inner, count=1)
    if count == 0:
        new_inner = f"{inner}\nstatus: {_yaml_str(status)}"
    return f"{open_fence}{new_inner}{close_fence}{rest}"


def _error_block(errors: list[str]) -> str:
    """The managed ``> [!error]`` callout (marker-delimited) listing *errors*.

    Shape is markdownlint-clean (MD032): a ``>`` spacer between the callout title
    and the list, and a blank line before the closing marker.
    """
    lines = [
        _ERR_START,
        "> [!error] ccc could not apply this file — fix the fields below"
        " (new jobs: set status back to ready)",
        ">",
    ]
    for err in errors or ["unknown error"]:
        lines.append(f"> - {err}")
    lines.extend(["", _ERR_END])
    return "\n".join(lines)


def upsert_error_block(text: str, errors: list[str]) -> str:
    """Flip ``status`` to ``error`` and insert/replace the ONE managed error callout.

    Only the frontmatter ``status`` line and the marker-delimited block are
    touched — all other user content is preserved. The result is a fixed point
    (applying twice equals applying once), so a launchd retrigger on the rewritten
    file is a cheap no-op rather than an edit loop.
    """
    without = _ERR_BLOCK_RE.sub("", _set_status(text, "error"), count=1)
    block = _error_block(errors)
    fm_text, body = _split_frontmatter(without)
    if fm_text or without.startswith("---"):
        fm_full = without[: len(without) - len(body)].rstrip("\n")
        body = body.lstrip("\n")
        result = f"{fm_full}\n\n{block}\n\n{body}" if body else f"{fm_full}\n\n{block}\n"
    else:
        body = without.lstrip("\n")
        result = f"{block}\n\n{body}" if body else f"{block}\n"
    return text if result == text else result


def clear_error_block(text: str) -> str:
    """Remove the managed error callout (leaving ``status`` and user content alone)."""
    return _ERR_BLOCK_RE.sub("", text, count=1)
