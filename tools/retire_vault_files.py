#!/usr/bin/env python3
"""Archive ccc's retired vault trees into a verified tarball, then (only with ``-x``) remove them.

Two kinds, one per run:

* ``mirrors`` -- the session mirrors under ``running_dir``, ``done_dir`` and
  ``sessions_dir``: only ``.md`` files whose frontmatter carries ``ccc_mirror``
  (``running`` / ``done`` / ``session``), plus the ``ccc_generated: true`` dashboards
  (``running.md`` and ``parked.md`` next to the running root, ``done.md`` inside the done
  root).
* ``future`` -- the job files under ``future_dir`` and ``delete_dir`` (``_archive/``
  included): only ``.md`` files whose frontmatter ``session_id`` is a UUID, plus the
  ``ccc_generated: true`` dashboards (``future.md`` next to the future root, ``delete.md``
  inside the trash). The capture pad (``future_pad``) is never taken. Refuses unless
  ``future_files`` is off in the loaded config and no store row still carries a
  ``future_file`` (run ``ccc sync-future --detach`` first).

Every other file under the roots (a foreign note, a symlink, a temp file) is listed and
left, and so is every directory that still holds one.

Default is a DRY RUN: it lists what it would take and keep and writes nothing. With
``-x`` it writes every candidate into ONE ``ccc-tasks-<kind>-<YYYY-MM-DD>.tar.gz``
(mode 0600; a ``-2``, ``-3``, ... suffix when that name exists, never an overwrite) under
the archive dir, re-opens the tarball and compares the member count, the member names and
each member's sha256 with the source files, and only when everything matches removes the
files (each re-hashed just before its unlink; a changed file is kept) and then the
directories left empty under the roots. Members are stored relative to ``vault_root``, so
``tar -xzf ARCHIVE -C VAULT_ROOT`` restores them. A re-run after success finds nothing and
does nothing.

Usage:
  tools/retire_vault_files.py -k mirrors|future [-a ARCHIVE_DIR] [-x]

Options:
  -k, --kind KIND          ``mirrors`` or ``future`` (required).
  -a, --archive-dir DIR    Where the tarball goes (default: ``<ccc app home>/archive``,
                           outside the vault).
  -x, --execute            Write + verify the archive, then remove the archived files.
                           Without it: dry run, nothing is written or removed.
  -h, --help               Show this help and exit.

Examples:
  tools/retire_vault_files.py -k mirrors
  tools/retire_vault_files.py -k mirrors -x
  tools/retire_vault_files.py -k future -a /path/to/archive -x

Exit codes: 0 done (or nothing to do), 1 archive/verify/remove failure (nothing removed
when the archive did not verify), 2 refused (preconditions) or usage.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sqlite3
import sys
import tarfile
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# pylint: disable=wrong-import-position
from command_center import config  # noqa: E402

KINDS = ("mirrors", "future")
#: ``ccc_mirror`` frontmatter values the mirror export wrote.
MIRROR_KINDS = frozenset({"running", "done", "session"})
#: Root config keys per kind (the mirror keys may be gone from ``Config`` one day, so
#: they are also read from the raw TOML and finally from these historical defaults).
_ROOT_KEYS: dict[str, tuple[str, ...]] = {
    "mirrors": ("running_dir", "done_dir", "sessions_dir"),
    "future": ("future_dir", "delete_dir"),
}
_HISTORIC_DEFAULTS: dict[str, str] = {
    "vault_root": "~/obsidian",
    "running_dir": "~/obsidian/01-llm/ccc-tasks/running",
    "done_dir": "~/obsidian/01-llm/ccc-tasks/done",
    "sessions_dir": "~/obsidian/01-llm/ccc-tasks/sessions",
    "future_dir": "~/obsidian/01-llm/ccc-tasks/future",
    "delete_dir": "~/obsidian/01-llm/ccc-tasks/delete",
    "future_pad": "~/obsidian/01-llm/ccc-tasks/new-prompt.md",
}
_MIRROR_SWITCHES = ("mirror_running", "mirror_done", "mirror_sessions")
_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_CHUNK = 1 << 20


@dataclass
class Candidate:
    """One file to archive: its path, archive member name and content hash."""

    path: Path
    arcname: str
    sha256: str
    size: int


@dataclass
class Plan:
    """What one run would take and keep."""

    kind: str
    base: Path
    roots: list[Path]
    take: list[Candidate] = field(default_factory=list)
    keep: list[tuple[Path, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def _raw_toml() -> dict[str, object]:
    try:
        with config.config_path().open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _cfg_path(cfg: config.Config, key: str, raw: dict[str, object]) -> Path:
    """Expanded path of *key*: the loaded config, else the raw TOML, else the old default."""
    value = getattr(cfg, key, None)
    if not isinstance(value, str) or not value.strip():
        raw_value = raw.get(key)
        value = raw_value if isinstance(raw_value, str) and raw_value.strip() else None
    if value is None:
        value = _HISTORIC_DEFAULTS[key]
    return config.guard_vault_path(Path(value).expanduser())


def _dashboards(kind: str, cfg: config.Config, raw: dict[str, object]) -> list[Path]:
    if kind == "mirrors":
        running = _cfg_path(cfg, "running_dir", raw)
        done = _cfg_path(cfg, "done_dir", raw)
        return [running.parent / "running.md", running.parent / "parked.md", done / "done.md"]
    future = _cfg_path(cfg, "future_dir", raw)
    delete = _cfg_path(cfg, "delete_dir", raw)
    return [future.parent / "future.md", delete / "delete.md"]


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_frontmatter(path: Path) -> dict[str, str]:
    """The top-level ``key: scalar`` lines of *path*'s leading frontmatter (``{}`` if none).

    Only the flat scalars ccc writes are needed (``ccc_mirror``, ``session_id``,
    ``ccc_generated``), so indented / list lines are skipped and quotes stripped; no YAML
    library is involved.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    match = _FM_RE.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line or line[0] in " \t-#" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out.setdefault(key.strip(), _unquote(value.split(" #", 1)[0]))
    return out


