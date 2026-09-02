"""A fake credential scrubber for the test suite — never a real broker.

The mirror gate (:mod:`command_center.scrub`) talks to ``mirror_scrub_cmd`` over
stdin/stdout/exit code only, so an executable ``/bin/sh`` script is a complete stand-in.
One script per *mode*: ``redact`` (the default — a passthrough unless the document holds
:data:`DUMMY_VALUE`, which it replaces by ``<<REDACTED:test.key>>`` and reports as
``SCRUBBED: test.key``) plus one failure mode per contract branch. Every invocation
appends its verb to a calls log so a test can assert "not called".

The dummy value is not a credential shape of any provider (it must never trip the
public-tree gate or a real scanner); it only has to be a literal the stub can grep for.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DUMMY_VALUE = "DUMMY-LIVE-VALUE-0123456789"
PLACEHOLDER = "<<REDACTED:test.key>>"

_HEAD = """#!/bin/sh
# fake scrubber for the ccc test suite — speaks the mirror_scrub_cmd contract only
case "$1" in
  -h|--help)
    echo "usage: fake-broker {help}"
    exit 0
    ;;
esac
printf '%s\\n' "$1" >> "{calls}"
"""

_BODIES = {
    "redact": """tmp=$(mktemp)
cat > "$tmp"
if grep -q "{value}" "$tmp"; then
  echo "SCRUBBED: test.key" >&2
fi
sed "s/{value}/{placeholder}/g" "$tmp"
rm -f "$tmp"
exit 0
""",
    "exit3": """cat > /dev/null
echo "secret-broker: unavailable — output withheld (exit 3)" >&2
exit 3
""",
    "empty": """cat > /dev/null
exit 0
""",
    "oversize": """cat
head -c 70000 /dev/zero | tr '\\0' x
exit 0
""",
    "badutf8": """cat > /dev/null
printf '\\377\\376'
exit 0
""",
    "identity": """sed 's/^session_id: .*/session_id: "not-this-session"/'
exit 0
""",
    "sleep": """exec sleep 5
""",
    "check": """document=$(cat)
case "$document" in
  *{value}*)
    echo "LEAK-VERDICT v1: 1 hit(s)"
    echo "LIVE CREDENTIAL IN OUTPUT: test.key" >&2
    exit 1
    ;;
esac
echo "CLEAN-VERDICT v1"
exit 0
""",
    "checkcrash": """cat > /dev/null
echo "Traceback: boom" >&2
exit 1
""",
}
# `nohelp` behaves like `redact` but its -h output names no `scrub` verb — an
# "outdated client" for the resolver's capability probe.
_BODIES["nohelp"] = _BODIES["redact"]


@dataclass
class Stub:
    """The written stub: its path and the log of verbs it was invoked with."""

    path: Path
    calls_file: Path
    mode: str

    @property
    def calls(self) -> list[str]:
        """Every verb the stub was invoked with so far (oldest first)."""
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text(encoding="utf-8").split()

    @property
    def scrub_cmd(self) -> str:
        """A ``mirror_scrub_cmd`` value pointing at this stub (verbatim absolute path)."""
        return f"{self.path} scrub"


def stub_scrubber(tmp_path: Path, mode: str = "redact") -> Stub:
    """Write an executable stub of *mode* under *tmp_path* and return its handle."""
    if mode not in _BODIES:
        raise ValueError(f"unknown stub mode {mode!r}")
    calls = tmp_path / f"scrubber-{mode}-calls.log"
    script = tmp_path / f"fake-broker-{mode}.sh"
    help_text = "< document" if mode == "nohelp" else "{ scrub | check } < document"
    body = _BODIES[mode].format(value=DUMMY_VALUE, placeholder=PLACEHOLDER)
    script.write_text(_HEAD.format(help=help_text, calls=calls) + body, encoding="utf-8")
    script.chmod(0o755)
    return Stub(path=script, calls_file=calls, mode=mode)
