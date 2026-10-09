#!/usr/bin/env python3
"""Memorable session names — generated once, unique, and overridable by hand.

Every tracked Claude Code session gets ONE short name (1-2 lowercase words, at most 24
characters, e.g. ``voice bridge``) that the user, the voice bridge and other agents use to
address it. The rules (PLAN_claude-bridge.md §7.1, decisions D2/D3):

* **Generated once.** Through ccc's single LLM route (``llm_custom_command``, purpose
  ``session-name``, 10 s budget) when a router is configured, else deterministically:
  the repo folder plus at most one meaningful AIM noun (origin ``fallback``). The AIM the
  name came from is stored (``name_source_aim``); later AIM changes never rename.
* **A ``fallback`` name is provisional.** ``ccc sessions -j`` hands one out without an
  LLM call (3 s budget), so the namer (the daemon pass and ``ccc name -A``) may replace
  it exactly ONCE with an LLM name (origin ``llm``) — re-snapshotting the AIM — after
  which the name is fixed. A failed attempt keeps the fallback and counts
  (``name_upgrade_tries``); after :data:`MAX_UPGRADE_TRIES` the fallback stays. ``llm``,
  ``manual`` and ``manual-tab`` names are never replaced by the namer.
* **Unique** (casefolded) among the ACTIVE rows (not done, not archived) of every
  account, decided inside one ``BEGIN IMMEDIATE`` transaction. A clash appends the
  account's label, then a short session-id word.
* **``ccc name -s ID "x y"``** renames (origin ``manual``); a clash is refused. Running
  Claude processes are never renamed by ccc — the command prints ``/rename <name>`` for
  the user to type.
* **A tab title typed by hand is authoritative (D3).** When the live iTerm title differs
  from the one ccc last wrote — after a 5 s grace so a detached ccc write can land, and
  when it is none of the titles ccc itself could have produced — its cleaned text
  becomes the name (origin ``manual-tab``) and ccc never writes that tab's title again.

Import-light: the store, config and LLM layers are imported lazily where used.
"""

# pylint: disable=import-outside-toplevel

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position  # the direct-run shim comes first
import logging
import re
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .models import Session
    from .store import Store

_LOG = logging.getLogger(__name__)

ORIGIN_LLM = "llm"
ORIGIN_FALLBACK = "fallback"
ORIGIN_MANUAL = "manual"
ORIGIN_MANUAL_TAB = "manual-tab"
# Origins the user authored — never replaced by an automatic namer.
MANUAL_ORIGINS = frozenset({ORIGIN_MANUAL, ORIGIN_MANUAL_TAB})

MAX_WORDS = 2
MAX_CHARS = 24
# Failed LLM attempts at upgrading a provisional fallback name before it is kept for good.
MAX_UPGRADE_TRIES = 3
# A name the user typed (``ccc name``, a hand-set tab title) may be longer than a
# generated one, but stays a name, not a sentence.
MAX_MANUAL_CHARS = 48
LLM_TIMEOUT_SEC = 10
# A live title that differs from ccc's last write counts as hand-set only this long after
# that write: ccc's title writes are detached AppleScript runs that land a moment later.
MANUAL_GRACE_MS = 5000

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
# Leading words of an AIM that carry no identity ("make sure the X works" → "x").
_STOPWORDS = frozenset(
    """
    a an the and or but of to for in on at by with from into onto via per as is are be
    been being was were it its this that these those my our your their his her we you
    i me us them all any every each some no not done when once until so then than after
    before about over under up down out off do does did make made makes making ensure
    sure get got let have has had can could should would will must may might implement
    implemented add added adds fix fixed fixes build built builds create created write
    written update updated run runs running set setting work works working use using new
    finish finished complete completed bring keep move check verify test tests tested
    ship shipped deliver land merge investigate debug review clean refactor support
    session job task goal aim now also just only how why what which who where
    continue continued resume please again still yet more less very really here there
    off else somewhere something anything everything nothing other others another same
    """.split()
)
# Nouns too generic to tell two sessions apart ("runai-quickstart items").
_GENERIC_NOUNS = frozenset(
    """
    item items lack lacks user users shared share stuff thing things issue issues problem
    problems part parts file files folder folders dir code changes change step steps plan
    plans todo todos list lists way ways info details detail data place places point points
    root main part side type types kind kinds case cases bit bits lot lots one ones two
    rest end start idea ideas note notes question questions answer answers result results
    version versions error errors bug bugs tp ticket tickets md py sh txt json yaml yml
    toml http https www com org
    """.split()
)