def _is_generated(fm: dict[str, str]) -> bool:
    return fm.get("ccc_generated", "").strip().lower() == "true"


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        uuid.UUID(value.strip())
    except ValueError:
        return False
    return True


def classify(path: Path, kind: str) -> str | None:  # pylint: disable=too-many-return-statements
    """``None`` when *path* is ccc's to retire for *kind*, else the reason it is kept."""
    if path.is_symlink():
        return "symlink"
    if not path.is_file():
        return "not a regular file"
    if path.suffix != ".md":
        return "not a .md file"
    fm = read_frontmatter(path)
    if not fm:
        return "no frontmatter"
    if _is_generated(fm):
        return None
    if kind == "mirrors":
        if str(fm.get("ccc_mirror") or "") in MIRROR_KINDS:
            return None
        return "no ccc_mirror marker"
    if _is_uuid(fm.get("session_id")):
        return None
    return "no session_id UUID"


def file_sha256(path: Path) -> str:
    """Hex sha256 of *path*'s bytes (read in 1 MiB chunks)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _arcname(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def _walk_files(root: Path) -> list[Path]:
    """Every non-directory entry under *root* (symlinked dirs are not followed), sorted."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        out.extend(here / name for name in filenames)
        # a symlink to a directory shows up in dirnames; it is listed, never descended
        out.extend(here / name for name in dirnames if (here / name).is_symlink())
    return sorted(out)


def build_plan(kind: str, cfg: config.Config) -> Plan:
    """Walk the *kind*'s roots and dashboards; split into take / keep."""
    raw = _raw_toml()
    roots = [_cfg_path(cfg, key, raw) for key in _ROOT_KEYS[kind]]
    base = _cfg_path(cfg, "vault_root", raw)
    paths = [p for p in roots if p.exists()] + _dashboards(kind, cfg, raw)
    if not all(p.is_relative_to(base) for p in paths):
        base = Path(os.path.commonpath([str(p) for p in paths])) if paths else base
    plan = Plan(kind=kind, base=base, roots=roots)
    pad = _cfg_path(cfg, "future_pad", raw)
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(_walk_files(root))
    files.extend(p for p in _dashboards(kind, cfg, raw) if p.exists() or p.is_symlink())
    for path in files:
        if path in seen:
            continue
        seen.add(path)
        reason = "capture pad" if path == pad else classify(path, kind)
        if reason is not None:
            plan.keep.append((path, reason))
            continue
        plan.take.append(
            Candidate(
                path=path,
                arcname=_arcname(path, base),
                sha256=file_sha256(path),
                size=path.stat().st_size,
            )
        )
    return plan


