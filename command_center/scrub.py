"""Fail-closed credential scrubbing through an external scrubber (the mirror invariant).

The three export-only mirror roots (``running/``, ``done/``, ``sessions/``) embed prompts,
replies and tool output **verbatim** — including any credential value a session ever
printed. This module is the ONE gate those bytes pass before :mod:`command_center.mirrors`
writes them into the vault, and the tripwire :mod:`command_center.futuresync` runs on a
FUTURE draft's prompt before launching it. Contract (settled in the tp#117 debate):

* **Scrubber = a subprocess**, configured by ``mirror_scrub_cmd`` (default
  ``secret-broker-client.py scrub --shapes``): first token = the executable, resolved
  through the external-deps registry (``$SECRET_BROKER_CLIENT`` → ``$PATH``; a token
  containing ``/`` is used verbatim and must exist + be executable), the rest = its
  arguments. ``shlex.split``, ``shell=False``, the document on **stdin**, the vouched
  document on **stdout**, ``SCRUBBED: <label>, …`` lines on stderr (labels only, never
  values). Exit 0 = vouched. Anything else — non-zero exit, timeout, empty output, output
  that grew past :data:`MAX_GROWTH_BYTES`, invalid UTF-8 — means the write is **withheld**.
* **Fail closed.** No scrubber configured (``mirror_scrub_cmd = ""``), unresolvable,
  outdated (lacks the verb), or degraded → withheld. The only passthrough is the explicit
  ``mirror_allow_unscrubbed = true`` knob, which the callers honour (not this module).
* **Reasons never carry content.** Every ``reason`` string is built from exit codes,
  sizes and the scrubber's own label lines — a withheld card's bytes stay in memory.
* ``check`` (the tripwire) speaks the client's v1 verdict contract: exit 0 with a
  leading ``CLEAN-VERDICT v1`` line = clean, exit 1 with a leading ``LEAK-VERDICT v1``
  line = leak (labels on stderr), anything else = degraded. A missing marker is a
  crashed checker, rendered as *degraded* — never as clean and never as a leak.

Nothing here runs at import; resolution happens per call (``extdeps.require`` runs one
``<exe> -h`` probe so an outdated client fails with a clear message, not mid-run).
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Per-document scrub timeout (seconds). The measured worst case over ~1300 real cards was
# 17 s (a 439 KB card); 60 s leaves headroom without letting one stuck broker call eat a
# whole daemon pass.
SCRUB_TIMEOUT_S = 60.0
# The tripwire checks a prompt + AIM (small); the broker's own request timeout is 90 s.
CHECK_TIMEOUT_S = 60.0
# A scrubber only ever REPLACES spans with short placeholders; output larger than the input
# by more than this is not a scrub of this document (a chatty wrapper, a mixed-up stream).
MAX_GROWTH_BYTES = 64 * 1024

SCRUBBED_PREFIX = "SCRUBBED: "
LEAK_LABELS_PREFIX = "LIVE CREDENTIAL IN OUTPUT: "
CLEAN_MARKER = "CLEAN-VERDICT v1"
LEAK_MARKER = "LEAK-VERDICT v1"

# The registry entry the default command resolves through (see external_deps.py).
_REGISTRY_KEY = "secret-broker-client.py"

# Verdict states of :func:`check`.
CLEAN = "clean"
LEAK = "leak"
DEGRADED = "degraded"

# Longest stderr excerpt a reason may quote (the scrubber's own one-line messages).
_REASON_EXCERPT = 200


@dataclass(frozen=True)
class Scrubber:
    """A resolved scrubber: absolute executable + arguments, and the policy it came from.

    ``policy`` is the exact ``mirror_scrub_cmd`` string (the vouch table keys on it, so a
    changed command invalidates every vouch — a new rule set re-scrubs everything).
    """

    argv: tuple[str, ...]
    policy: str

    @property
    def executable(self) -> str:
        """The resolved absolute path of the scrubber program."""
        return self.argv[0]


@dataclass(frozen=True)
class Resolution:
    """Outcome of :func:`resolve_scrubber`: a scrubber, or the reason there is none."""

    scrubber: Scrubber | None
    reason: str = ""  # "" when resolved; else why (never content)

    @property
    def ok(self) -> bool:
        """Whether a scrubber resolved."""
        return self.scrubber is not None


@dataclass(frozen=True)
class ScrubResult:
    """Outcome of one :func:`scrub` call: the vouched text, or a withheld reason."""

    ok: bool
    text: str = ""  # the vouched document ("" when withheld)
    labels: tuple[str, ...] = ()  # SCRUBBED labels (names only), in reported order
    reason: str = ""  # why withheld ("" when ok)

    @property
    def withheld(self) -> bool:
        """Whether the write must be withheld (the inverse of ``ok``)."""
        return not self.ok


@dataclass(frozen=True)
class CheckVerdict:
    """Outcome of one :func:`check` call on the v1 verdict contract."""

    state: str  # CLEAN | LEAK | DEGRADED
    labels: tuple[str, ...] = ()  # leak labels (names only)
    reason: str = ""  # DEGRADED detail ("" otherwise)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def sha256(text: str) -> str:
    """Hex SHA-256 of *text* (UTF-8) — the vouch table's document identity."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _first_line(text: str) -> str:
    """The last non-empty line of a stderr blob, capped — a scrubber's own message."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1][:_REASON_EXCERPT]


def resolve_scrubber(  # pylint: disable=too-many-locals,too-many-return-statements
    policy: str, *, verb: str = "scrub"
) -> Resolution:
    """Resolve ``mirror_scrub_cmd`` (*policy*) to a runnable :class:`Scrubber`, or a reason.

    *verb* is the subcommand the caller needs (``scrub`` for the mirrors, ``check`` for the
    FUTURE-draft tripwire); it is probed via ``<exe> -h`` so an outdated client fails
    here, clearly, instead of deep inside a pass. For ``verb != "scrub"`` the configured
    arguments are replaced by the verb alone (``<exe> check``) — the configured tail
    belongs to the scrub verb.

    Never raises: every failure is a :class:`Resolution` with ``scrubber=None`` and a
    reason (no scrubber configured, unparseable command, missing, not executable, lacks
    the verb, ``extdeps`` not installed in this environment).
    """
    policy = (policy or "").strip()
    if not policy:
        return Resolution(None, "no scrubber configured (mirror_scrub_cmd is empty)")
    try:
        tokens = shlex.split(policy)
    except ValueError as exc:
        return Resolution(None, f"mirror_scrub_cmd is not parseable: {exc}")
    if not tokens:
        return Resolution(None, "no scrubber configured (mirror_scrub_cmd is empty)")
    try:
        from extdeps import (  # pylint: disable=import-outside-toplevel
            Dep,
            MissingExternalDependency,
            require,
        )

        from .external_deps import EXTERNAL_DEPS  # pylint: disable=import-outside-toplevel
    except ImportError as exc:  # a stale tool venv (dependency added after install)
        return Resolution(
            None,
            f"extdeps package not importable ({exc}) — reinstall ccc "
            "(uv tool install --editable . --reinstall)",
        )
    head, tail = tokens[0], tokens[1:]
    needed_for = f"mirror scrubbing (mirror_scrub_cmd, verb {verb!r})"
    if "/" in head:
        path = Path(os.path.expanduser(head))
        if not path.is_file():
            return Resolution(None, f"scrubber {head!r} does not exist")
        if not os.access(path, os.X_OK):
            return Resolution(None, f"scrubber {head!r} is not executable")
        dep = Dep(
            name=head,
            install_hint="fix the first token of mirror_scrub_cmd",
            siblings=(str(path),),
            requires_subcommand=verb,
        )
    else:
        base = EXTERNAL_DEPS[_REGISTRY_KEY]
        dep = dataclasses.replace(
            base,
            name=head,
            command=head,
            # A bare command other than the registered one still honours the override
            # variable (it is the daemon's only way to point at a non-PATH location).
            requires_subcommand=verb,
        )
    try:
        exe = require(dep, needed_for=needed_for)
    except MissingExternalDependency as exc:
        # The message's FIRST line names the dependency and what needs it; the install
        # hint below it belongs in `ccc doctor`, not in a per-pass reason.
        head_line = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), str(exc))
        return Resolution(None, head_line[:_REASON_EXCERPT])
    argv = (exe, *tail) if verb == "scrub" else (exe, verb)
    return Resolution(Scrubber(argv=argv, policy=policy))


# ---------------------------------------------------------------------------
# scrub (mirror documents)
# ---------------------------------------------------------------------------
def _run(
    argv: tuple[str, ...], data: bytes, timeout: float
) -> tuple[subprocess.CompletedProcess[bytes] | None, str]:
    """Run the scrubber once; ``(proc, "")`` or ``(None, reason)`` on a spawn/timeout failure."""
    try:
        proc = subprocess.run(  # noqa: S603
            list(argv),
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"scrubber timed out after {timeout:.0f} s"
    except OSError as exc:
        return None, f"scrubber could not be started: {exc.strerror or exc}"
    return proc, ""


def _labels(stderr: bytes, prefix: str) -> tuple[str, ...]:
    """Comma-separated labels from every stderr line starting with *prefix* (names only)."""
    out: list[str] = []
    for raw in stderr.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            out.extend(part.strip() for part in line[len(prefix) :].split(",") if part.strip())
    return tuple(out)


def scrub(scrubber: Scrubber, content: str, *, timeout: float = SCRUB_TIMEOUT_S) -> ScrubResult:
    """Pass *content* through *scrubber*; the vouched text, or a withheld reason.

    Withheld on: non-zero exit, timeout, spawn failure, empty stdout, stdout longer than
    the input by more than :data:`MAX_GROWTH_BYTES`, or stdout that is not strict UTF-8.
    The caller adds the document-identity check (frontmatter kind + session id) because
    only it knows what the document was supposed to be.
    """
    data = content.encode("utf-8")
    proc, reason = _run(scrubber.argv, data, timeout)
    if proc is None:
        return ScrubResult(False, reason=reason)
    if proc.returncode != 0:
        detail = _first_line(proc.stderr.decode("utf-8", errors="replace"))
        suffix = f": {detail}" if detail else ""
        return ScrubResult(False, reason=f"scrubber exit {proc.returncode}{suffix}")
    if not proc.stdout:
        return ScrubResult(False, reason="scrubber exit 0 but returned no output")
    if len(proc.stdout) > len(data) + MAX_GROWTH_BYTES:
        return ScrubResult(
            False,
            reason=(
                f"scrubber output grew by {len(proc.stdout) - len(data)} bytes "
                f"(limit {MAX_GROWTH_BYTES})"
            ),
        )
    try:
        text = proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return ScrubResult(False, reason="scrubber output is not valid UTF-8")
    return ScrubResult(True, text=text, labels=_labels(proc.stderr, SCRUBBED_PREFIX))


# ---------------------------------------------------------------------------
# check (FUTURE-draft tripwire)
# ---------------------------------------------------------------------------
def check(scrubber: Scrubber, text: str, *, timeout: float = CHECK_TIMEOUT_S) -> CheckVerdict:
    """Run the ``check`` verb on *text* and classify the v1 verdict.

    ``scrubber`` must have been resolved with ``verb="check"``. A verdict needs BOTH the
    exit code and the matching first-stdout-line marker; any other combination (a
    crash, a missing marker, exit 3, a spawn failure) is DEGRADED — never clean, never
    a leak.
    """
    proc, reason = _run(scrubber.argv, text.encode("utf-8"), timeout)
    if proc is None:
        return CheckVerdict(DEGRADED, reason=reason)
    first = proc.stdout.decode("utf-8", errors="replace").split("\n", 1)[0].strip()
    if proc.returncode == 0 and first == CLEAN_MARKER:
        return CheckVerdict(CLEAN)
    if proc.returncode == 1 and first.startswith(LEAK_MARKER):
        labels = _labels(proc.stderr, LEAK_LABELS_PREFIX)
        return CheckVerdict(LEAK, labels=labels)
    detail = _first_line(proc.stderr.decode("utf-8", errors="replace"))
    if proc.returncode in (0, 1):
        why = f"checker exit {proc.returncode} without a v1 verdict marker (crashed checker)"
    else:
        why = f"checker exit {proc.returncode}"
    if detail:
        why = f"{why}: {detail}"
    return CheckVerdict(DEGRADED, reason=why)