_PROMPT = """\
Name an AI coding session for a list a person scans and speaks aloud. Given its \
done-condition (its "AIM") and its repository folder, reply with ONE memorable name.

Rules:
- One or two plain lowercase English words, at most {max_chars} characters in total.
- Name WHAT is worked on (the feature, the system, the person) — no verbs like "fix" or \
"implement", no ticket numbers, no punctuation, no quotes, no markdown.
- Easy to say and to tell apart from other sessions.

Repository folder: {folder}
AIM: {aim}

Reply with ONLY the name."""


class NameTaken(Exception):
    """``ccc name`` asked for a name another active session already holds."""

    def __init__(self, name: str, holder: str) -> None:
        super().__init__(f"name {name!r} is already used by session {holder[:8]}")
        self.name = name
        self.holder = holder


# --------------------------------------------------------------------------- #
# Text shaping (pure)
# --------------------------------------------------------------------------- #
def ascii_fold(text: str) -> str:
    """*text* with accents folded to ASCII and every other non-ASCII character dropped."""
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def _clip_words(words: list[str], limit: int = MAX_CHARS) -> str:
    """Join *words* (at most :data:`MAX_WORDS`), dropping trailing ones past *limit* chars."""
    words = words[:MAX_WORDS]
    while words and len(" ".join(words)) > limit:
        if len(words) == 1:
            return words[0][:limit].rstrip("-")
        words = words[:-1]
    return " ".join(words)


def normalize_generated(raw: str | None) -> str | None:
    """Reduce a (possibly chatty) model reply to a valid generated name, or ``None``.

    First non-empty line, ASCII-folded, lowercased, only ``[a-z0-9-]`` words kept, at most
    two words and :data:`MAX_CHARS` characters.
    """
    if not raw:
        return None
    line = next((ln for ln in raw.strip().splitlines() if ln.strip()), "")
    if line.startswith("```"):
        lines = [ln for ln in raw.strip().splitlines() if ln.strip() and not ln.startswith("```")]
        line = lines[0] if lines else ""
    words = _WORD_RE.findall(ascii_fold(line).lower())
    words = [w.strip("-") for w in words if w.strip("-")]
    name = _clip_words(words)
    return name or None


def repo_folder(cwd: str) -> str:
    """The basename of the git work tree holding *cwd* (else of *cwd* itself)."""
    if not cwd:
        return ""
    path = Path(cwd).expanduser()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate.name
    return path.name


def _aim_words(aim: str) -> list[str]:
    """The ASCII words of *aim*, absolute paths and URLs dropped (they name no thing)."""
    kept = [
        tok
        for tok in aim.split()
        if not tok.startswith(("/", "~")) and "://" not in tok and not tok.startswith("www.")
    ]
    words = _WORD_RE.findall(ascii_fold(" ".join(kept)).lower().replace("_", " "))
    return [w.strip("-") for w in words if w.strip("-")]


def aim_nouns(aim: str | None, folder: str = "") -> list[str]:
    """The meaningful nouns of *aim*, in order and deduplicated.

    Skipped: stopwords and imperative verbs, generic nouns (``items``, ``users``,
    ``shared`` …), words under 3 characters, numbers, and words the repo *folder* already
    says (``oicd`` in ``oicd-azure-build``).
    """
    if not aim:
        return []
    skip = _STOPWORDS | _GENERIC_NOUNS | set(folder.split("-")) | {folder}
    out: list[str] = []
    for word in _aim_words(aim):
        if len(word) >= 3 and any(c.isalpha() for c in word) and word not in skip:
            skip = skip | {word}
            out.append(word)
    return out


def first_aim_noun(aim: str | None) -> str | None:
    """The first word of *aim* that names a thing (stopwords and generic nouns skipped)."""
    nouns = aim_nouns(aim)
    return nouns[0] if nouns else None


def _folder_word(cwd: str) -> str:
    """The repo folder as ONE lowercase ASCII word (``my_repo`` → ``my-repo``), uncut."""
    raw = ascii_fold(repo_folder(cwd)).lower().replace("_", "-").replace(".", "-")
    words = [w.strip("-") for w in _WORD_RE.findall(raw) if w.strip("-")]
    return words[0] if words else ""


