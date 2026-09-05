#!/usr/bin/env python3
"""End-to-end acceptance smoke matrix for a BUILT ``ccc`` wheel on a clean machine.

This is the pre-publish acceptance battery. It never touches the developer's real
``$HOME`` / ``~/.claude`` — and it *proves* it didn't:

1. ``uv build`` the wheel (or reuse one passed with ``-w``).
2. Create a scratch sandbox: a temp ``HOME``, a temp ``CLAUDE_HOME`` (``$HOME/.claude``),
   a fresh ``uv venv``, and ``uv pip install`` the wheel into it.
3. Fingerprint the REAL user state the run must not touch (recursive file map + newest
   mtime of ``~/.claude/command-center`` and the mtime+hash of ``~/.claude/settings.json``).
4. Run the acceptance commands inside the sandbox (``HOME`` / ``CLAUDE_HOME`` overridden,
   the venv bin first on ``PATH``, and only a minimal env — ``PATH`` / ``LANG`` / ``TERM``
   — passed through) and assert their exit codes and side effects.
5. Re-fingerprint the real state → it must be unchanged. The only realistic source of a
   diff is the developer's own live daemon/launchd agents rewriting their runtime files
   (``state.db*``, ``*.log``, ``usage.json`` …); those are classified as expected churn.
   A change to ``settings.json``, or a *non-volatile* file added/removed/modified under
   ``command-center``, is a real leak and FAILS the run, naming the path.
6. Print a ✅/❌ matrix and exit non-zero on any failure. The sandbox is deleted unless
   ``-k/--keep`` is given.

Usage:
  tools/smoke_matrix.py [-w WHEEL] [-k] [-v]

Options:
  -w, --wheel PATH   Use this pre-built wheel instead of running ``uv build``.
  -k, --keep         Keep the scratch sandbox instead of deleting it.
  -v, --verbose      Print each command's full stdout/stderr.
  -h, --help         Show this help and exit.

Stdlib + subprocess only (no third-party imports), so it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# Real-state proof: files the developer's own live daemon / launchd agents keep
# rewriting under ~/.claude/command-center. A diff limited to these is expected
# churn from a concurrent real daemon, NOT a leak from this sandbox run (which
# cannot even resolve the real path — CLAUDE_HOME is overridden). Anything else
# that changes is treated as a real leak and fails the run.
# --------------------------------------------------------------------------- #
_VOLATILE_NAMES = {
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "state.db-journal",
    "daemon.log",
    "daemon.err",
    "events.log",
    "future-sync.log",
    "mirror-sync.log",
    "usage.json",
    "copilot_usage.json",
    "alerts.json",
    "jump_selected",
    "jump_request",
    "future_sync.lock",
    "future_sync_state.json",
    "mirror_sync.lock",
    "resume_watch.lock",
    "resume_queue.json",
    "resume_reset.signal",
}
_VOLATILE_SUFFIXES = (".log", ".err", ".lock", ".signal", ".tmp", "-wal", "-shm", "-journal")

# Ccc hook events (mirrors install.HOOK_SPEC) — used only to explain the uninstall check.
_HOOK_EVENTS = frozenset(
    {
        "session-start",
        "user-prompt",
        "session-end",
        "pre-compact",
        "subagent-start",
        "subagent-stop",
        "pre-tool-use",
        "post-tool-use",
        "stop",
        "release-locks",
    }
)


def _is_volatile(rel: str) -> bool:
    """True when *rel* (posix path under command-center) is a daemon-owned runtime file."""
    base = rel.rsplit("/", 1)[-1]
    if base in _VOLATILE_NAMES or base.startswith("state.db") or base.startswith("."):
        return True
    # Per-account usage caches (usage.account_usage_path): usage-<label>-<hash8>.json.
    if base.startswith("usage-") and base.endswith(".json"):
        return True
    return any(base.endswith(suf) for suf in _VOLATILE_SUFFIXES)


# --------------------------------------------------------------------------- #
# result plumbing
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    """One matrix row."""

    label: str
    ok: bool
    detail: str = ""


@dataclass
class Ctx:
    """Shared run context threaded through the checks."""

    home: Path
    claude_home: Path
    venv: Path
    bindir: Path
    verbose: bool
    results: list[Result] = field(default_factory=list)

    def env(self) -> dict[str, str]:
        """The minimal sandbox env: HOME + CLAUDE_HOME + a venv-first PATH + LANG/TERM."""
        env = {
            "HOME": str(self.home),
            "CLAUDE_HOME": str(self.claude_home),
            "PATH": f"{self.bindir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": os.environ.get("TERM", "xterm-256color"),
        }
        return env

    def add(self, label: str, ok: bool, detail: str = "") -> bool:
        """Record a matrix row and return its pass/fail bool."""
        self.results.append(Result(label, ok, detail))
        return ok


# --------------------------------------------------------------------------- #
# subprocess helpers
# --------------------------------------------------------------------------- #
def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    stdin_devnull: bool = False,
    timeout: int = 120,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* capturing text output; never raises on a non-zero exit."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL if stdin_devnull else None,
    )
    if verbose:
        print(f"    $ {' '.join(cmd)}  (rc={proc.returncode})")
        for stream in (proc.stdout, proc.stderr):
            for line in stream.splitlines():
                print(f"      {line}")
    return proc


def _has_traceback(proc: subprocess.CompletedProcess[str]) -> bool:
    blob = f"{proc.stdout}\n{proc.stderr}"
    return "Traceback (most recent call last)" in blob


# --------------------------------------------------------------------------- #
# build + install
# --------------------------------------------------------------------------- #
def build_wheel(uv: str, out_dir: Path, *, verbose: bool) -> Path:
    """``uv build --wheel`` into *out_dir*; return the newest ``.whl``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [uv, "build", "--wheel", "-o", str(out_dir)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if verbose:
        print(proc.stdout)
        print(proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"uv build failed:\n{proc.stdout}\n{proc.stderr}")
    wheels = sorted(out_dir.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise RuntimeError(f"uv build produced no wheel in {out_dir}")
    return wheels[-1]


def make_venv_and_install(uv: str, ctx: Ctx, wheel: Path) -> None:
    """``uv venv`` + ``uv pip install <wheel>`` into the sandbox venv."""
    proc = subprocess.run(
        [uv, "venv", str(ctx.venv)], capture_output=True, text=True, check=False, timeout=300
    )
    if proc.returncode != 0:
        raise RuntimeError(f"uv venv failed:\n{proc.stdout}\n{proc.stderr}")
    py = ctx.venv / "bin" / "python"
    proc = subprocess.run(
        [uv, "pip", "install", "--python", str(py), str(wheel)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"uv pip install failed:\n{proc.stdout}\n{proc.stderr}")


# --------------------------------------------------------------------------- #
# real-state fingerprint
# --------------------------------------------------------------------------- #
def _file_map(root: Path) -> dict[str, int]:
    """relpath (posix) → mtime_ns for every file under *root* (empty if absent)."""
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                rel = p.relative_to(root).as_posix()
                out[rel] = p.lstat().st_mtime_ns
            except OSError:
                continue
    return out


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def snapshot_real(real_claude_home: Path) -> dict:
    """Fingerprint the real user state this run must not touch."""
    cc = real_claude_home / "command-center"
    settings = real_claude_home / "settings.json"
    cc_map = _file_map(cc)
    return {
        "cc_map": cc_map,
        "cc_count": len(cc_map),
        "cc_newest": max(cc_map.values()) if cc_map else None,
        "settings_stat_ns": _stat_ns(settings, follow=True),
        "settings_lstat_ns": _stat_ns(settings, follow=False),
        "settings_hash": _sha256(settings) if settings.exists() else None,
        "settings_realpath": str(settings.resolve()) if settings.exists() else None,
    }


def _stat_ns(path: Path, *, follow: bool) -> int | None:
    try:
        st = path.stat() if follow else path.lstat()
        return st.st_mtime_ns
    except OSError:
        return None


def _iso(ns: int | None) -> str:
    if ns is None:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ns / 1e9))


def compare_real_state(before: dict, after: dict) -> tuple[bool, list[str]]:
    """Return (ok, notes). ok is False on any real (non-volatile) change."""
    notes: list[str] = []
    leaks: list[str] = []

    # settings.json must be byte-identical (content + both mtimes + link target).
    settings_fields = (
        "settings_stat_ns",
        "settings_lstat_ns",
        "settings_hash",
        "settings_realpath",
    )
    if any(before[f] != after[f] for f in settings_fields):
        leaks.append("~/.claude/settings.json changed (content or mtime)")

    # command-center: per-file diff, partitioned into volatile churn vs real leak.
    b, a = before["cc_map"], after["cc_map"]
    added = sorted(set(a) - set(b))
    removed = sorted(set(b) - set(a))
    modified = sorted(k for k in (set(a) & set(b)) if a[k] != b[k])
    for group, names in (("added", added), ("removed", removed), ("modified", modified)):
        volatile = [n for n in names if _is_volatile(n)]
        real = [n for n in names if not _is_volatile(n)]
        if volatile:
            notes.append(f"command-center {group} (daemon churn): {', '.join(volatile)}")
        for n in real:
            leaks.append(f"~/.claude/command-center/{n} {group} (NOT a known daemon file)")

    notes.insert(
        0,
        f"command-center files {before['cc_count']}→{after['cc_count']}  "
        f"newest {_iso(before['cc_newest'])} → {_iso(after['cc_newest'])}",
    )
    notes.append(
        "settings.json mtime "
        f"{_iso(before['settings_stat_ns'])} → {_iso(after['settings_stat_ns'])}  "
        f"(hash {'identical' if before['settings_hash'] == after['settings_hash'] else 'CHANGED'})"
    )
    return (not leaks), notes + [f"LEAK: {x}" for x in leaks]


# --------------------------------------------------------------------------- #
# acceptance checks
# --------------------------------------------------------------------------- #
def _ccc(ctx: Ctx) -> str:
    return str(ctx.bindir / "ccc")


def _load_settings(ctx: Ctx) -> dict:
    path = ctx.claude_home / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _ccc_hook_commands(settings: dict) -> list[str]:
    """Every ``<ccc> hook <event>`` command wired in *settings* (empty when none)."""
    found: list[str] = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for entry in group.get("hooks", []) or []:
                if not isinstance(entry, dict):
                    continue
                cmd = str(entry.get("command", ""))
                parts = cmd.split()
                for i in range(1, len(parts) - 1):
                    if (
                        parts[i] == "hook"
                        and parts[i + 1] in _HOOK_EVENTS
                        and Path(parts[i - 1]).name == "ccc"
                    ):
                        found.append(cmd)
                        break
    return found


def run_checks(ctx: Ctx) -> None:  # pylint: disable=too-many-locals
    """Run the acceptance commands, recording a Result per check."""
    ccc = _ccc(ctx)
    env = ctx.env()
    v = ctx.verbose
    settings_path = ctx.claude_home / "settings.json"
    config_path = ctx.claude_home / "command-center" / "config.toml"

    # 1. ccc --help
    p = _run([ccc, "--help"], env=env, verbose=v)
    ctx.add("ccc --help", p.returncode == 0, f"exit {p.returncode}")

    # 2. ccc ls (empty store)
    p = _run([ccc, "ls"], env=env, verbose=v)
    ctx.add(
        "ccc ls (empty store)", p.returncode == 0 and not _has_traceback(p), f"exit {p.returncode}"
    )

    # 3. ccc demo --ls
    p = _run([ccc, "demo", "--ls"], env=env, verbose=v)
    ctx.add("ccc demo --ls", p.returncode == 0 and not _has_traceback(p), f"exit {p.returncode}")

    # 4. ccc doctor — exit 0 or 1 both OK on a bare machine; a traceback is a FAIL.
    p = _run([ccc, "doctor"], env=env, verbose=v)
    ok = p.returncode in (0, 1) and not _has_traceback(p)
    ctx.add("ccc doctor (0/1, no traceback)", ok, f"exit {p.returncode}")

    # 5. ccc daemon --dry-run
    p = _run([ccc, "daemon", "--dry-run"], env=env, verbose=v)
    ctx.add(
        "ccc daemon --dry-run", p.returncode == 0 and not _has_traceback(p), f"exit {p.returncode}"
    )

    # 6. ccc install-hooks --dry-run → exit 0, settings.json NOT created.
    p = _run([ccc, "install-hooks", "--dry-run"], env=env, verbose=v)
    ok = p.returncode == 0 and not settings_path.exists()
    ctx.add(
        "ccc install-hooks --dry-run (no write)",
        ok,
        f"exit {p.returncode}, settings.json exists={settings_path.exists()}",
    )

    # 7-9. install-hooks → idempotent rerun → uninstall.
    p1 = _run([ccc, "install-hooks"], env=env, verbose=v)
    bytes1 = settings_path.read_bytes() if settings_path.exists() else None
    hooks1 = _ccc_hook_commands(_load_settings(ctx))
    ctx.add(
        "ccc install-hooks (writes hooks)",
        p1.returncode == 0 and bytes1 is not None and len(hooks1) > 0,
        f"exit {p1.returncode}, {len(hooks1)} ccc hook entries",
    )
    p2 = _run([ccc, "install-hooks"], env=env, verbose=v)
    bytes2 = settings_path.read_bytes() if settings_path.exists() else None
    ctx.add(
        "ccc install-hooks rerun (idempotent)",
        p2.returncode == 0 and bytes1 is not None and bytes1 == bytes2,
        f"exit {p2.returncode}, settings.json byte-identical={bytes1 == bytes2}",
    )
    p3 = _run([ccc, "install-hooks", "--uninstall"], env=env, verbose=v)
    hooks_after = _ccc_hook_commands(_load_settings(ctx))
    ctx.add(
        "ccc install-hooks --uninstall (entries gone)",
        p3.returncode == 0 and len(hooks_after) == 0,
        f"exit {p3.returncode}, remaining ccc hook entries={len(hooks_after)}",
    )

    # 10. ccc init --minimal → exit 0, config.toml written.
    p = _run([ccc, "init", "--minimal"], env=env, stdin_devnull=True, verbose=v)
    ctx.add(
        "ccc init --minimal (writes config)",
        p.returncode == 0 and config_path.exists(),
        f"exit {p.returncode}, config.toml exists={config_path.exists()}",
    )

    # 11. non-TTY ccc init (no flags) → exit 3.
    p = _run([ccc, "init"], env=env, stdin_devnull=True, verbose=v)
    ctx.add("ccc init non-TTY (exit 3)", p.returncode == 3, f"exit {p.returncode}")

    # 12-13. the other two console entry points.
    for exe in ("claude-session-continue", "codex-in-claude"):
        p = _run([str(ctx.bindir / exe), "--help"], env=env, verbose=v)
        ctx.add(f"{exe} --help", p.returncode == 0, f"exit {p.returncode}")


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def print_matrix(ctx: Ctx, wheel: Path, state_ok: bool, state_notes: list[str]) -> bool:
    """Render the ✅/❌ matrix + sandbox proof; return whether everything passed."""
    passed = sum(1 for r in ctx.results if r.ok)
    total = len(ctx.results)
    width = max((len(r.label) for r in ctx.results), default=20)

    print("\n" + "=" * 72)
    print(f"SMOKE MATRIX — wheel: {wheel.name}")
    print("=" * 72)
    for r in ctx.results:
        mark = "✅" if r.ok else "❌"
        print(f"{mark}  {r.label.ljust(width)}   {r.detail}")
    print("-" * 72)
    print("sandbox proof — real ~/.claude untouched:")
    for note in state_notes:
        prefix = "  ❌ " if note.startswith("LEAK:") else "     "
        print(f"{prefix}{note}")
    print(f"  {'✅ real state identical' if state_ok else '❌ REAL STATE CHANGED'}")
    print("=" * 72)
    all_ok = passed == total and state_ok
    verdict = "PASS" if all_ok else "FAIL"
    print(f"RESULT: {verdict}  ({passed}/{total} checks + sandbox proof)")
    print("=" * 72)
    return all_ok


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Acceptance smoke matrix for a built ccc wheel on a simulated clean machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-w", "--wheel", type=Path, default=None, help="use this pre-built wheel (skip uv build)"
    )
    parser.add_argument(
        "-k", "--keep", action="store_true", help="keep the scratch sandbox (do not delete)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each command's full output"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build+install the wheel, run the acceptance battery, prove real state untouched."""
    args = _parse_args(argv)
    uv = shutil.which("uv")
    if uv is None and args.wheel is None:
        print("error: `uv` is required to build the wheel (or pass -w/--wheel)", file=sys.stderr)
        return 2

    real_claude_home = Path(os.path.expanduser("~")) / ".claude"

    sandbox = Path(tempfile.mkdtemp(prefix="ccc-smoke-"))
    ctx = Ctx(
        home=sandbox / "home",
        claude_home=sandbox / "home" / ".claude",
        venv=sandbox / "venv",
        bindir=sandbox / "venv" / "bin",
        verbose=args.verbose,
    )
    ctx.home.mkdir(parents=True, exist_ok=True)
    ctx.claude_home.mkdir(parents=True, exist_ok=True)

    try:
        print(f"sandbox: {sandbox}")
        if args.wheel is not None:
            wheel = args.wheel.resolve()
            if not wheel.is_file():
                print(f"error: wheel not found: {wheel}", file=sys.stderr)
                return 2
            print(f"using pre-built wheel: {wheel.name}")
        else:
            print("building wheel (uv build --wheel) …")
            assert uv is not None  # guaranteed above (uv or wheel required)
            wheel = build_wheel(uv, sandbox / "dist", verbose=args.verbose)
            print(f"built wheel: {wheel.name}")

        print("creating venv + installing wheel …")
        make_venv_and_install(uv or "uv", ctx, wheel)

        before = snapshot_real(real_claude_home)
        print("running acceptance checks …")
        run_checks(ctx)
        after = snapshot_real(real_claude_home)

        state_ok, state_notes = compare_real_state(before, after)
        all_ok = print_matrix(ctx, wheel, state_ok, state_notes)
        return 0 if all_ok else 1
    finally:
        if args.keep:
            print(f"sandbox kept: {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
