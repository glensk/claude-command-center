#!/usr/bin/env python3
"""Wait until the Claude Code session limit resets, then resume a session.

Built for the "You've hit your session limit / resets 1:10am" situation: exit the
limited session, run this in the SAME folder, leave the terminal open (and the
machine awake), and the session resumes itself one minute after the reset.

The reset time can be given explicitly (``1:10am``) or auto-detected: with no time
argument the script probes ``claude -p`` for the limit message and parses the reset
time out of it (fallback: the end of the active 5h block reported by ``ccusage``, an
hour-floored estimate). After waiting it probes again to verify the limit is really
gone before resuming, re-waiting if not.

This module is the ``claude-session-continue`` console entry point AND the engine the
command center's auto-resume (:mod:`command_center.resume`) shells out to. The CLI
contract it relies on is stable: a positional session id plus an optional positional
time (``now`` / a clock time / ``auto``), ``-w/--wait-only`` (+ ``-s/--signal-file``)
for headless reset detection, and exit code 0 on success. Stdlib-only, no side effects
on import.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time as time_mod
from datetime import datetime, timedelta

DEFAULT_PROMPT = "Continue where you left off."
LAST_SESSION_KEYWORDS = ("last", "latest", "continue")
AUTO_KEYWORDS = ("auto",)
NOW_KEYWORDS = ("now", "immediately")
SAFETY_MARGIN = timedelta(minutes=1)
PROBE_CMD = ["claude", "--print", "--model", "haiku", "hi"]
PROBE_TIMEOUT_S = 300
MAX_AUTO_ATTEMPTS = 12
FALLBACK_WAIT = timedelta(minutes=15)


def _claude_home() -> str:
    """Root of Claude Code's state (``~/.claude`` unless ``CLAUDE_HOME`` is set)."""
    env = os.environ.get("CLAUDE_HOME")
    return env if env else os.path.join(os.path.expanduser("~"), ".claude")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse args (``-h`` works with no dependencies / setup)."""
    parser = argparse.ArgumentParser(
        prog="claude-session-continue",
        description=(
            "Wait until the Claude Code session limit resets, then resume a session "
            "in the current directory via 'claude --resume'. Use it when Claude Code "
            'reports "You\'ve hit your session limit - resets 1:10am": exit the '
            "session, run this, and leave the terminal open. The reset time is "
            "auto-detected when omitted (probe 'claude -p' for the limit message, "
            "fallback ccusage); the planned start time is printed immediately."
        ),
        epilog=(
            "Examples:\n"
            "  %(prog)s last\n"
            "      Auto-detect the reset time, wait, verify, then resume the\n"
            "      most recent session in this folder.\n"
            "  %(prog)s 0199a1b2-aaaa-bbbb-cccc-c3d4e5f6a7b8 1:10am\n"
            "      Wait until 01:11 (today or tomorrow), then resume.\n"
            "  %(prog)s 0199a1b2-aaaa-bbbb-cccc-c3d4e5f6a7b8 13:45\n"
            "      24h time format also works.\n"
            "  %(prog)s last now\n"
            "      Resume immediately without any probing or waiting.\n"
            "  %(prog)s last 1:10am --no-prompt\n"
            "      Open the resumed REPL without auto-submitting a prompt.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help=(
            "Claude Code session id (UUID) to resume, or 'last'/'latest' to resume "
            "the most recent session of the current directory. Optional (and ignored) "
            "with --wait-only, which only waits for the reset."
        ),
    )
    parser.add_argument(
        "time",
        nargs="?",
        default=None,
        help=(
            "reset time, e.g. '1:10am', '01:10', '13:45' (one minute is added as a "
            "safety margin; a time already past today means tomorrow). 'now' resumes "
            "immediately. Omit (or 'auto') to detect the reset time automatically by "
            "probing 'claude -p' and parsing its limit message, with the ccusage "
            "active-block end as fallback; the probe is repeated after waiting to "
            "verify the limit is really gone."
        ),
    )
    parser.add_argument(
        "-p",
        "--prompt",
        default=DEFAULT_PROMPT,
        metavar="TEXT",
        help=f"prompt auto-submitted on resume (default: {DEFAULT_PROMPT!r})",
    )
    parser.add_argument(
        "-n",
        "--no-prompt",
        action="store_true",
        help="do not auto-submit a prompt, just open the resumed session",
    )
    parser.add_argument(
        "-K",
        "--no-skip-permissions",
        action="store_true",
        help=(
            "do not pass --dangerously-skip-permissions (the default passes it so an "
            "unattended resumed session is not blocked by permission prompts)"
        ),
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="print the planned start time and command, then exit",
    )
    parser.add_argument(
        "-w",
        "--wait-only",
        action="store_true",
        help=(
            "wait for the session-limit reset (probe/verify exactly as normal) then "
            "exit 0 WITHOUT resuming. For orchestrators (e.g. ccc) that do the resume "
            "themselves but want this script's reset detection. session_id is ignored "
            "in this mode."
        ),
    )
    parser.add_argument(
        "-s",
        "--signal-file",
        default=None,
        metavar="PATH",
        help=(
            "with --wait-only, create/touch this file once the reset is confirmed "
            "(an atomic signal an orchestrator can poll for)"
        ),
    )
    return parser.parse_args(argv)


def parse_clock_time(raw: str) -> tuple[int, int] | None:
    """Parse '1:10am', '01:10', '13:45', '1am' ... into (hour, minute)."""
    cleaned = raw.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.hour, parsed.minute
        except ValueError:
            continue
    return None


def compute_start(now: datetime, hour: int, minute: int) -> datetime:
    """Today (or tomorrow) at hour:minute, plus one minute safety margin."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + SAFETY_MARGIN
    if target <= now:
        target += timedelta(days=1)
    return target