def fallback_candidates(cwd: str, aim: str | None) -> list[str]:
    """Deterministic names in order of preference — each at most 2 words, 24 chars.

    ``folder noun`` for every meaningful AIM noun that fits beside the whole folder name,
    then the folder alone. A folder longer than the budget (cut anyway) is cut so the
    first noun still fits — it is what tells two sessions of the same repo apart.
    """
    folder = _folder_word(cwd)
    nouns = aim_nouns(aim, folder)
    if not folder:
        return [n[:MAX_CHARS] for n in nouns] or ["session"]
    out = [f"{folder} {n}" for n in nouns if len(folder) + 1 + len(n) <= MAX_CHARS]
    if not out and nouns and len(folder) > MAX_CHARS:
        # the folder has to be cut anyway: cut it so the first noun still fits
        cut = folder[: max(1, MAX_CHARS - 1 - len(nouns[0]))].rstrip("-")
        out.append(_clip_words([cut, nouns[0]]))
    out.append(folder[:MAX_CHARS].rstrip("-"))
    return out


def fallback_name(cwd: str, aim: str | None) -> str:
    """The deterministic name: repo folder (+ at most one meaningful AIM noun)."""
    return fallback_candidates(cwd, aim)[0]


def clean_manual(name: str) -> str:
    """A user-typed name (``ccc name``): control characters removed, whitespace collapsed."""
    return _WS_RE.sub(" ", _CONTROL_RE.sub("", name or "")).strip()


def _key(name: str) -> str:
    return name.casefold()


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def llm_name(aim: str, cwd: str, command: str) -> str | None:
    """Ask the configured router (purpose ``session-name``, 10 s) for a name, or ``None``."""
    if not command.strip() or not aim.strip():
        return None
    from . import llm

    prompt = _PROMPT.format(max_chars=MAX_CHARS, folder=repo_folder(cwd) or "-", aim=aim.strip())
    # run_custom (not run_model) only for the 10 s budget: the route is the same single
    # llm_custom_command, and a failure is logged like llm._dispatch does.
    raw = llm.run_custom(
        prompt,
        command,
        timeout=LLM_TIMEOUT_SEC,
        purpose="session-name",
        note=llm.concise_note(aim),
    )
    name = normalize_generated(raw)
    if name is None:
        _LOG.warning("LLM router failed for purpose session-name: %s", command)
    return name


def generate(
    session: Session, cfg: Config, *, use_llm: bool, hint: str = ""
) -> tuple[list[str], str]:
    """``(names, origin)``: the LLM's name when allowed and it answers, else the fallback
    candidates in order of preference (*hint* stands in for a missing AIM, e.g. the name
    Claude Code gave a background job)."""
    aim = session.first_aim or session.aim or ""
    if use_llm and aim:
        name = llm_name(aim, session.cwd, cfg.llm_custom_command)
        if name:
            return [name], ORIGIN_LLM
    return fallback_candidates(session.cwd, aim or hint or None), ORIGIN_FALLBACK


def upgradable(session: Session, cfg: Config) -> bool:
    """True when *session*'s provisional fallback name may still become an LLM name."""
    return (
        session.canonical_name_origin == ORIGIN_FALLBACK
        and bool(session.canonical_name)
        and bool(cfg.llm_custom_command.strip())
        and bool((session.first_aim or session.aim or "").strip())
        and session.name_upgrade_tries < MAX_UPGRADE_TRIES
    )


# --------------------------------------------------------------------------- #
# Uniqueness (one transaction)
# --------------------------------------------------------------------------- #
def _taken(conn: sqlite3.Connection, exclude: str) -> dict[str, str]:
    """``{casefolded name: session id}`` of every ACTIVE named row except *exclude*."""
    rows = conn.execute(
        "SELECT session_id, canonical_name FROM sessions "
        "WHERE canonical_name IS NOT NULL AND canonical_name != '' "
        "AND done = 0 AND archived = 0 AND session_id != ?",
        (exclude,),
    ).fetchall()
    return {_key(str(r[1])): str(r[0]) for r in rows}