# --------------------------------------------------------------------------- #
# preconditions
# --------------------------------------------------------------------------- #
def rows_with_future_file(db: Path) -> tuple[int, int]:
    """``(drafts, other rows)`` whose ``future_file`` is still set (store opened read-only)."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT draft, COUNT(*) FROM sessions "
            "WHERE future_file IS NOT NULL AND future_file != '' GROUP BY draft"
        ).fetchall()
    finally:
        conn.close()
    counts = {int(draft): int(n) for draft, n in rows}
    return counts.get(1, 0), sum(n for d, n in counts.items() if d != 1)


def refusals(kind: str, cfg: config.Config) -> list[str]:
    """Why this run must not proceed (empty = go)."""
    out: list[str] = []
    if kind == "mirrors":
        on = [key for key in _MIRROR_SWITCHES if getattr(cfg, key, False)]
        if on:
            out.append(f"mirror switch(es) still on in the loaded config: {', '.join(on)}")
        return out
    if cfg.future_files:
        out.append("future_files is on in the loaded config (turn it off first)")
    db = config.db_path()
    if not db.is_file():
        out.append(f"store not found: {db}")
        return out
    try:
        drafts, others = rows_with_future_file(db)
    except sqlite3.Error as exc:
        out.append(f"cannot read the store {db}: {exc}")
        return out
    if drafts or others:
        out.append(
            f"{drafts} draft row(s) and {others} other row(s) still carry future_file "
            "(run `ccc sync-future --detach` first)"
        )
    return out


# --------------------------------------------------------------------------- #
# archive
# --------------------------------------------------------------------------- #
def archive_path(archive_dir: Path, kind: str, day: str) -> Path:
    """A fresh ``ccc-tasks-<kind>-<day>[-N].tar.gz`` under *archive_dir* (never existing)."""
    stem = f"ccc-tasks-{kind}-{day}"
    path = archive_dir / f"{stem}.tar.gz"
    n = 2
    while path.exists():
        path = archive_dir / f"{stem}-{n}.tar.gz"
        n += 1
    return path


def write_archive(path: Path, take: list[Candidate]) -> None:
    """Write *take* into the gzip tarball *path* (0600; the bytes hashed are the bytes stored).

    A file whose bytes changed since selection raises ``ValueError`` (nothing is removed).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for cand in take:
            data = cand.path.read_bytes()
            if hashlib.sha256(data).hexdigest() != cand.sha256:
                raise ValueError(f"changed since selection: {cand.path}")
            st = cand.path.stat()
            info = tarfile.TarInfo(cand.arcname)
            info.size = len(data)
            info.mtime = int(st.st_mtime)
            info.mode = st.st_mode & 0o777
            tar.addfile(info, io.BytesIO(data))
        raw.flush()
        os.fsync(raw.fileno())


def verify_archive(path: Path, take: list[Candidate]) -> list[str]:
    """Problems found re-reading *path* against *take* and the source files (empty = match)."""
    problems: list[str] = []
    expected = {cand.arcname: cand for cand in take}
    found: dict[str, str] = {}
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    problems.append(f"unexpected non-file member: {member.name}")
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    problems.append(f"unreadable member: {member.name}")
                    continue
                with handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                if member.name in found:
                    problems.append(f"duplicate member: {member.name}")
                found[member.name] = digest
    except (OSError, EOFError, tarfile.TarError, ValueError) as exc:
        return [f"archive unreadable: {exc}"]
    if len(found) != len(expected):
        problems.append(f"member count {len(found)} != source count {len(expected)}")
    for name, cand in expected.items():
        got = found.get(name)
        if got is None:
            problems.append(f"missing member: {name}")
        elif got != cand.sha256:
            problems.append(f"sha256 mismatch (archive): {name}")
        else:
            try:
                if file_sha256(cand.path) != cand.sha256:
                    problems.append(f"source changed since archiving: {name}")
            except OSError as exc:
                problems.append(f"source unreadable: {name}: {exc}")
    problems.extend(f"extra member: {name}" for name in sorted(set(found) - set(expected)))
    return problems


