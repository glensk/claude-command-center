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


from extdeps import Dep

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
}