def unique_variant(base: str, taken: dict[str, str], account: str, session_id: str) -> str:
    """*base*, or *base* + account label, or + a short session-id word — the first free one."""
    candidates = [base]
    if account:
        candidates.append(f"{base} {account}")
    short = re.sub(r"[^0-9a-z]", "", session_id.lower())
    for width in (4, 6, 8, len(short)):
        if short[:width]:
            candidates.append(f"{base} {short[:width]}")
    for cand in candidates:
        if _key(cand) not in taken:
            return cand
    return f"{base} {session_id}"


def _account_label(config_dir: str) -> str:
    try:
        from . import accounts

        return accounts.account_label(config_dir) if config_dir else ""
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        return ""


def _write_name(  # pylint: disable=too-many-arguments,too-many-locals
    store: Store,
    session_id: str,
    base: str,
    origin: str,
    *,
    source_aim: str | None,
    only_if_unnamed: bool,
    refuse_clash: bool,
    extra: dict[str, object] | None = None,
    upgrade: tuple[str, str] | None = None,
    alternatives: tuple[str, ...] = (),
) -> str | None:
    """Pick a unique variant of *base* and store it, all inside ONE ``BEGIN IMMEDIATE``.

    Returns the stored name, or the existing one when *only_if_unnamed* and the row
    already has a name (``None`` when the row does not exist). *upgrade* =
    ``(origin, name)`` lets *only_if_unnamed* replace exactly that still-current name of
    that origin (a provisional fallback; a peer that renamed meanwhile wins).
    *alternatives* are tried (free as they are) before *base* gets a suffix.
    *refuse_clash* raises :class:`NameTaken` instead of suffixing.
    """
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT canonical_name, config_dir, canonical_name_origin FROM sessions "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        replaceable = upgrade is not None and (str(row[2] or ""), str(row[0] or "")) == upgrade
        if only_if_unnamed and row[0] and not replaceable:
            conn.rollback()
            return str(row[0])
        taken = _taken(conn, session_id)
        if refuse_clash and _key(base) in taken:
            raise NameTaken(base, taken[_key(base)])
        free = next((c for c in (base, *alternatives) if c and _key(c) not in taken), None)
        name = free or unique_variant(base, taken, _account_label(str(row[1] or "")), session_id)
        fields: dict[str, object] = {
            "canonical_name": name,
            "canonical_name_origin": origin,
            "name_source_aim": source_aim,
            **(extra or {}),
        }
        assignments = ", ".join(f"{col} = ?" for col in fields)
        conn.execute(
            f"UPDATE sessions SET {assignments}, updated_at = ? WHERE session_id = ?",  # noqa: S608
            (*fields.values(), int(time.time() * 1000), session_id),
        )
        conn.commit()
        return name
    except BaseException:
        conn.rollback()
        raise


def ensure_name(
    store: Store,
    session_id: str,
    *,
    use_llm: bool,
    cfg: Config | None = None,
    hint: str = "",
) -> str | None:
    """The session's name, generating and storing one first when it has none.

    Generated ONCE — except a provisional ``fallback`` name, which *use_llm* may replace
    exactly once with an LLM name (:func:`upgradable`); ``llm``, ``manual`` and
    ``manual-tab`` names are returned as they are. The LLM call runs BEFORE the
    transaction, so the write lock is never held across a 10 s network call; a peer that
    named (or renamed) the row meanwhile wins.
    """
    session = store.get(session_id)
    if session is None:
        return None
    if cfg is None and (not session.canonical_name or use_llm):
        from .config import load_config

        cfg = load_config()
    if session.canonical_name:
        if not (use_llm and cfg is not None and upgradable(session, cfg)):
            return session.canonical_name
        return _upgrade(store, session, cfg)
    assert cfg is not None
    names, origin = generate(session, cfg, use_llm=use_llm, hint=hint)
    return _write_name(
        store,
        session_id,
        names[0],
        origin,
        source_aim=session.first_aim or session.aim,
        only_if_unnamed=True,
        refuse_clash=False,
        # another AIM noun before a clash suffix — but never the bare folder: §7.1's clash
        # rule (account label, then a session-id word) keeps sessions of one repo apart
        alternatives=tuple(names[1:-1]),
    )