def parse_limit_message(output: str, now: datetime) -> datetime | None:
    """Extract the reset time from a Claude limit message (incl. +1 min margin).

    Handles the known message variants:
    - 'Claude AI usage limit reached|1749600600'        (epoch s or ms)
    - 'resets 2026-06-12T01:10:00+02:00'                (ISO timestamp)
    - 'resets in 2h 7m'                                 (relative duration)
    - "You've hit your session limit - resets 1:10am"   (clock time)
    """
    match = re.search(r"limit reached\|(\d{10,13})", output)
    if match:
        epoch = int(match.group(1))
        if epoch > 10**12:  # milliseconds
            epoch //= 1000
        return datetime.fromtimestamp(epoch) + SAFETY_MARGIN

    match = re.search(r"resets[^\d]*(\d{4}-\d{2}-\d{2}T[\d:.+Zz-]+)", output)
    if match:
        try:
            parsed = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            return parsed.astimezone().replace(tzinfo=None) + SAFETY_MARGIN
        except ValueError:
            pass

    match = re.search(r"resets in\s+(?:(\d+)\s*h\w*)?\s*,?\s*(?:(\d+)\s*m\w*)?", output, re.I)
    if match and (match.group(1) or match.group(2)):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        return now + timedelta(hours=hours, minutes=minutes) + SAFETY_MARGIN

    match = re.search(
        r"resets?\s+(?:at\s+)?(\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))",
        output,
        re.I,
    )
    if match:
        parsed_clock = parse_clock_time(match.group(1))
        if parsed_clock is not None:
            return compute_start(now, *parsed_clock)
    return None


