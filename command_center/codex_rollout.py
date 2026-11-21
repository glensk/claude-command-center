#!/usr/bin/env python3
"""Codex rollout evidence: the rate-limit windows and refusals Codex logs per run.

Codex has no usage API; it writes a ``rate_limits`` block onto ``token_count`` events in
``$CODEX_HOME/sessions/**/rollout-*.jsonl``. This module reads those blocks tail-first,
memoises the evidence per file (fingerprint-keyed, persisted in a manifest under the app
home, tp#2513) and labels the refusal codes.

It is a leaf below both :mod:`command_center.codex_in_claude` (the runner) and
:mod:`command_center.usage` (the cards), so neither has to import the other for it
(tp#2522). :mod:`codex_in_claude` re-exports the names its callers use; keep this
module free of imports of either.

Imports: the standard library, plus :mod:`config` / :mod:`usagecache` per call.
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
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_FIVE_HOUR_MINUTES = 5 * 60
_SEVEN_DAY_MINUTES = 7 * 24 * 60


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

    Answered from the per-file evidence memo (:func:`_rollout_evidence`): one tail-first
    pass per file serves this and :func:`_latest_limit_block` alike.
    """
    return _rollout_evidence(path).rate


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
    return _rollout_evidence(path).refusal


# ── rollout evidence: one tail-first pass per file, memoised per process (tp#2513) ──
# Both per-file answers sit near the END of a rollout, so the file is read backwards in
# binary chunks and the walk stops as soon as both are known. Only lines holding
# ``"rate_limits"`` are decoded and parsed; everything else is skipped as raw bytes. The
# first chunk is small (the answer is usually in the last few KiB) and each further one
# doubles up to ``_ROLLOUT_CHUNK``, so a deep walk still needs few reads.
_ROLLOUT_FIRST_CHUNK = 16 * 1024
_ROLLOUT_CHUNK = 256 * 1024
_RATE_LIMITS_NEEDLE = b'"rate_limits"'
# ``(st_dev, st_ino, st_size, st_mtime_ns)``: an append, a rewrite and a same-size,
# same-mtime replacement (new inode) all change it.
_RolloutFingerprint = tuple[int, int, int, int]


@dataclass(frozen=True)
class _RolloutEvidence:
    """Both per-file answers: the newest windowed (or malformed) block, the newest block."""

    rate: _CodexRateSnapshot | None
    refusal: _CodexRefusal | None


_NO_ROLLOUT_EVIDENCE = _RolloutEvidence(rate=None, refusal=None)
# CODEX_HOME partition -> canonical rollout path -> (fingerprint, evidence). ONE entry per
# path; each partition is pruned to its home's current scan set by :func:`_rollout_scan`,
# which bounds the long-lived TUI process. Rollouts are static once a run ends, so after
# the first lookup an unchanged file costs no rollout bytes — across processes too, via
# the persistent manifest below (:func:`_load_rollout_manifest`).
_rollout_memo: dict[str, dict[str, tuple[_RolloutFingerprint, _RolloutEvidence]]] = {}
_rollout_memo_lock = threading.Lock()


