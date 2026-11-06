#!/usr/bin/env python3
"""The Claude usage cache write and account resolution — stdlib only (tp#1806 B2).

``ccc statusline --capture-usage`` merges the status-line ``rate_limits`` into the
account's usage cache on every render. That read-merge-write, the account the capture
belongs to, and the cache's path and config-dir hash live here, importable without
:mod:`command_center.usage` (rich, dataclasses, the whole provider zoo) or
:mod:`command_center.config`. :mod:`usage` and :mod:`config` delegate to this module and
re-export its names, so every caller runs the same code.

Windows are plain ``(used_percentage, resets_at)`` tuples here (``usage.Window`` stays a
dataclass built from them). Every config value is an explicit argument: the caller passes
the app home and the account's resolved config dir.

Imports: the standard library only.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
# pylint: disable=duplicate-code  # config's other "label=…" parsers share these idioms
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

#: A window: ``(used_percentage, resets_at)`` — resets_at in Unix epoch seconds.
WindowTuple = tuple[float, int]

# A Claude account label: it feeds usage-cache filenames, so it may never carry a path
# separator (see :func:`parse_claude_accounts`).
_ACCOUNT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Window at which a stored authoritative OAuth fetch still outranks an incoming
# status-line window in the merge (see :func:`_merge_window`). One hour: long enough to
# survive a persistently-idle session replaying its pre-rebase ``rate_limits`` every 3s,
# short enough that the periodic fetch keeps re-asserting authority.
_OAUTH_AUTHORITY_SEC = 3600

# A window counts as full at this percentage. ``full_since`` (tp#1327) records, per window,
# the FIRST capture that read it full for the window's current boundary. ``captured_at``
# cannot date that: the status line bumps it every few seconds, so a turn is never newer
# than it. The handoff's capped check asks "did a turn succeed on the seat after it filled?"
# and needs the earliest moment the window was known full.
_FULL_PCT = 100.0
# Two readings of one window may name boundaries a few seconds apart (OAuth ISO vs the
# status line's epoch); a genuine new window of the same name is hours away.
_SAME_WINDOW_SLACK_SEC = 3600


def claude_home() -> Path:
    """Root of Claude Code's state (``~/.claude`` unless ``CLAUDE_HOME`` is set)."""
    env = os.environ.get("CLAUDE_HOME")
    return Path(env) if env else Path.home() / ".claude"


def parse_claude_accounts(entries: list[str]) -> dict[str, Path]:
    """Pure ``"label=path"`` parser behind :func:`config.claude_config_dirs`.

    Split out so callers holding an already-loaded ``claude_accounts`` list (the TUI's 5 s
    render tick, the lean status line) can resolve the accounts without re-reading the
    config file. Malformed entries are skipped; nothing valid ⇒ ``{"private": claude_home()}``.
    """
    dirs: dict[str, Path] = {}
    for entry in entries:
        label, sep, raw = entry.partition("=")
        if not sep:
            continue  # no "=" → not a "label=path" entry
        label, raw = label.strip(), raw.strip()
        if not raw or not _ACCOUNT_LABEL_RE.match(label):
            continue  # blank path or a label that could smuggle a path separator
        dirs[label] = Path(raw).expanduser().resolve()
    return dirs or {"private": claude_home()}


def account_from_env(dirs: dict[str, Path]) -> str | None:
    """The Claude account label for the CURRENT statusline capture, or ``None``.

    *dirs* is the configured accounts (``config.claude_config_dirs()``). The statusline
    runs as a child of ``claude``, which exports ``CLAUDE_CONFIG_DIR`` whenever it is set;
    we match that (resolved) path against *dirs*. When the env var is ABSENT the session
    runs under the DEFAULT account (``accounts.py`` keeps it UNSET for the default), so an
    unset env IS the first configured label. A set env matching no configured account
    returns ``None`` (the caller skips the write rather than contaminate a card).
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        try:
            target = Path(env).expanduser().resolve()
        except OSError:
            return None
        for label, path in dirs.items():
            try:
                if path.resolve() == target:
                    return label
            except OSError:
                continue
        return None
    return next(iter(dirs), None)


def account_config_dir(account: str, dirs: dict[str, Path]) -> Path:
    """Resolved config dir for *account* among *dirs* (falls back to :func:`claude_home`)."""
    return dirs.get(account) or claude_home()


def _account_hash(account_dir: Path) -> str:
    """8-hex ``sha256`` of the account's resolved config dir — keys the usage cache."""
    return hashlib.sha256(str(account_dir).encode()).hexdigest()[:8]


