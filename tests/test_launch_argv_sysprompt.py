"""The startup-system-prompt opt-in of :func:`launch_argv.claude_argv` (tp#2168 B2).

An ``execvp`` site that bypasses the user's shell wrapper (``claude-session-continue``)
asks the external ``cc-startup-sysprompt.py -a -c <config dir>`` for its NUL-separated
flags. Pinned: switch off → argv unchanged and the helper never runs; switch on → the
flags right before *extra* (and before ``--`` + prompt); a caller's own settings / agents
/ system prompt → no helper flag at all; helper missing / failing / slow / empty /
malformed → the plain argv; typed commands (no opt-in) never get the flags.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import launch_argv, session_continue

FLAGS = ["--append-system-prompt-file", "/s/sysprompt.md", "--settings", "/s/settings.json"]
AGENTS = ["--agents", '{"general-purpose":{"description":"d \\"q\\" $HOME `x`","prompt":"p"}}']

HELPER = r"""#!/bin/bash
printf '%s\n' "$*" >>"{log}"
case "{mode}" in
    ok) printf '%s\0' --append-system-prompt-file /s/sysprompt.md --settings /s/settings.json ;;
    agents) printf '%s\0' --append-system-prompt-file /s/sysprompt.md --settings /s/settings.json \
        --agents '{{"general-purpose":{{"description":"d \"q\" $HOME `x`","prompt":"p"}}}}' ;;
    fail) printf '%s\0' --append-system-prompt-file /s/sysprompt.md; exit 1 ;;
    empty) exit 0 ;;
    malformed) printf '%s\0' --append-system-prompt-file /s/sysprompt.md --settings ;;
    reordered) printf '%s\0' --settings /s/settings.json --append-system-prompt-file /s/x ;;
    nonul) printf '%s' --append-system-prompt-file ;;
    slow) sleep 5 ;;
esac
"""


class Helper:
    """A stub ``cc-startup-sysprompt.py`` reached through ``CC_STARTUP_SYSPROMPT_BIN``."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.path = tmp_path / "cc-startup-sysprompt.py"
        self.log = tmp_path / "helper.log"
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("CC_STARTUP_SYSPROMPT_BIN", str(self.path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv(launch_argv.STARTUP_SYSPROMPT_ENV, raising=False)
        monkeypatch.setattr(launch_argv, "lookup_name", lambda _sid: "")
        self.mode("ok")

    def mode(self, mode: str) -> None:
        """(Re)write the stub in *mode*."""
        self.path.write_text(HELPER.format(log=self.log, mode=mode), encoding="utf-8")
        self.path.chmod(0o755)

    def switch(self, value: str = "1") -> None:
        """Set the opt-in switch."""
        self.monkeypatch.setenv(launch_argv.STARTUP_SYSPROMPT_ENV, value)

    def calls(self) -> list[str]:
        """The recorded helper command lines."""
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture(name="helper")
def helper_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Helper:
    """A stub helper in ``ok`` mode, the switch unset."""
    return Helper(tmp_path, monkeypatch)


def _plain(**kwargs: object) -> list[str]:
    return launch_argv.claude_argv(name="", resume="sid", **kwargs)  # type: ignore[arg-type]


EXTRA = ("--dangerously-skip-permissions",)


@pytest.mark.parametrize("value", [None, "0", "", "yes"])
def test_switch_off_leaves_the_argv_alone(helper: Helper, value: str | None) -> None:
    if value is not None:
        helper.switch(value)
    argv = _plain(extra=EXTRA, prompt="go", startup_sysprompt=True)
    assert argv == ["claude", "--resume", "sid", *EXTRA, "--", "go"]
    assert helper.calls() == []


def test_switch_on_inserts_the_flags_before_extra_and_prompt(
    helper: Helper, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper.switch()
    monkeypatch.setenv("HOME", str(tmp_path))
    argv = _plain(extra=EXTRA, prompt="go", startup_sysprompt=True)
    assert argv == ["claude", "--resume", "sid", *FLAGS, *EXTRA, "--", "go"]
    assert helper.calls() == [f"-a -c {tmp_path}/.claude"]


def test_seat_follows_claude_config_dir_or_the_explicit_one(
    helper: Helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper.switch()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/seat/work")
    _plain(startup_sysprompt=True)
    _plain(startup_sysprompt=True, config_dir="/seat/other")
    assert helper.calls() == ["-a -c /seat/work", "-a -c /seat/other"]


def test_agents_json_arrives_as_one_verbatim_element(helper: Helper) -> None:
    helper.switch()
    helper.mode("agents")
    assert _plain(startup_sysprompt=True) == ["claude", "--resume", "sid", *FLAGS, *AGENTS]


def test_typed_commands_without_the_opt_in_never_get_the_flags(helper: Helper) -> None:
    helper.switch()
    assert _plain(extra=EXTRA) == ["claude", "--resume", "sid", *EXTRA]
    assert helper.calls() == []


@pytest.mark.parametrize(
    "own",
    [
        ("--settings", "{}"),
        ("--settings={}",),
        ("--append-system-prompt", "x"),
        ("--append-system-prompt-file", "/x"),
        ("--system-prompt", "x"),
        ("--system-prompt-file", "/x"),
        ("--agents", "{}"),
        ("--agents={}",),
    ],
)
def test_own_flags_skip_every_helper_flag(helper: Helper, own: tuple[str, ...]) -> None:
    helper.switch()
    assert _plain(extra=own, startup_sysprompt=True) == ["claude", "--resume", "sid", *own]
    assert helper.calls() == []


def test_own_flag_detection_stops_at_the_end_of_options_marker() -> None:
    assert not launch_argv.has_own_sysprompt_flags(["--", "--settings"])
    assert launch_argv.has_own_sysprompt_flags(["-x", "--system-prompt-file=/f"])
    assert not launch_argv.has_own_sysprompt_flags(["--settingsx", "--agent"])


@pytest.mark.parametrize("mode", ["fail", "empty", "malformed", "reordered", "nonul"])
def test_helper_failure_launches_plain(helper: Helper, mode: str) -> None:
    helper.switch()
    helper.mode(mode)
    assert _plain(extra=EXTRA, startup_sysprompt=True) == ["claude", "--resume", "sid", *EXTRA]
    assert len(helper.calls()) == 1


def test_slow_helper_is_cut_off(helper: Helper, monkeypatch: pytest.MonkeyPatch) -> None:
    helper.switch()
    helper.mode("slow")
    monkeypatch.setattr(launch_argv, "STARTUP_SYSPROMPT_TIMEOUT_S", 0.5)
    assert _plain(startup_sysprompt=True) == ["claude", "--resume", "sid"]


def test_missing_helper_launches_plain(
    helper: Helper, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    helper.switch()
    monkeypatch.setenv("CC_STARTUP_SYSPROMPT_BIN", str(tmp_path / "missing"))
    assert _plain(startup_sysprompt=True) == ["claude", "--resume", "sid"]


def test_session_continue_opts_in(helper: Helper) -> None:
    args = session_continue.parse_args(["sid"])
    assert session_continue.build_command(args, "sid") == [
        "claude",
        "--resume",
        "sid",
        "--dangerously-skip-permissions",
        "--",
        session_continue.DEFAULT_PROMPT,
    ]
    helper.switch()
    assert session_continue.build_command(args, "sid") == [
        "claude",
        "--resume",
        "sid",
        *FLAGS,
        "--dangerously-skip-permissions",
        "--",
        session_continue.DEFAULT_PROMPT,
    ]