def remove_files(take: list[Candidate]) -> tuple[int, list[str]]:
    """Unlink every archived file whose bytes still match; ``(removed, failures)``."""
    removed = 0
    failures: list[str] = []
    for cand in take:
        try:
            if file_sha256(cand.path) != cand.sha256:
                failures.append(f"changed after verification, kept: {cand.path}")
                continue
            cand.path.unlink()
            removed += 1
        except OSError as exc:
            failures.append(f"cannot remove {cand.path}: {exc}")
    return removed, failures


def remove_empty_dirs(roots: list[Path]) -> list[Path]:
    """Remove directories left empty under (and including) each root; return them."""
    gone: list[Path] = []
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for dirpath, _dirs, _files in os.walk(root, topdown=False, followlinks=False):
            here = Path(dirpath)
            try:
                here.rmdir()
            except OSError:
                continue  # not empty (a kept file) -- leave it
            gone.append(here)
    return gone


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_plan(plan: Plan) -> None:
    for cand in plan.take:
        print(f"take  {cand.arcname}")
    for path, reason in plan.keep:
        print(f"keep  {path}  ({reason})")
    size = sum(c.size for c in plan.take)
    print(
        f"{plan.kind}: {len(plan.take)} file(s) to archive ({size} bytes), "
        f"{len(plan.keep)} kept; roots: {', '.join(str(r) for r in plan.roots)}"
    )


def run(kind: str, archive_dir: Path, execute: bool) -> int:  # pylint: disable=too-many-return-statements
    """One retire run; returns the exit code."""
    cfg = config.load_config()
    blockers = refusals(kind, cfg)
    if blockers:
        for line in blockers:
            print(f"retire_vault_files: refused: {line}", file=sys.stderr)
        return 2
    plan = build_plan(kind, cfg)
    if not plan.take:
        for path, reason in plan.keep:
            print(f"keep  {path}  ({reason})")
        print(f"{kind}: nothing to retire")
        return 0
    _print_plan(plan)
    if archive_dir.expanduser().resolve().is_relative_to(plan.base.resolve()):
        print(
            f"retire_vault_files: refused: archive dir {archive_dir} is inside {plan.base}",
            file=sys.stderr,
        )
        return 2
    target = archive_path(archive_dir, kind, time.strftime("%Y-%m-%d"))
    if not execute:
        print(f"dry run: would write {target}; nothing written or removed (add -x)")
        return 0
    try:
        write_archive(target, plan.take)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"retire_vault_files: archive failed: {exc}; nothing removed", file=sys.stderr)
        return 1
    problems = verify_archive(target, plan.take)
    if problems:
        for line in problems:
            print(f"retire_vault_files: verify: {line}", file=sys.stderr)
        print(
            f"retire_vault_files: archive {target} did not verify (kept for inspection); "
            "nothing removed",
            file=sys.stderr,
        )
        return 1
    print(f"archive verified: {target} ({len(plan.take)} member(s), sha256 match)")
    removed, failures = remove_files(plan.take)
    gone = remove_empty_dirs(plan.roots)
    for line in failures:
        print(f"retire_vault_files: {line}", file=sys.stderr)
    print(
        f"removed {removed} file(s) and {len(gone)} empty dir(s); "
        f"restore: tar -xzf {target} -C {plan.base}"
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        prog="retire_vault_files.py",
        description=(
            "Archive ccc's retired vault trees (session mirrors or future-job files) into "
            "one verified tarball outside the vault; remove them only with -x."
        ),
        epilog=(
            "examples:\n"
            "  tools/retire_vault_files.py -k mirrors\n"
            "  tools/retire_vault_files.py -k mirrors -x\n"
            "  tools/retire_vault_files.py -k future -a /path/to/archive -x"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-k", "--kind", required=True, choices=KINDS, help="which tree to retire")
    ap.add_argument(
        "-a",
        "--archive-dir",
        help="tarball directory (default: <ccc app home>/archive, outside the vault)",
    )
    ap.add_argument(
        "-x",
        "--execute",
        action="store_true",
        help="write + verify the archive, then remove the files (default: dry run)",
    )
    args = ap.parse_args(argv)
    archive_dir = (
        Path(args.archive_dir).expanduser() if args.archive_dir else config.app_home() / "archive"
    )
    return run(args.kind, archive_dir, args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
