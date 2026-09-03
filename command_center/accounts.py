#!/usr/bin/env python3
"""Multi-account Claude Code launch environment (the ONE billing pin).

Claude Code hashes ``CLAUDE_CONFIG_DIR`` into its Keychain service name whenever
the var is *SET* (verified against the 2.1.205 binary): setting it to the default
``~/.claude`` therefore does **not** authenticate, because the service name
``Claude Code-credentials-ebcf0c99`` does not exist. So the account pin is:

* the DEFAULT account (``claude_home()``, active when the var is unset) → **UNSET**
  ``CLAUDE_CONFIG_DIR``;
* any OTHER account → **SET** it to that account's absolute config dir.

``CLAUDE_SECURESTORAGE_CONFIG_DIR`` is always stripped: it takes PRECEDENCE in the
Keychain-service hash and would otherwise defeat the pin.

Two renderings of the same rule:

* :func:`launch_env` — a child-process env ``dict`` for ``Popen(env=)`` / ``execvpe``.
* :func:`launch_env_prefix` — a POSIX-shell snippet for the command *strings* the
  iTerm / tmux launchers build (there is no ``env=`` to pass there).

Per-SESSION launch policy (the account pin plus everything else a specific session's
row demands, currently ``CCC_NO_CODEX``) lives in :func:`session_launch_env` and its two
siblings :func:`session_apply_to_environ` / :func:`session_launch_env_prefix`. Every
ccc-owned launch/resume surface must go through one of those three — a surface that
calls the bare account functions silently drops the session's own flags.
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
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from . import config

if TYPE_CHECKING:
    from .adapters.base import Adapter

# CLAUDE_SECURESTORAGE_CONFIG_DIR takes precedence over CLAUDE_CONFIG_DIR in the
# Keychain-service hash, so an ambient value would silently defeat the pin below.
_SECURE_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
_CONFIG_VAR = "CLAUDE_CONFIG_DIR"

# One-shot dedupe so a same-id-in-two-registries conflict warns once, not every
# 5 s TUI refresh / daemon pass.
_WARNED_CONFLICTS: set[str] = set()


def _resolve(path: str | Path) -> Path:
    """``expanduser`` + ``resolve`` (non-strict), tolerating a missing path."""
    try:
        return Path(path).expanduser().resolve()
    except OSError:
        return Path(path).expanduser()


def default_config_dir() -> Path:
    """The DEFAULT account's config dir (``claude_home()``, the unset-var account)."""
    return _resolve(config.claude_home())


def is_default_config_dir(config_dir: str) -> bool:
    """True when *config_dir* is (or is empty ⇒) the default account.

    An empty string is treated as the default here so :func:`launch_env` UNSETS the
    var for an unstamped row; the multi-account "unknown ⇒ refuse" guard is enforced
    separately at the launch call sites (they check ``config_dir == "" and
    is_multi_account()`` BEFORE calling this).
    """
    if not config_dir:
        return True
    return _resolve(config_dir) == default_config_dir()


def is_multi_account() -> bool:
    """True when more than one Claude account is configured."""
    return len(config.claude_config_dirs()) > 1


def same_config_dir(a: str, b: str) -> bool:
    """True if *a* and *b* name the same account dir (empty ⇒ the default account)."""
    return (_resolve(a) if a else default_config_dir()) == (
        _resolve(b) if b else default_config_dir()
    )


def account_label(config_dir: str) -> str:
    """The configured label for *config_dir* (falls back to its basename / path)."""
    if not config_dir:
        return next(iter(config.claude_config_dirs()), "private")
    target = _resolve(config_dir)
    for label, path in config.claude_config_dirs().items():
        if _resolve(path) == target:
            return label
    return Path(config_dir).name or config_dir