def _usage_path(app_home: Path, account: str, account_dir: Path) -> Path:
    """Per-account usage-cache path under *app_home*.

    The default account (label ``private``) keeps the back-compat ``usage.json``; any
    other label writes ``usage-<label>-<hash8>.json`` (``hash8`` = the account's config
    dir hash), so two accounts never share a file and label reuse never collides.
    """
    if account == "private":
        return app_home / "usage.json"
    return app_home / f"usage-{account}-{_account_hash(account_dir)}.json"


@contextlib.contextmanager
def _flock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory ``flock`` on *lock_path* for a critical section.

    Serializes the read-merge-write across every concurrent statusline writer (each
    session calls :func:`write_usage` every few seconds), so the shared cache file is
    never interleaved. The lock file itself is a persistent zero-byte sentinel.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically replace *path* with *payload* via a unique ``mkstemp`` temp file.

    A per-write unique temp name (not a fixed ``.json.tmp``) plus ``os.replace`` means
    concurrent writers cannot clobber one another's temp file.

    The ``finally`` unlink covers every *exception* path (the old ``except OSError``
    leaked on ``TypeError`` from ``json.dump`` and on ``KeyboardInterrupt``, which
    contradicted :func:`write_usage`'s "never raises" contract). It cannot cover
    asynchronous death: Claude Code cancels an in-flight status-line command when the
    next update arrives, and this writer is the last thing the status-line script runs
    every few seconds — so the process is killed outright with no Python unwinding.
    Those orphans are reclaimed by ``usage.sweep_stale_temps``, not here.

    Deliberately no ``fsync``: this is a cache refreshed every few seconds, and the
    extra latency would widen the very cancellation window that strands the temp file.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, path)
        replaced = True
    finally:
        # Only when the replace did NOT happen — otherwise tmp_name IS *path* now.
        if not replaced:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def _window(raw: object) -> WindowTuple | None:
    """A cache/payload window dict as ``(used_percentage, resets_at)``, else ``None``."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    resets = raw.get("resets_at")
    if pct is None or resets is None:
        return None
    try:
        return (float(pct), int(resets))
    except (TypeError, ValueError):
        return None


def _merge_window(
    incoming: WindowTuple | None,
    stored: WindowTuple | None,
    now: int,
    *,
    stored_authoritative: bool = False,
    incoming_stale: bool = False,
) -> WindowTuple | None:
    """Pick the live window between a fresh capture and the persisted one.

    A window's reset is always in the *future*; ``resets_at <= now`` means the
    capturing session simply hasn't talked to the API since the window rolled, so
    its snapshot is stale — discard it. Between two live windows keep the one with
    the later ``resets_at`` (the account-global boundary only ever moves forward, so
    a smaller one is an older session's view), and **on an equal reset keep the
    higher ``used_percentage``**: usage within a fixed window is cumulative, so the
    larger figure is the freshest total — this stops idle sessions, which share the
    same weekly boundary but report a days-old lower total, from flip-flopping the
    card (e.g. 8% ↔ 28%). A genuine reset moves ``resets_at`` forward, so the new
    low percentage is still adopted (later reset beats higher percentage).

    **Authority guard** (*stored_authoritative*): when the stored window was written
    by a recent authoritative OAuth fetch (within :data:`_OAUTH_AUTHORITY_SEC`) and is
    still LIVE, two kinds of incoming status-line windows are REJECTED (stored kept):

    - one whose reset is *later* than the stored one, and
    - ANY window from a stale payload (*incoming_stale* — the reporting session's own
      5-hour window is dead/absent, i.e. it has not talked to the API for >5h).

    Anthropic can rebase windows at a rollout (seen in July 2026: the weekly boundary
    moved BACKWARD Jul 15 → Jul 11, and the weekly percentage was recalibrated DOWN,
    84% → 3%, at the *same* boundary); a long-idle session then replays its pre-rebase
    ``rate_limits`` every 3s, and the plain "later reset wins" / "same reset, higher
    percentage wins" rules would re-pin the stale figures minutes after every
    authoritative fetch heals them. An ACTIVE session's same-reset increase still wins
    (the ~3s fast path between fetches survives). A genuine forward roll is unaffected:
    a truly reset stored window is dead (``resets_at <= now``), dropped from the live
    set, so the incoming later window is adopted as today regardless of authority.
    """
    live = [w for w in (incoming, stored) if w is not None and w[1] > now]
    if not live:
        return None
    if stored_authoritative and stored is not None and stored[1] > now:
        if incoming_stale:
            return stored
        if incoming is not None and incoming[1] > stored[1]:
            return stored
    return max(live, key=lambda w: (w[1], w[0]))


def _window_dict(win: WindowTuple | None) -> dict | None:
    """Serialize a window to its cache-JSON shape (``None`` stays ``None``)."""
    if win is None:
        return None
    return {"used_percentage": win[0], "resets_at": win[1]}


def _int_field(data: dict | None, key: str) -> int:
    """Read an int field from a cache dict, tolerating missing/malformed values (→ 0)."""
    if not data:
        return 0
    try:
        return int(data.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _full_since(
    prev: dict | None, windows: dict[str, WindowTuple | None], now: int
) -> dict[str, int]:
    """``{window: epoch}`` of the first capture that read each window full (tp#1327).

    A window full in *prev* with a still-live boundary within
    :data:`_SAME_WINDOW_SLACK_SEC` of the new one keeps its stored time; a window that
    became full now starts at *now*; one below :data:`_FULL_PCT` drops out.
    """
    stored = prev.get("full_since") if prev else None
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, int] = {}
    for name, win in windows.items():
        if win is None or win[0] < _FULL_PCT:
            continue
        old = _window(prev.get(name)) if prev else None
        since = _int_field(stored, name)
        same = (
            old is not None
            and old[0] >= _FULL_PCT
            and old[1] > now
            and abs(old[1] - win[1]) < _SAME_WINDOW_SLACK_SEC
        )
        out[name] = since if same and 0 < since <= now else now
    return out


def _load_json_dict(path: Path) -> dict | None:
    """*path* parsed as a JSON object; ``None`` when absent, unreadable or not an object."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_validated(app_home: Path, account: str, account_dir: Path) -> dict | None:
    """Load + hash-validate *account*'s cache dict, or ``None`` if absent/refused.

    **Refuses on config-dir-hash mismatch**: if the stored ``config_dir_hash`` is
    present and differs from the account's expected hash the snapshot belonged to a
    DIFFERENT config dir (label reuse), so return ``None``. A hashless payload (a
    pre-existing ``usage.json``) is accepted for the default ``private`` account only.
    """
    data = _load_json_dict(_usage_path(app_home, account, account_dir))
    if data is None:
        return None
    stored_hash = data.get("config_dir_hash")
    if stored_hash is not None:
        if stored_hash != _account_hash(account_dir):
            return None  # this cache belongs to a different config dir → refuse
    elif account != "private":
        return None  # hashless payloads are legacy usage.json — default account only
    return data


def write_usage(
    rate_limits: object,
    *,
    account: str,
    app_home: Path,
    account_dir: Path,
    now: int | None = None,
) -> bool:
    """Merge a ``rate_limits`` snapshot into *account*'s cache; ``True`` if written.

    *app_home* is ccc's state dir, *account_dir* the account's resolved config dir. See
    ``usage.write_usage`` for the full contract: skip a payload with no window, merge
    each window under a per-account ``flock`` (:func:`_merge_window`), stamp the
    ``config_dir_hash``, preserve the OAuth-only fields verbatim. Never raises
    ``OSError``; ``False`` when nothing live survives the merge.
    """
    if not isinstance(rate_limits, dict):
        return False
    incoming_five = _window(rate_limits.get("five_hour"))
    incoming_seven = _window(rate_limits.get("seven_day"))
    if incoming_five is None and incoming_seven is None:
        return False
    now = int(time.time()) if now is None else now
    path = _usage_path(app_home, account, account_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _flock(path.with_name(path.name + ".lock")):
            prev = _read_validated(app_home, account, account_dir)
            prev_five = _window(prev.get("five_hour")) if prev else None
            prev_seven = _window(prev.get("seven_day")) if prev else None
            prev_oauth = _int_field(prev, "oauth_fetched_at") if prev else 0
            authoritative = 0 < prev_oauth and (now - prev_oauth) < _OAUTH_AUTHORITY_SEC
            # A payload whose own 5-hour window is dead/absent comes from a session that
            # has not talked to the API for >5h — its weekly figure is at least that old
            # too, so under authority it may fill gaps but never override (see
            # _merge_window's guard; the 84%-replay case).
            payload_stale = incoming_five is None or incoming_five[1] <= now
            five = _merge_window(
                incoming_five,
                prev_five,
                now,
                stored_authoritative=authoritative,
                incoming_stale=payload_stale,
            )
            seven = _merge_window(
                incoming_seven,
                prev_seven,
                now,
                stored_authoritative=authoritative,
                incoming_stale=payload_stale,
            )
            if five is None and seven is None:
                return False
            payload = {
                "captured_at": now,
                "config_dir_hash": _account_hash(account_dir),
                "five_hour": _window_dict(five),
                "seven_day": _window_dict(seven),
                "full_since": _full_since(prev, {"five_hour": five, "seven_day": seven}, now),
                # OAuth-only fields preserved verbatim (status line never carries them).
                "oauth_fetched_at": prev_oauth,
                "oauth_backoff_until": _int_field(prev, "oauth_backoff_until") if prev else 0,
            }
            _atomic_write_json(path, payload)
    except OSError:
        return False
    return True
