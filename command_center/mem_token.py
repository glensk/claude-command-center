"""The shared-memory attestation token every ccc-started Claude / Codex child carries.

The shared LLM memory service (``mem``, mydotfiles ``bin/``) trusts a write only when the
writer's environment holds a token a trusted launcher minted: ``MEM_TOKEN=<token>`` plus
``MEM_LAUNCHER=<launcher>``. ccc stamps it on the Claude sessions it starts
(``MEM_LAUNCHER=ccc``) and ``codex-in-claude`` on every ``codex exec`` it runs
(``MEM_LAUNCHER=codex-in-claude``). :func:`mem_env` is the ONE place both mint it.

Rules (tp#1249 L1):

* an ambient ``MEM_TOKEN`` is kept exactly as it is — nothing is minted, nothing added
  (a session launched from an already-attested shell inherits that attestation);
* the mint is ``mem token -l <launcher> -t`` with ``MEM_CLIENT_TIMEOUT=3`` in its env and a
  hard :data:`MINT_TIMEOUT_S` wall (the process group is killed past it);
* ANY failure — ``mem`` not found, a non-zero exit (3 refused, 7 service down), a timeout,
  empty or malformed output — answers ``{}``: the launch goes ahead without the variables,
  and the memory service then files that tool's writes as proposals (safe degradation);
* the token is never printed, logged or persisted — it only ever travels in a child env.

The ``mem`` CLI resolves through :data:`command_center.external_deps.EXTERNAL_DEPS`
(``MEM_BIN`` override → ``$PATH``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

TOKEN_VAR = "MEM_TOKEN"
LAUNCHER_VAR = "MEM_LAUNCHER"

# The launcher names ccc mints for.
LAUNCHER_CCC = "ccc"
LAUNCHER_CODEX = "codex-in-claude"

# Wall clock for one mint. The client's own budget (MEM_CLIENT_TIMEOUT) is 3 s; the
# extra 2 s cover the interpreter start of the `mem` script.
MINT_TIMEOUT_S = 5.0
CLIENT_TIMEOUT_S = "3"

# A token is one printable, whitespace-free run: anything else (a banner, an error text
# printed with exit 0) is not trusted into a child env.
_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]{8,4096}")


def staff_launcher(base: Mapping[str, str]) -> str | None:
    """``staff-<agent>`` when *base* is a staff session (``STAFF_AGENT`` set), else ``None``.

    Staff agents read untrusted mail and Slack, so their sessions get an UNTRUSTED token
    (tp#1249): every memory they submit lands proposed and goes through the checker.
    """
    agent = re.sub(r"[^a-z0-9-]", "-", (base.get("STAFF_AGENT") or "").strip().lower()).strip("-")
    return f"staff-{agent}"[:41] if agent else None


def mint(launcher: str, trusted: bool = True) -> str | None:
    """A fresh token for *launcher* from ``mem token -l <launcher> -t|-u``, or ``None``.

    Never raises and never logs the token: every failure is a silent ``None``.
    """
    # Lazy: only a launch pays for the resolver and the subprocess helper.
    # pylint: disable=import-outside-toplevel
    from extdeps import resolve

    from .checks import run_structured
    from .external_deps import EXTERNAL_DEPS

    try:
        exe = resolve(EXTERNAL_DEPS["mem"])
    except Exception:  # pylint: disable=broad-exception-caught  # fail open: no token
        return None
    if not exe:
        return None
    env = dict(os.environ)
    env["MEM_CLIENT_TIMEOUT"] = CLIENT_TIMEOUT_S
    result = run_structured(
        [exe, "token", "-l", launcher, "-t" if trusted else "-u"],
        timeout=MINT_TIMEOUT_S,
        max_bytes=8192,
        env=env,
    )
    if result.timed_out or result.truncated or result.exit != 0:
        return None
    token = result.stdout.strip()
    return token if _TOKEN_SHAPE.fullmatch(token) else None


def mem_env(launcher: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env entries to ADD to a child launched by *launcher* (``{}`` = add nothing).

    *base* is the env the child would otherwise get (default ``os.environ``). When it
    already carries ``MEM_TOKEN`` the answer is ``{}`` — the inherited attestation is kept
    as is. Otherwise ``{MEM_TOKEN: <token>, MEM_LAUNCHER: <launcher>}`` after a successful
    :func:`mint`, else ``{}``.
    """
    current = os.environ if base is None else base
    if current.get(TOKEN_VAR):
        return {}
    staff = staff_launcher(current)
    if staff:
        launcher = staff
    token = mint(launcher, trusted=staff is None)
    if not token:
        return {}
    return {TOKEN_VAR: token, LAUNCHER_VAR: launcher}


# The shell snippet a TYPED launch line carries (tp#1290). A command line ccc types into a
# tab lands in that shell's history and on screen, so it can never hold the token itself.
# Instead the shell mints it at run time through `ccc mem-token` (the same :func:`mem_env`
# policy, run in its own process) and exports it from a command substitution: the value
# only ever travels through a pipe into the shell's variable, never through argv, typed text
# or a log. A failed mint (or an ambient MEM_TOKEN, which `ccc mem-token` leaves alone)
# exports nothing, so the launch goes ahead exactly as before. POSIX sh and zsh.
TYPED_SNIPPET = (
    '_ccc_mt=$(ccc mem-token 2>/dev/null) && export MEM_TOKEN="$_ccc_mt" '
    f"{LAUNCHER_VAR}={LAUNCHER_CCC}; unset _ccc_mt; "
)


def print_for_shell() -> int:
    """``ccc mem-token``: print a fresh ``ccc`` token for :data:`TYPED_SNIPPET`'s ``$(…)``.

    Exit 0 with the token on stdout; 1 (nothing printed) when an ambient ``MEM_TOKEN`` is
    kept or the mint failed (:func:`mem_env`); 2 when stdout is a terminal — the token is
    only ever handed to a command substitution, never shown on a screen.
    """
    import sys  # pylint: disable=import-outside-toplevel

    if sys.stdout.isatty():
        print(
            "ccc mem-token: prints a secret — only for a $(…) command substitution, "
            "never to a terminal",
            file=sys.stderr,
        )
        return 2
    token = mem_env(LAUNCHER_CCC).get(TOKEN_VAR, "")
    if not token:
        return 1
    sys.stdout.write(token + "\n")
    return 0