def effective_account_label(config_dir: str) -> str:
    """The label *config_dir* should present as, honoring the identity hard-link.

    :func:`account_label` alone is purely path-based, so it shows the WRONG
    label/glyph after the exact drift :func:`resolve_card_label` guards against (a
    bare ``/login`` in the wrong shell silently swaps which account a config dir
    holds) — this is that same correction, but dir → label instead of label → dir.
    When a hard link (``claude_account_emails``) is configured, this reads
    *config_dir*'s CURRENT identity (:func:`account_email`) and returns whichever
    label's hard-linked email matches it. Falls back to the plain path-based
    :func:`account_label` when no hard link is configured, the identity cannot be
    read, or it matches nothing configured — never worse than today's behaviour for
    a single-account or non-hard-linked setup.

    Meant for a single per-invocation query (e.g. the statusline's own badge, or one
    row's marker) — each call costs one ``.claude.json`` read, so a per-row loop over
    many sessions should resolve labels once per configured account up front instead.
    """
    emails = config.claude_account_email_map()
    if not emails:
        return account_label(config_dir)
    actual = account_email(config_dir)
    if actual is None:
        return account_label(config_dir)
    for label, email in emails.items():
        if email == actual:
            return label
    return account_label(config_dir)


def current_account_glyph() -> str:
    """Bare glyph (no trailing space) for the CURRENT shell's account, or ``""``.

    ``🏠`` for ``private``, ``💼`` for ``work``, ``""`` for any other/unresolved
    account or in single-account mode (matching :func:`home_marker`'s "no signal"
    rule) — honors the identity hard-link via :func:`effective_account_label`, so
    this reflects who is ACTUALLY logged into the current ``CLAUDE_CONFIG_DIR``
    rather than which dir it happens to be. The statusline's ``ccc statusline
    --print-glyph`` prints exactly this.
    """
    if len(config.claude_config_dirs()) <= 1:
        return ""
    return card_glyph(effective_account_label(env_config_dir()))


def card_glyph(label: str) -> str:
    """Bare glyph (no trailing space) for account *label* — ``🏠`` private / ``💼``
    work / ``""`` for anything else. The one PUBLIC accessor for the private
    ``_HOME_GLYPH``/``_WORK_GLYPH`` table-column constants, for callers outside this
    module (e.g. the TUI's static usage-card titles) that want the bare glyph.
    """
    return {"private": _HOME_GLYPH, "work": _WORK_GLYPH}.get(label, "").strip()


def _claude_json_path(config_dir: str | Path) -> Path:
    """Where Claude Code's own account/profile JSON lives for *config_dir*.

    Mirrors Claude Code's own resolution (the same fork :mod:`usage` uses for its
    Keychain service name): the DEFAULT account's file is ``$HOME/.claude.json`` —
    a sibling of ``~/.claude/``, NOT inside it — while any other account's file is
    ``<config_dir>/.claude.json``.
    """
    if _resolve(config_dir) == default_config_dir():
        return Path.home() / ".claude.json"
    return _resolve(config_dir) / ".claude.json"


TRUST_KEY = "hasTrustDialogAccepted"


