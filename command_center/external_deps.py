#!/usr/bin/env python3
"""External-dependency registry for ccc (the ``extdeps`` convention).

Declares every other-repo script or system executable an **opt-in** ccc feature shells
out to, so a missing one fails loud and useful — a withheld write, an exit 3, a
``ccc doctor`` ❌ — instead of a cryptic ``FileNotFoundError`` deep inside a subprocess
call. Resolution chain per entry: explicit env override → ``$PATH``. There is
deliberately **no conventional sibling path** here: ccc's public tree carries no private
checkout layout (``tools/check_public_tree.py``), so a non-standard location is always
declared through the env override (the daemon's launchd plist environment included).

Capability-scoped, never global: nothing here runs at import time and nothing runs
before argument parsing. Call sites use :func:`extdeps.require` (which also probes
``<dep> -h`` for ``requires_subcommand`` so an *outdated* copy fails clearly) only for
the feature the user actually turned on — see :mod:`command_center.scrub`.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)


# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first
import os

from extdeps import Dep, MissingExternalDependency, require, resolve

# The one external tool the vault mirrors depend on: the secret-broker client. Its
# `scrub` verb vouches for a document (stdin → scrubbed stdout, exit 0) or withholds it
# (exit 3, nothing emitted); its `check` verb is the FUTURE-draft tripwire (exit 0 clean,
# 1 leak, 3 unavailable — each with a v1 verdict marker as the first stdout line).
# `SECRET_BROKER_CLIENT` is the same override the secret-check-index commit gate honours.
EXTERNAL_DEPS: dict[str, Dep] = {
    "secret-broker-client.py": Dep(
        name="secret-broker-client.py",
        command="secret-broker-client.py",
        env="SECRET_BROKER_CLIENT",
        requires_subcommand="scrub",
        install_hint=(
            "Install the secret-broker client (the same `secret-broker-client.py` the "
            "secret-check-index pre-commit gate uses) and put it on $PATH, or set "
            "SECRET_BROKER_CLIENT to its absolute path in the daemon's environment. "
            "Needed only while a mirror switch (mirror_running / mirror_done / "
            "mirror_sessions) is on; `mirror_allow_unscrubbed = true` is the explicit "
            "opt-out that writes mirrors without a scrubber."
        ),
    ),
    # Google's Antigravity CLI. ccc calls it for ONE thing: `agy -p /usage
    # --output-format json`, the account's own quota meter (zero tokens — the CLI answers
    # from the quota service, not from a model turn). Resolved with `resolve`, not
    # `require`: the fetcher behind `agy_usage` is best-effort, and a machine with no
    # Antigravity install must simply report the provider as `unknown`.
    "agy": Dep(
        name="agy",
        command="agy",
        env="AGY_BIN",
        install_hint=(
            "Install Google's Antigravity CLI (`agy`) and put it on $PATH, or set AGY_BIN "
            "to its absolute path. Needed only while `agy_usage = true`; with it off, ccc "
            "never looks for the binary."
        ),
    ),
    # The LLM ladder registry's CLI (`ai.py`). ccc asks it ONE question: `ai
    # ladders -j`, i.e. which model the hourly OpenCode free-tier probe should ask
    # (:mod:`command_center.quota_probe`). Resolved with `resolve`: without it the probe
    # asks `opencode_free_model` from config.toml instead — the value it always used.
    "ai.py": Dep(
        name="ai.py",
        command="ai.py",
        env="AI_BIN",
        install_hint=(
            "Put `ai.py` on $PATH, or set AI_BIN to its absolute path (the "
            "quota-probe launchd agent gets it written into its environment when ccc "
            "finds it at install time). Without it `ccc quota -P` probes "
            "`opencode_free_model` from config.toml."
        ),
    ),
    # The two `ccc await` probes. Both are resolved ONCE, at arm time, and the absolute
    # path is persisted in the source's spec — the poller never re-resolves, so a PATH
    # that differs under launchd cannot silently change which script answers. `require`,
    # not `resolve`: a user who typed `ccc await -z` asked for this dependency.
    "zoho-api.py": Dep(
        name="zoho-api.py",
        command="zoho-api.py",
        env="ZOHO_API_BIN",
        requires_subcommand="--inbound-since",
        install_hint=(
            "Put the Zoho Desk CLI `zoho-api.py` (with its read-only -i/--inbound-since "
            "probe) on $PATH, or set ZOHO_API_BIN to its path. Needed only by "
            "`ccc await -z`."
        ),
    ),
    "slack_api.py": Dep(
        name="slack_api.py",
        command="slack_api.py",
        env="SLACK_API_BIN",
        requires_subcommand="--dm",
        install_hint=(
            "Put the Slack CLI `slack_api.py` (--dm USER --oldest TS --json) on $PATH, or "
            "set SLACK_API_BIN to its path. Needed only by `ccc await -S`."
        ),
    ),
}


def canonical_exe(path: str) -> str:
    """*path* as the absolute, normalized spelling persisted in an await spec.

    A relative ``ZOHO_API_BIN=./zoho-api.py`` would resolve against whatever cwd the
    poller runs in; the arm-time answer is made absolute before it is stored. Symlinks
    are kept (the user's spelling), only ``~``, ``.`` and ``..`` are resolved.
    """
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def await_exe(name: str, *, needed_for: str) -> str:
    """The canonical path of await dependency *name*; raises MissingExternalDependency.

    Also refuses a found-but-not-executable file (the probe would fail at spawn time,
    every pass, as a "permanent" error — better to say so at arm time).
    """
    found = canonical_exe(require(EXTERNAL_DEPS[name], needed_for=needed_for))
    if not os.access(found, os.X_OK):
        raise MissingExternalDependency(
            EXTERNAL_DEPS[name], needed_for, detail=f"found at {found}, but it is not executable."
        )
    return found


def ai_exe() -> str | None:
    """``ai.py`` (``AI_BIN`` → ``$PATH``), or ``None`` — a missing ai.py is a degrade."""
    return resolve(EXTERNAL_DEPS["ai.py"])