def probe_limit() -> tuple[bool, datetime | None]:
    """Run a cheap 'claude -p' call; return (limited?, reset target or None)."""
    now = datetime.now()
    try:
        proc = subprocess.run(
            PROBE_CMD,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("⚠️  probe timed out; assuming limit still active", file=sys.stderr)
        return True, None

    output = (proc.stdout or "") + (proc.stderr or "")
    target = parse_limit_message(output, now)
    if target is not None:
        return True, target
    if re.search(r"(usage|session|rate|hour)\s*limit", output, re.I):
        return True, None  # limited, but reset time not parseable
    if proc.returncode != 0:
        print(
            f"⚠️  probe failed (exit {proc.returncode}) without a limit "
            f"message:\n{output.strip()[:500]}",
            file=sys.stderr,
        )
        return True, None  # conservative: wait and retry instead of failing
    return False, None


def ccusage_reset_target() -> datetime | None:
    """End of the active 5h block from ccusage (hour-floored estimate) + 1 min."""
    if shutil.which("ccusage") is None:
        return None
    try:
        proc = subprocess.run(
            ["ccusage", "blocks", "-a", "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        data = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    for block in data.get("blocks", []):
        if block.get("isActive") and block.get("endTime"):
            try:
                end = datetime.fromisoformat(block["endTime"].replace("Z", "+00:00"))
            except ValueError:
                return None
            return end.astimezone().replace(tzinfo=None) + SAFETY_MARGIN
    return None


def auto_wait_loop() -> None:
    """Probe the limit, wait until the detected reset, verify, repeat."""
    for attempt in range(1, MAX_AUTO_ATTEMPTS + 1):
        print(f"🔍 probe {attempt}/{MAX_AUTO_ATTEMPTS}: {' '.join(PROBE_CMD)}")
        limited, target = probe_limit()
        if not limited:
            print("✅ no active session limit")
            return
        if target is None:
            target = ccusage_reset_target()
            if target is not None:
                print("ℹ️  reset time from ccusage (hour-floored estimate)")
        if target is None:
            target = datetime.now() + FALLBACK_WAIT
            print(f"⚠️  limit active but no reset time found; retrying at {target:%H:%M}")
        else:
            print(f"⏲  limit active — waiting until {target:%Y-%m-%d %H:%M} (incl. +1 min margin)")
        wait_until(target)
    sys.exit(f"ERROR: still limited after {MAX_AUTO_ATTEMPTS} attempts, giving up.")


def session_file_path(session_id: str) -> str:
    """Path where Claude Code stores this directory's session transcript."""
    munged = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    return os.path.join(_claude_home(), "projects", munged, f"{session_id}.jsonl")


def resolve_last_session() -> str | None:
    """Most recent session id of the current directory, by transcript mtime."""
    project_dir = os.path.dirname(session_file_path("x"))
    try:
        files = [
            os.path.join(project_dir, name)
            for name in os.listdir(project_dir)
            if name.endswith(".jsonl")
        ]
    except FileNotFoundError:
        return None
    if not files:
        return None
    newest = max(files, key=os.path.getmtime)
    return os.path.basename(newest)[: -len(".jsonl")]


def build_command(args: argparse.Namespace, session_id: str) -> list[str]:
    """Assemble the claude CLI invocation."""
    if session_id.lower() in LAST_SESSION_KEYWORDS:
        cmd = ["claude", "--continue"]
    else:
        cmd = ["claude", "--resume", session_id]
    if not args.no_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if not args.no_prompt:
        cmd.append(args.prompt)
    return cmd


def wait_until(target: datetime) -> None:
    """Sleep with a live countdown until the target time is reached."""
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            break
        hours, rest = divmod(int(remaining), 3600)
        minutes, seconds = divmod(rest, 60)
        print(
            f"\r⏳ resuming in {hours:02d}:{minutes:02d}:{seconds:02d} "
            f"(at {target:%Y-%m-%d %H:%M})   ",
            end="",
            flush=True,
        )
        time_mod.sleep(min(1.0, max(remaining, 0.05)))
    print()


def run_wait_only(args: argparse.Namespace) -> int:
    """Wait for the reset (probe/verify), touch the signal file, exit — no resume.

    The orchestrator path: reuse this script's verified reset detection without
    handing the terminal to ``claude``. Honours the same time argument as a normal
    run ('now' = no wait, 'auto'/omitted = probe-and-verify, else an explicit clock).
    """
    now = datetime.now()
    # session_id is ignored in wait-only mode, so a lone positional ('now', a clock
    # time) lands in session_id, not time — accept it from either slot.
    raw_time = (args.time or args.session_id or "auto").strip().lower()
    if raw_time in NOW_KEYWORDS:
        print("✅ --wait-only 'now': no wait requested")
    elif raw_time in AUTO_KEYWORDS:
        print(f"▶ --wait-only: auto-detecting reset via {' '.join(PROBE_CMD)}")
        if not args.dry_run:
            auto_wait_loop()
    else:
        parsed_clock = parse_clock_time(raw_time)
        if parsed_clock is None:
            sys.exit(
                f"ERROR: cannot parse time {args.time!r}. Use e.g. '1:10am', "
                "'01:10', '13:45', '1am', 'now' or omit it for auto-detect."
            )
        target = compute_start(now, *parsed_clock)
        print(f"▶ --wait-only: waiting until {target:%Y-%m-%d %H:%M} (+1 min margin)")
        if not args.dry_run:
            wait_until(target)
    if args.dry_run:
        print("  (dry-run: no wait performed, no signal written)")
        return 0
    if args.signal_file:
        path = os.path.abspath(os.path.expanduser(args.signal_file))
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"{datetime.now().isoformat()}\n")
            print(f"📶 reset signal written: {path}")
        except OSError as exc:
            print(f"⚠️  could not write signal file {path}: {exc}", file=sys.stderr)
            return 1
    print(f"✅ {datetime.now():%Y-%m-%d %H:%M:%S} session limit clear")
    return 0


def _main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-branches
    """Wait for the session-limit reset, then exec 'claude --resume' here."""
    args = parse_args(argv)

    if shutil.which("claude") is None:
        print("ERROR: 'claude' binary not found in PATH", file=sys.stderr)
        return 1

    if args.wait_only:
        return run_wait_only(args)
    if not args.session_id:
        sys.exit("ERROR: session_id is required (or use --wait-only to just wait).")

    # Pin 'last' to a concrete id NOW: the auto-mode probe itself creates a newer
    # session, which would otherwise hijack 'claude --continue'.
    session_id = args.session_id
    if session_id.lower() in LAST_SESSION_KEYWORDS:
        resolved = resolve_last_session()
        if resolved is not None:
            session_id = resolved
            print(f"ℹ️  '{args.session_id}' = most recent session: {session_id}")

    now = datetime.now()
    raw_time = (args.time or "auto").strip().lower()
    if raw_time in AUTO_KEYWORDS:
        mode = "auto"
        target = None
        estimate = ccusage_reset_target()
        when = "auto-detect via 'claude -p' probe"
        if estimate is not None:
            when += f" (ccusage estimate: {estimate:%Y-%m-%d %H:%M})"
    elif raw_time in NOW_KEYWORDS:
        mode = "now"
        target = now
        when = f"{target:%Y-%m-%d %H:%M:%S} (now)"
    else:
        mode = "explicit"
        parsed_clock = parse_clock_time(raw_time)
        if parsed_clock is None:
            sys.exit(
                f"ERROR: cannot parse time {args.time!r}. Use e.g. '1:10am', "
                "'01:10', '13:45', '1am', 'now' or omit it for auto-detect."
            )
        target = compute_start(now, *parsed_clock)
        when = f"{target:%Y-%m-%d %H:%M:%S} (reset time + 1 min safety margin)"

    cmd = build_command(args, session_id)
    print(f"▶ start time : {when}")
    print(f"  session    : {session_id}")
    print(f"  directory  : {os.getcwd()}")
    print(f"  command    : {' '.join(shlex.quote(part) for part in cmd)}")

    if session_id.lower() not in LAST_SESSION_KEYWORDS:
        transcript = session_file_path(session_id)
        if not os.path.exists(transcript):
            print(
                f"⚠️  no transcript at {transcript} — 'claude --resume' may not find "
                "this session here. Run this from the folder where the original "
                "session was started.",
                file=sys.stderr,
            )

    if args.dry_run:
        return 0

    if mode == "explicit" and target is not None and target > now:
        wait_until(target)
    elif mode == "auto":
        auto_wait_loop()

    # flush=True: execvp replaces the process without flushing Python's stdio buffers,
    # which would lose this and earlier output when stdout is a file.
    print(f"🚀 {datetime.now():%Y-%m-%d %H:%M:%S} resuming session ...", flush=True)
    os.execvp(cmd[0], cmd)
    return 1  # execvp does not return


def main(argv: list[str] | None = None) -> int:
    """Console entry point: run :func:`_main`, mapping Ctrl-C to exit code 130."""
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