def _rollout_fingerprint(st: os.stat_result) -> _RolloutFingerprint:
    """The memo identity of a rollout file's stat result."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _rollout_partition(canonical: str) -> str:
    """The CODEX_HOME a canonical rollout path belongs to (the dir above ``sessions/``)."""
    head, sep, _tail = canonical.rpartition(f"{os.sep}sessions{os.sep}")
    return head if sep else os.path.dirname(canonical)


def _scan_rollout_lines(lines: list[bytes], fallback: int, found: dict[str, Any]) -> bool:
    """Feed *lines* (newest first) into *found*; True once both answers are known.

    ``found`` holds ``refusal`` (the newest block of any kind) and ``rate`` (the newest
    windowed or malformed block). A candidate line is decoded and split exactly like the
    old whole-file ``read_text().splitlines()`` did, so the answers are identical.
    """
    for raw in lines:
        if _RATE_LIMITS_NEEDLE not in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        for line in reversed(text.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            rate_limits = _dig_rate_limits(obj)
            if rate_limits is None:
                continue
            captured_at = _event_timestamp(obj, fallback)
            if found.get("refusal") is None:
                reached = rate_limits.get("rate_limit_reached_type")
                found["refusal"] = _CodexRefusal(
                    captured_at=captured_at,
                    reached_type=reached.strip() if isinstance(reached, str) else "",
                )
            windows, malformed = _windows_by_duration(rate_limits)
            if windows or malformed:
                found["rate"] = _CodexRateSnapshot(
                    captured_at=captured_at, windows=windows, malformed=malformed
                )
                return True
    return False


def _read_rollout_tail(path: str) -> tuple[_RolloutEvidence, _RolloutFingerprint]:
    """One backwards pass over *path*: its evidence and the fingerprint of what was read.

    Reads from the end in chunks growing from ``_ROLLOUT_FIRST_CHUNK`` to
    ``_ROLLOUT_CHUNK`` bytes, carrying the partial first line of each chunk into the next
    one; a line longer than a chunk is joined once, not re-copied per chunk. Raises
    :class:`OSError` when the file cannot be read.
    """
    with open(path, "rb") as handle:  # noqa: PTH123  # module-level ``open`` is the test seam
        st = os.fstat(handle.fileno())
        fallback = int(st.st_mtime)
        found: dict[str, Any] = {}
        pos = st.st_size
        partial: list[bytes] = []  # pieces of the line that continues past the last chunk
        size = min(_ROLLOUT_FIRST_CHUNK, _ROLLOUT_CHUNK)
        while pos > 0:
            start = max(0, pos - size)
            size = min(size * 2, _ROLLOUT_CHUNK)
            handle.seek(start)
            chunk = handle.read(pos - start)
            pos = start
            if pos > 0 and b"\n" not in chunk:
                partial.append(chunk)  # still inside one long line
                continue
            data = chunk + b"".join(reversed(partial)) if partial else chunk
            lines = data.split(b"\n")
            partial = [lines[0]] if pos > 0 else []
            body = lines[1:] if pos > 0 else lines
            if _scan_rollout_lines(body[::-1], fallback, found):
                break
    evidence = _RolloutEvidence(rate=found.get("rate"), refusal=found.get("refusal"))
    return evidence, _rollout_fingerprint(st)


def _rollout_tail_blocks(
    path: Path,
) -> tuple[_CodexRateSnapshot | None, _CodexRefusal | None]:
    """``(newest windowed block, newest block)`` of one rollout, read tail-first, unmemoised."""
    try:
        evidence, _fingerprint = _read_rollout_tail(str(path))
    except OSError:
        return None, None
    return evidence.rate, evidence.refusal


def _canonical_rollout(path: Path, dirs: dict[str, str] | None = None) -> str:
    """*path* with its directory resolved; *dirs* caches the resolution within one scan.

    Rollout files are regular files, so resolving the directory (once per day dir, not
    once per file) is what makes two spellings of one CODEX_HOME share a memo entry.
    """
    parent, name = os.path.split(os.fspath(path))
    if dirs is None:
        return os.path.join(os.path.realpath(parent), name)
    resolved = dirs.get(parent)
    if resolved is None:
        resolved = dirs[parent] = os.path.realpath(parent)
    return os.path.join(resolved, name)


def _rollout_evidence_at(
    path: Path, dirs: dict[str, str] | None = None
) -> tuple[str, _RolloutEvidence]:
    """``(canonical path, evidence)`` for one rollout, through the per-process memo.

    A memo entry is used only while the file's fingerprint still matches. After a parse
    the file is re-stat'ed: a fingerprint that moved during the read (an append, a
    replacement) retries once, and a result is never stored under a fingerprint that
    does not describe the bytes it came from — it is returned uncached instead.
    """
    canonical = _canonical_rollout(path, dirs)
    try:
        current = _rollout_fingerprint(os.stat(canonical))
    except OSError:
        return canonical, _NO_ROLLOUT_EVIDENCE
    partition = _rollout_partition(canonical)
    with _rollout_memo_lock:
        entry = _rollout_memo.get(partition, {}).get(canonical)
    if entry is not None and entry[0] == current:
        return canonical, entry[1]
    evidence = _NO_ROLLOUT_EVIDENCE
    for _attempt in range(2):
        try:
            evidence, read_as = _read_rollout_tail(canonical)
            after = _rollout_fingerprint(os.stat(canonical))
        except OSError:
            return canonical, _NO_ROLLOUT_EVIDENCE
        if read_as == after:
            with _rollout_memo_lock:
                _rollout_memo.setdefault(partition, {})[canonical] = (after, evidence)
            return canonical, evidence
    return canonical, evidence


# ── persistent rollout evidence manifest (tp#2513 B2) ──
# The memo above, persisted per CODEX_HOME partition so a NEW process reads no rollout
# bytes for unchanged files. ccc's own state dir, never Codex's: Codex owns ``~/.codex``.
# Schema ``{"version": 1, "homes": {<home>: {<canonical path>: {"fingerprint": [dev, ino,
# size, mtime_ns], "rate": ..., "refusal": ...}}}}`` — only parsed quota/refusal fields,
# never rollout text. A disposable cache: every entry is validated before use and only
# trusted while its fingerprint still matches the file on disk; a corrupt, foreign,
# unreadable or unwritable manifest simply means a cold parse. No lock across processes:
# a lost concurrent update costs a later reparse, never a different answer.
_ROLLOUT_MANIFEST_NAME = "codex-rollout-evidence-v1.json"
_ROLLOUT_MANIFEST_VERSION = 1
_ManifestPartitions = dict[str, dict[str, tuple[_RolloutFingerprint, _RolloutEvidence]]]
_rollout_manifest_lock = threading.Lock()
# ``path``: the manifest this process loaded (``None`` = not yet); ``disk``: partition ->
# {canonical path: fingerprint} as last read or written, so a scan that changed nothing
# writes nothing.
_rollout_manifest_state: dict[str, Any] = {"path": None, "disk": {}}


class _ManifestInvalid(ValueError):
    """A manifest entry that fails validation; the entry is skipped."""


def _rollout_manifest_path() -> Path:
    """``<app_home>/codex-rollout-evidence-v1.json``."""
    from . import config  # pylint: disable=import-outside-toplevel  # cheap, per-call

    return config.app_home() / _ROLLOUT_MANIFEST_NAME


def _json_int(raw: object) -> int:
    """*raw* as an int; bools and every other type are invalid."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _ManifestInvalid(raw)
    return raw