def ensure_trusted(config_dir: str, cwd: str | Path | None = None) -> bool:
    """Trust *cwd* (default: the process cwd) for the account of *config_dir*.

    Claude Code's "Do you trust the files in this folder?" answer is
    ``projects[<abs cwd>].hasTrustDialogAccepted`` in that ACCOUNT's
    ``.claude.json`` (:func:`_claude_json_path`) — private and work are separate
    files, so a folder trusted on one seat still prompts on the other, and an
    unattended launch (``ccc start-job`` from launchd, a tp drive) parks on the
    dialog forever (2026-09-01: gitlab-ci-watch's sandbox clone). The rule here:
    every folder ccc launches into is trusted, on every account. Only that key is
    written, atomically; a missing config (an account that never ran) is left
    alone. Returns True when the file was written.
    """
    import json
    import os
    import tempfile

    target = str(Path(cwd).resolve()) if cwd else os.getcwd()
    # Claude Code's layout: the DEFAULT account's file is the config dir's SIBLING
    # (~/.claude.json next to ~/.claude/), any other account's is inside its dir.
    # Derived from the config dir (not Path.home()) so a test that pins CLAUDE_HOME
    # under tmp can never reach the real ~/.claude.json.
    resolved = _resolve(config_dir)
    path = (
        (resolved.parent / ".claude.json")
        if is_default_config_dir(config_dir)
        else resolved / ".claude.json"
    )
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return False
    entry = projects.get(target)
    if not isinstance(entry, dict):
        entry = {}
        projects[target] = entry
    if entry.get(TRUST_KEY) is True:
        return False
    entry[TRUST_KEY] = True
    fd, tmp = tempfile.mkstemp(prefix=".claude.json.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        return False
    return True


def is_trusted(config_dir: str, cwd: str | Path | None = None) -> bool:
    """Read back whether *cwd* carries the accepted-trust flag under *config_dir*'s account.

    The success predicate :func:`ensure_trusted` deliberately is not (its ``False`` means
    "already trusted" as well as "could not read/write"): callers that must NOT launch
    into a trust dialog call ``ensure_trusted`` and then require this to be ``True``.
    """
    target = str(Path(cwd).resolve()) if cwd else os.getcwd()  # ensure_trusted's own key
    try:
        data = json.loads(_claude_json_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    entry = projects.get(target) if isinstance(projects, dict) else None
    return isinstance(entry, dict) and entry.get(TRUST_KEY) is True


def account_email(config_dir: str) -> str | None:
    """The email currently logged into *config_dir*'s Claude account, or ``None``.

    Reads Claude Code's own ``.claude.json`` (``oauthAccount.emailAddress`` — the
    same field ``/status`` prints as "Email:"). Best-effort and read-only: a
    missing/unreadable/malformed file, or no active OAuth account, returns ``None``.
    Never raises.
    """
    try:
        data = json.loads(_claude_json_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    return email if isinstance(email, str) and email else None


def resolve_card_label(card_label: str) -> str | None:
    """Which configured account label's cache actually backs *card_label*'s card.

    ccc's account labels ("private"/"work") name a config DIR (``claude_accounts``);
    WHICH Claude account is logged into that dir can drift — a bare ``/login`` run in
    a shell with the wrong (or unset) ``CLAUDE_CONFIG_DIR`` silently overwrites it, so
    a dir named "work" can end up holding the private account or vice-versa with no
    visible cause. When *card_label* has a hard-linked email configured
    (``claude_account_emails``), this walks every configured account, reads its
    CURRENT identity (:func:`account_email`), and returns whichever label's live
    email matches that hard link — so e.g. the "work" card always shows the SDSC
    account's numbers regardless of which physical dir currently holds them.

    Returns:

    * *card_label* itself, unchanged, when no hard link is configured for it —
      today's pure path-based behaviour, fully backward compatible.
    * the label whose live identity matches the hard link. When SEVERAL dirs hold
      that identity at once (an accidental ``/login`` duplicated one account into
      another dir), the card's OWN dir wins the tie: the duplicate is the drifted
      copy, and first-match-in-config-order once bound the work card to the private
      dir's cache — whose OAuth fetch was in 429 backoff — showing a stale Fable
      figure while the work dir's own cache was fresh.
    * ``None`` when a hard link IS configured but no configured account currently
      matches it (logged out, or the seat lapsed) — callers must render this as "no
      data", never fall back to a path-based guess that could be wrong again.
    """
    expected = config.claude_account_email_map().get(card_label)
    if not expected:
        return card_label
    matches = [
        label
        for label, path in config.claude_config_dirs().items()
        if account_email(str(path)) == expected
    ]
    if not matches:
        return None
    return card_label if card_label in matches else matches[0]


def account_config_dir(label: str) -> str:
    """The absolute config dir for account *label* ("" when the label is unknown)."""
    path = config.claude_config_dirs().get(label)
    return str(path) if path is not None else ""


# The ccc ``model`` column marks each row with a per-account glyph: 🏠 for the ``private``
# (cpriv) account, 💼 for the ``work`` (cwork) account. Each is a width-2 colored emoji + a
# trailing space (3 terminal cells, matching the width-2 badge emoji convention in
# ``tabsymbol``); ``_NO_HOME`` is the same width in blanks so the model text stays
# column-aligned on rows for any OTHER (unknown / third) account. In single-account mode the
# marker is empty (every row would carry it, so it would mean nothing). The statusline
# (``dotfiles/claude/.claude/statusline-command.sh``) shows the SAME 🏠/💼 after the model —
# keep the two in sync.
_HOME_GLYPH = "🏠 "
_WORK_GLYPH = "💼 "
_NO_HOME = "   "


def _bills_account(config_dir: str, label: str, dirs: dict[str, Path]) -> bool:
    """True when *config_dir* resolves to the account *label*'s dir (empty ⇒ never)."""
    if not config_dir:
        return False
    target = dirs.get(label)
    return target is not None and _resolve(config_dir) == _resolve(target)


def is_private_account(config_dir: str, dirs: dict[str, Path] | None = None) -> bool:
    """True when *config_dir* bills to the account labelled ``private`` (cpriv).

    An empty *config_dir* is the multi-account UNKNOWN sentinel (id live under two
    accounts, or never observed) — never private. Compares RESOLVED paths so a symlinked
    or differently-spelled dir still matches. *dirs* is the already-parsed account map
    (pass it to avoid a config-file read per row); ``None`` reads the config.
    """
    return _bills_account(
        config_dir, "private", config.claude_config_dirs() if dirs is None else dirs
    )


def is_work_account(config_dir: str, dirs: dict[str, Path] | None = None) -> bool:
    """True when *config_dir* bills to the account labelled ``work`` (cwork). See
    :func:`is_private_account` for the empty-``config_dir`` / resolution semantics."""
    return _bills_account(config_dir, "work", config.claude_config_dirs() if dirs is None else dirs)


def home_marker(config_dir: str, dirs: dict[str, Path] | None = None) -> str:
    """A fixed-width per-account glyph for the ccc ``model`` column (TUI + ``ccc ls``).

    Returns ``"🏠 "`` for the ``private`` (cpriv) account, ``"💼 "`` for the ``work``
    (cwork) account, an equal-width blank for any OTHER account (so the model text stays
    aligned), and ``""`` in single-account mode (the mark would be on every row and so
    carries no signal). *dirs* is the already-parsed account map — pass it to avoid a
    config read per row.

    Purely PATH-based: row-rendering callers should use the identity-corrected pair
    :func:`effective_home_markers` + :func:`home_marker_from` instead, which survive a
    drifted login (see :func:`resolve_card_label`).
    """
    dirs = config.claude_config_dirs() if dirs is None else dirs
    if len(dirs) <= 1:  # single account → the mark would be on every row → drop it
        return ""
    if is_private_account(config_dir, dirs):
        return _HOME_GLYPH
    if is_work_account(config_dir, dirs):
        return _WORK_GLYPH
    return _NO_HOME


def effective_home_markers(dirs: dict[str, Path] | None = None) -> dict[str, str]:
    """Resolved account-dir path → that dir's IDENTITY-CORRECTED model-column marker.

    :func:`home_marker` decides purely by PATH, so after the drift
    :func:`resolve_card_label` guards against (a bare ``/login`` in the wrong shell
    silently swaps which account a config dir holds) every row of that dir claims the
    wrong account — a session that actually bills ``work`` still shows 🏠 because it ran
    under the dir *named* private. This is the row-rendering counterpart of the
    statusline's :func:`current_account_glyph`: it asks each configured dir who is
    CURRENTLY logged into it (:func:`effective_account_label`) and maps its resolved path
    to the glyph of the account it TRULY bills.

    Build the map ONCE per render / listing and look each row up with
    :func:`home_marker_from` — every entry costs one ``.claude.json`` read, which a
    per-row call would repeat for every session. Returns ``{}`` in single-account mode
    (``len(dirs) <= 1``), the same "the mark would be on every row ⇒ no signal" rule
    :func:`home_marker` follows, which :func:`home_marker_from` renders as no marker at
    all. *dirs* is the already-parsed account map; ``None`` reads the config.
    """
    dirs = config.claude_config_dirs() if dirs is None else dirs
    if len(dirs) <= 1:  # single account → the mark would be on every row → drop it
        return {}
    markers: dict[str, str] = {}
    for path in dirs.values():
        label = effective_account_label(str(path))
        markers[str(_resolve(path))] = {"private": _HOME_GLYPH, "work": _WORK_GLYPH}.get(
            label, _NO_HOME
        )
    return markers


def home_marker_from(config_dir: str, markers: dict[str, str]) -> str:
    """One row's model-column marker, looked up in a precomputed *markers* map.

    The per-row half of :func:`effective_home_markers` — a plain dict lookup, so the
    identity resolution is paid once per render instead of once per row. Returns ``""``
    when *markers* is empty (single-account mode ⇒ no marker at all) and the equal-width
    blank for the multi-account UNKNOWN sentinel (``config_dir == ""``: an id live under
    two accounts, or never observed) or a dir no configured account claims — so the model
    text stays column-aligned on those rows.
    """
    if not markers:  # single account (or nothing configured) → no marker at all
        return ""
    if not config_dir:  # multi-account UNKNOWN → blanks only, keeping the column aligned
        return _NO_HOME
    return markers.get(str(_resolve(config_dir)), _NO_HOME)


def _export_value(config_dir: str) -> str:
    """The exact string to export as ``CLAUDE_CONFIG_DIR`` for *config_dir*.

    Claude hashes the LITERAL value into its Keychain service name, so what we export
    must match what the user's own account shell aliases export byte-for-byte.
    Comparisons in this module resolve symlinks, and :func:`env_config_dir` stamps a
    RESOLVED path into the store — so map back to the CONFIGURED spelling here. Without
    this, a symlinked account dir would export a different string than the alias, hash to
    a different service name, and read as "not authenticated" with no visible cause.
    An unconfigured dir is exported as given (expanded).
    """
    target = _resolve(config_dir)
    for path in config.claude_config_dirs().values():
        if _resolve(path) == target:
            return str(path)
    return str(Path(config_dir).expanduser())


def launch_env(config_dir: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Child env that launches/resumes a session under *config_dir*'s account.

    Starts from a COPY of *base* (``os.environ`` by default), always strips
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR``, then either UNSETS ``CLAUDE_CONFIG_DIR``
    (default account) or SETS it to the account's dir — so an ambient work
    ``CLAUDE_CONFIG_DIR`` never leaks into a private launch (and vice-versa).
    """
    env = dict(os.environ if base is None else base)
    env.pop(_SECURE_VAR, None)
    if is_default_config_dir(config_dir):
        env.pop(_CONFIG_VAR, None)
    else:
        env[_CONFIG_VAR] = _export_value(config_dir)
    return env


def apply_to_environ(config_dir: str) -> None:
    """Pin *config_dir*'s account into ``os.environ`` IN PLACE (for an ``os.execvp``).

    ``os.execvp`` inherits the current ``os.environ``, so mutating it here — strip
    ``CLAUDE_SECURESTORAGE_CONFIG_DIR``, then unset (default account) or set
    ``CLAUDE_CONFIG_DIR`` — pins the account without needing ``execvpe`` (which would
    escape tests that monkeypatch the 2-arg ``os.execvp``).
    """
    os.environ.pop(_SECURE_VAR, None)
    if is_default_config_dir(config_dir):
        os.environ.pop(_CONFIG_VAR, None)
    else:
        os.environ[_CONFIG_VAR] = _export_value(config_dir)
    # The exec that follows starts claude in os.getcwd(): trust it for this
    # account first, or an unattended launch parks on the trust dialog.
    ensure_trusted(config_dir)


def launch_env_prefix(config_dir: str) -> str:
    """A POSIX-shell prefix pinning the account for a launcher command *string*.

    The iTerm / tmux launchers hand a shell a command string (no ``env=`` to pass),
    so the pin must survive INTO the string. Returns a trailing-space snippet to
    prepend verbatim, e.g. ``"unset CLAUDE_SECURESTORAGE_CONFIG_DIR; export
    CLAUDE_CONFIG_DIR=/home/user/.claude-work; "``. The default account unsets
    both vars.
    """
    if is_default_config_dir(config_dir):
        return f"unset {_SECURE_VAR} {_CONFIG_VAR}; "
    quoted = shlex.quote(_export_value(config_dir))
    return f"unset {_SECURE_VAR}; export {_CONFIG_VAR}={quoted}; "


# --------------------------------------------------------------------------- #
# Per-session launch policy (account pin + the session's own env flags)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LaunchTarget:
    """A minimal :class:`SessionLaunch` for surfaces that only carry the two values.

    ``terminal``/``snapshot`` build their command STRINGS from loose arguments rather than
    a full session row; this keeps them on the same policy without widening their APIs.
    """

    config_dir: str = ""
    no_codex: bool = False


class SessionLaunch(Protocol):
    """The two fields a launch surface needs off a session row (structural).

    A :class:`command_center.models.Session` satisfies it, and so does any small stand-in
    (``snapshot.SnapPane``, :class:`LaunchTarget`) — the helpers below never need the whole
    row. Read-only members on purpose: a frozen dataclass must satisfy it too.
    """

    @property
    def config_dir(self) -> str:
        """Absolute config dir of the Claude account this launch bills to ("" = unknown)."""

    @property
    def no_codex(self) -> bool:
        """Whether this launch must ban every Codex integration."""


def session_env_flags(session: SessionLaunch) -> dict[str, str]:
    """The NON-account env a session's launch must carry (the single source of that list).

    Today exactly one entry: ``CCC_NO_CODEX=1`` when the row's ``no_codex`` flag is set,
    which is the kill switch every Codex integration honours (plan-gate debates, the
    optional-offload hook, ``codex-in-claude``). It is only ever ADDED — an ambient
    ``CCC_NO_CODEX`` from the parent shell is left exactly as it was, so a session
    launched from inside a no-codex shell keeps that inheritance.
    """
    return {"CCC_NO_CODEX": "1"} if getattr(session, "no_codex", False) else {}


def session_launch_env(
    session: SessionLaunch, base: dict[str, str] | None = None
) -> dict[str, str]:
    """Child env for launching/resuming *session*: the account pin plus its own flags."""
    env = launch_env(session.config_dir, base)
    env.update(session_env_flags(session))
    return env


def session_apply_to_environ(session: SessionLaunch) -> None:
    """The :func:`apply_to_environ` rendering: pin IN PLACE for an ``os.execvp``."""
    apply_to_environ(session.config_dir)
    os.environ.update(session_env_flags(session))


def session_launch_env_prefix(session: SessionLaunch) -> str:
    """The :func:`launch_env_prefix` rendering: a POSIX-shell snippet for a command string."""
    prefix = launch_env_prefix(session.config_dir)
    exports = "".join(
        f"export {name}={shlex.quote(value)}; "
        for name, value in sorted(session_env_flags(session).items())
    )
    return prefix + exports


def relaunch_command(session: SessionLaunch, session_id: str, cwd: str) -> str:
    """The ONE-LINE shell command that relaunches *session_id* under *session*'s account.

    Typed by ``ccc switch-now`` into the session's OWN, already-interactive shell once
    the old Claude process has exited: ``cd <cwd> && ( <env pin> claude --resume <id> )``.
    The pin is :func:`session_launch_env_prefix` — ccc's single billing pin, plus the
    session's own env flags — and the subshell keeps its ``export`` from persisting in
    the user's shell. ``claude`` is left to the interactive shell to resolve, so a user's
    own alias/wrapper (permission mode, pre-flight checks) applies exactly as it does to
    their manual launches. Never anything configurable: a typo'd launcher would be a
    second, unchecked account selector.
    """
    quoted_id = shlex.quote(session_id)
    cd_prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
    return f"{cd_prefix}( {session_launch_env_prefix(session)}claude --resume {quoted_id} )"


def env_config_dir() -> str:
    """The account this shell is billing to, from the in-session env.

    Hooks run INSIDE a Claude session, so ``CLAUDE_CONFIG_DIR`` is authoritative:
    when set, that account's resolved dir; when unset, the default account
    (``claude_home()``). Always a concrete absolute path (never "").
    """
    env = os.environ.get(_CONFIG_VAR)
    return str(_resolve(env) if env else default_config_dir())


def live_conflict(session_id: str, adapter: Adapter | None = None) -> bool:
    """True if *session_id* is live under two account registries (a D9 conflict).

    Best-effort (a discover error → no conflict): a conflicting id must never be
    resumed/focused until one side exits, or ccc could silently bill the wrong
    account. Shared by every launch-shaped surface (``ccc resume``, ``ccc jump``);
    *adapter* is injectable for tests, defaulting to a fresh :class:`ClaudeAdapter`.
    """
    try:
        if adapter is None:
            # Lazy: keep this module import-light (hooks' hot path) and free of a
            # module-level accounts ⇄ adapters edge (adapters.claude imports us).
            from .adapters import ClaudeAdapter  # pylint: disable=import-outside-toplevel

            adapter = ClaudeAdapter()
        for live in adapter.discover():
            if live.session_id == session_id and live.conflict:
                return True
    except OSError:
        return False
    return False


def warn_conflict(session_id: str, first: str, second: str) -> None:
    """Warn ONCE that *session_id* is live in two account registries (D9).

    A same-id collision is a conflict, not a race to win — the caller leaves
    ``config_dir`` unstamped and refuses resume/focus until one side exits.
    """
    if session_id in _WARNED_CONFLICTS:
        return
    _WARNED_CONFLICTS.add(session_id)
    print(
        f"ccc: warning: session {session_id[:8]} is live under two Claude accounts "
        f"({account_label(first)} and {account_label(second)}); refusing to attribute "
        "an account until one exits.",
        file=sys.stderr,
    )