def _upgrade(store: Store, session: Session, cfg: Config) -> str | None:
    """Replace *session*'s provisional fallback name with an LLM one (once), or count a miss."""
    aim = session.first_aim or session.aim or ""
    current = session.canonical_name or ""
    name = llm_name(aim, session.cwd, cfg.llm_custom_command)
    if not name:
        store.conn.execute(
            "UPDATE sessions SET name_upgrade_tries = name_upgrade_tries + 1 "
            "WHERE session_id = ? AND canonical_name_origin = ?",
            (session.session_id, ORIGIN_FALLBACK),
        )
        store.conn.commit()
        return current
    return _write_name(
        store,
        session.session_id,
        name,
        ORIGIN_LLM,
        source_aim=aim,
        only_if_unnamed=True,
        refuse_clash=False,
        upgrade=(ORIGIN_FALLBACK, current),
    )


def rename(store: Store, session_id: str, name: str) -> str:
    """``ccc name``: set *name* (origin ``manual``). Raises :class:`NameTaken` / ``ValueError``.

    Clears ``title_written`` so ccc's next title write lands unconditionally — the user
    asked for this name, so it may replace even a hand-set tab title.
    """
    cleaned = clean_manual(name)
    if not cleaned:
        raise ValueError("a name needs at least one visible character")
    if len(cleaned) > MAX_MANUAL_CHARS:
        raise ValueError(f"a name is at most {MAX_MANUAL_CHARS} characters")
    if store.get(session_id) is None:
        raise ValueError(f"no session {session_id}")
    got = _write_name(
        store,
        session_id,
        cleaned,
        ORIGIN_MANUAL,
        source_aim=None,
        only_if_unnamed=False,
        refuse_clash=True,
        extra={"title_written": None},
    )
    assert got is not None
    return got


def find(store: Store, name: str) -> list[Session]:
    """Active sessions whose name equals *name* case-insensitively (index-backed)."""
    want = clean_manual(name)
    if not want:
        return []
    rows = store.conn.execute(
        "SELECT session_id, canonical_name FROM sessions WHERE lower(canonical_name) = lower(?) "
        "AND done = 0 AND archived = 0",
        (want,),
    ).fetchall()
    out = []
    for sid, stored in rows:
        if _key(str(stored)) == _key(want):
            got = store.get(str(sid))
            if got is not None:
                out.append(got)
    return out


# --------------------------------------------------------------------------- #
# Tab titles (D3)
# --------------------------------------------------------------------------- #
def already_named(session: Session, cleaned: str) -> bool:
    """True when *cleaned* already is *session*'s name — or the clash-suffixed form of it.

    A hand-set title that collided got a suffix (``x`` → ``x work``); the tab still shows
    ``x``, which must not be re-adopted on every pass.
    """
    current = session.canonical_name or ""
    if not current:
        return False
    if _key(current) == _key(cleaned):
        return True
    return session.canonical_name_origin == ORIGIN_MANUAL_TAB and _key(current).startswith(
        _key(cleaned) + " "
    )


def adopt_manual_title(store: Store, session_id: str, cleaned: str) -> str | None:
    """Make the hand-set title *cleaned* the session's name (origin ``manual-tab``).

    A clash with another active session's name gets the usual suffix (account label, then
    a short id word) — names stay unique; the tab keeps showing what the user typed.
    """
    cleaned = clean_manual(cleaned)
    if not cleaned:
        return None
    return _write_name(
        store,
        session_id,
        cleaned,
        ORIGIN_MANUAL_TAB,
        source_aim=None,
        only_if_unnamed=False,
        refuse_clash=False,
    )


def record_title_write(store: Store, session_id: str, core: str, now_ms: int | None = None) -> None:
    """Remember that ccc just wrote *core* as this session's tab title (generation += 1)."""
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    store.conn.execute(
        "UPDATE sessions SET title_written = ?, title_generation = title_generation + 1, "
        "title_written_at = ? WHERE session_id = ?",
        (core, stamp, session_id),
    )
    store.conn.commit()


def title_name(session: Session, cfg: Config) -> str | None:
    """The name a tab title should show for *session* (``None`` when names are off/unset)."""
    if not getattr(cfg, "session_names", False):
        return None
    return session.canonical_name or None


def titles_frozen(session: Session) -> bool:
    """True when ccc must never write this session's tab title (the user set it by hand)."""
    return session.canonical_name_origin == ORIGIN_MANUAL_TAB