def _rate_to_json(rate: _CodexRateSnapshot | None) -> dict[str, Any] | None:
    if rate is None:
        return None
    return {
        "captured_at": rate.captured_at,
        "malformed": rate.malformed,
        "windows": [
            {
                "used_percent": w.used_percent,
                "resets_at": w.resets_at,
                "window_minutes": w.window_minutes,
            }
            for w in rate.windows.values()
        ],
    }


def _rate_from_json(raw: object) -> _CodexRateSnapshot | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("malformed"), bool):
        raise _ManifestInvalid(raw)
    items = raw.get("windows")
    if not isinstance(items, list):
        raise _ManifestInvalid(raw)
    windows: dict[int, _CodexRateWindow] = {}
    for item in items:
        if not isinstance(item, dict):
            raise _ManifestInvalid(item)
        pct = item.get("used_percent")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            raise _ManifestInvalid(item)
        window = _CodexRateWindow(
            float(pct), _json_int(item.get("resets_at")), _json_int(item.get("window_minutes"))
        )
        if window.window_minutes <= 0 or window.window_minutes in windows:
            raise _ManifestInvalid(item)
        windows[window.window_minutes] = window
    if not windows and not raw["malformed"]:
        raise _ManifestInvalid(raw)  # the reader never stores such a block
    return _CodexRateSnapshot(
        captured_at=_json_int(raw.get("captured_at")),
        windows=windows,
        malformed=raw["malformed"],
    )


def _refusal_from_json(raw: object) -> _CodexRefusal | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("reached_type"), str):
        raise _ManifestInvalid(raw)
    return _CodexRefusal(
        captured_at=_json_int(raw.get("captured_at")), reached_type=raw["reached_type"]
    )


def _manifest_entry(
    home: str, path: object, raw: object
) -> tuple[_RolloutFingerprint, _RolloutEvidence]:
    """One validated manifest entry; raises :class:`_ManifestInvalid` otherwise."""
    if (
        not isinstance(path, str)
        or not os.path.isabs(path)
        or _rollout_partition(path) != home
        or not os.path.basename(path).startswith("rollout-")
        or not isinstance(raw, dict)
    ):
        raise _ManifestInvalid(path)
    fingerprint = raw.get("fingerprint")
    if not isinstance(fingerprint, list) or len(fingerprint) != 4:
        raise _ManifestInvalid(fingerprint)
    fp = (
        _json_int(fingerprint[0]),
        _json_int(fingerprint[1]),
        _json_int(fingerprint[2]),
        _json_int(fingerprint[3]),
    )
    evidence = _RolloutEvidence(
        rate=_rate_from_json(raw.get("rate")), refusal=_refusal_from_json(raw.get("refusal"))
    )
    return fp, evidence


def _read_rollout_manifest(path: Path) -> _ManifestPartitions:
    """The valid entries of the manifest at *path*; ``{}`` when absent or unusable.

    Read with :mod:`pathlib`, not the module-level ``open`` (that is the rollout test
    seam). Invalid entries are dropped one by one; a wrong version drops everything.
    """
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(payload, dict):
        return {}
    version = payload.get("version")
    homes = payload.get("homes")
    if isinstance(version, bool) or version != _ROLLOUT_MANIFEST_VERSION:
        return {}
    if not isinstance(homes, dict):
        return {}
    partitions: _ManifestPartitions = {}
    for home, entries in homes.items():
        if not isinstance(home, str) or not isinstance(entries, dict):
            continue
        valid: dict[str, tuple[_RolloutFingerprint, _RolloutEvidence]] = {}
        for name, raw in entries.items():
            try:
                valid[name] = _manifest_entry(home, name, raw)
            except (_ManifestInvalid, TypeError, ValueError, OverflowError):
                continue
        if valid:
            partitions[home] = valid
    return partitions


def _manifest_fingerprints(partitions: _ManifestPartitions) -> dict[str, dict[str, Any]]:
    return {
        home: {name: entry[0] for name, entry in entries.items()}
        for home, entries in partitions.items()
    }


def _load_rollout_manifest() -> None:
    """Seed the memo from the manifest, once per process (and per manifest path).

    Existing memo entries win (they are at least as fresh); seeded ones are trusted only
    while their fingerprint matches, exactly like entries this process parsed itself.
    """
    path = _rollout_manifest_path()
    if _rollout_manifest_state["path"] == str(path):
        return
    with _rollout_manifest_lock:
        if _rollout_manifest_state["path"] == str(path):
            return
        partitions = _read_rollout_manifest(path)
        with _rollout_memo_lock:
            for home, entries in partitions.items():
                bucket = _rollout_memo.setdefault(home, {})
                for name, entry in entries.items():
                    bucket.setdefault(name, entry)
        _rollout_manifest_state["disk"] = _manifest_fingerprints(partitions)
        _rollout_manifest_state["path"] = str(path)


def _save_rollout_manifest(homes: set[str]) -> None:
    """Persist *homes*' memo partitions if they differ from what the manifest holds.

    Read-merge-write: the file is re-read so other homes' partitions (possibly written by
    another process) survive. A failed write is remembered as done, so an unwritable
    state dir costs one attempt per change, not one per refresh.
    """
    with _rollout_memo_lock:
        current = {home: dict(_rollout_memo.get(home, {})) for home in homes}
    with _rollout_manifest_lock:
        disk: dict[str, dict[str, Any]] = _rollout_manifest_state["disk"]
        changed = {
            home
            for home, entries in current.items()
            if {name: entry[0] for name, entry in entries.items()} != disk.get(home, {})
        }
        if not changed:
            return
        path = _rollout_manifest_path()
        merged = _read_rollout_manifest(path)
        for home in changed:
            if current[home]:
                merged[home] = current[home]
            else:
                merged.pop(home, None)
        payload = {
            "version": _ROLLOUT_MANIFEST_VERSION,
            "homes": {
                home: {
                    name: {
                        "fingerprint": list(fp),
                        "rate": _rate_to_json(evidence.rate),
                        "refusal": (
                            None
                            if evidence.refusal is None
                            else {
                                "captured_at": evidence.refusal.captured_at,
                                "reached_type": evidence.refusal.reached_type,
                            }
                        ),
                    }
                    for name, (fp, evidence) in entries.items()
                }
                for home, entries in merged.items()
            },
        }
        from .usagecache import (  # pylint: disable=import-outside-toplevel
            _atomic_write_json,
        )

        with contextlib.suppress(OSError, TypeError, ValueError):
            _atomic_write_json(path, payload)
        _rollout_manifest_state["disk"] = _manifest_fingerprints(merged)
        _rollout_manifest_state["path"] = str(path)


def _rollout_evidence(path: Path) -> _RolloutEvidence:
    """Memoised evidence for one rollout file (see :func:`_rollout_evidence_at`)."""
    _load_rollout_manifest()
    return _rollout_evidence_at(path)[1]


def _rollout_scan(files: list[Path]) -> list[_RolloutEvidence]:
    """Evidence for every file of a home's scan set, then prune that home's memo to it.

    *files* is the complete scan set (already cut to the scan limit); memo entries of
    the same CODEX_HOME partition that are no longer in it are dropped. The memo is
    seeded from the persistent manifest first and the scanned partitions are written
    back when they changed.
    """
    _load_rollout_manifest()
    keep: dict[str, set[str]] = {}
    results: list[_RolloutEvidence] = []
    dirs: dict[str, str] = {}
    for path in files:
        canonical, evidence = _rollout_evidence_at(path, dirs)
        keep.setdefault(_rollout_partition(canonical), set()).add(canonical)
        results.append(evidence)
    with _rollout_memo_lock:
        for partition, paths in keep.items():
            entries = _rollout_memo.get(partition)
            if entries is None:
                continue
            for stale in [name for name in entries if name not in paths]:
                del entries[stale]
    if keep:
        _save_rollout_manifest(set(keep))
    return results


def latest_refusal(scan: list[Path]) -> _CodexRefusal | None:
    """The live refusal in *scan* (rollout files, newest mtime first, already cut).

    Only the NEWEST block across the files decides: an older refusal that a later
    successful call superseded must never keep the seat marked blocked.
    """
    blocks = [ev.refusal for ev in _rollout_scan(scan) if ev.refusal is not None]
    newest = max(blocks, key=lambda item: item.captured_at, default=None)
    return newest if newest is not None and newest.reached_type else None
