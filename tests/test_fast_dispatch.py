"""The per-subcommand parser fast path (tp#115) — `build_parser(only=...)`.

The status line, the hooks and the shell badge hook spawn ccc hundreds of times a
minute, so `main` builds ONLY the subparser of the hot subcommand it is about to run.
The contract these tests pin: the short parser is INDISTINGUISHABLE from the full one
for every argv form of a hot subcommand (values, `func`, help text, argparse errors),
the generated wiring only ever spawns hot subcommands, and the hot path never imports
`llmrouting`.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from command_center import cli, doctor, hookspec, install, shell_install

_HOT_ARGVS: list[list[str]] = [
    ["statusline", "--print-glyph"],
    ["statusline", "--session", "abc-123", "--capture-usage"],
    ["aim", "--session", "abc-123", "--format", "bar"],
    *[["hook", event] for event in hookspec.ALL_HOOK_ARGS],
    ["tab-symbol", "--print", "/tmp/x"],
    ["tab-symbol"],
    ["tab-symbol", "--print", "--color", "/tmp/x"],
]


def _subparser(parser, name: str):
    """The subparser registered for *name* (argparse keeps no public accessor)."""
    return parser._subparsers._group_actions[0].choices[name]


def _error_stderr(parser, argv: list[str]) -> str:
    """The stderr *parser* prints while rejecting *argv* (which must be rejected)."""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer), pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    assert exc.value.code == 2
    return buffer.getvalue()


# ------------------------------ parity ------------------------------ #
@pytest.mark.parametrize("argv", _HOT_ARGVS, ids=lambda a: " ".join(a))
def test_short_parser_parses_exactly_like_the_full_one(argv: list[str]) -> None:
    short = cli.build_parser(only=argv[0]).parse_args(argv)
    full = cli.build_parser().parse_args(argv)
    assert vars(short) == vars(full)  # same keys, same values, same `func`


def test_hook_subparser_help_and_errors_are_identical_in_both_modes() -> None:
    """A duplicate `choices=` source or a diverging prog would show up here."""
    short, full = cli.build_parser(only="hook"), cli.build_parser()
    assert _subparser(short, "hook").format_help() == _subparser(full, "hook").format_help()
    assert _error_stderr(short, ["hook", "nope"]) == _error_stderr(full, ["hook", "nope"])


def test_short_mode_builds_one_subparser_and_falls_back_when_unknown() -> None:
    only = cli.build_parser(only="statusline")
    group = only._subparsers
    assert group is not None
    choices = group._group_actions[0].choices
    assert choices is not None
    assert list(choices) == ["statusline"]
    # Defensive: an unrecognised `only` is the full parser, never an error.
    assert cli.build_parser(only="nope").parse_args(["ls"]).func is cli.cmd_ls


@pytest.mark.parametrize("argv", [["hook", "not-an-event"], ["statusline", "--bogus"]])
def test_main_rejects_bad_hot_argv_with_the_argparse_exit_code(argv: list[str]) -> None:
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


# ------------------------------ argv normalisation ------------------------------ #
def _spy_build_parser(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Record every `only` value `main` asks `build_parser` for."""
    recorded: list[str | None] = []
    real = cli.build_parser

    def spy(only: str | None = None):
        recorded.append(only)
        return real(only=only)

    monkeypatch.setattr(cli, "build_parser", spy)
    return recorded


def test_main_picks_the_mode_from_sys_argv_when_called_with_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main()` (no argv) must normalise sys.argv FIRST, then choose the parser mode."""
    recorded = _spy_build_parser(monkeypatch)
    # `func` is bound inside `_add_statusline` when the parser is built — which happens
    # inside main() — so patching the handler here is what the parser picks up.
    monkeypatch.setattr(cli, "cmd_statusline", lambda args: 0)
    monkeypatch.setattr(sys, "argv", ["ccc", "statusline", "--print-glyph"])
    assert cli.main() == 0
    assert recorded == ["statusline"]

    recorded.clear()
    monkeypatch.setattr(cli, "cmd_ls", lambda args: 0)
    monkeypatch.setattr(sys, "argv", ["ccc", "ls"])
    assert cli.main() == 0
    assert recorded == [None]


# ------------------------------ the generated forms ------------------------------ #
def _ccc_subcommands(text: str) -> list[str]:
    """Every `<ccc-ref> <subcommand>` in *text* (the shell forms ccc generates)."""
    names: list[str] = []
    for line in text.splitlines():
        tokens = shlex.split(line, comments=True) if not line.lstrip().startswith("#") else []
        for index, token in enumerate(tokens[:-1]):
            if token in ("ccc", "$CCC_BIN", "${CCC_BIN}") or token.endswith("/ccc"):
                names.append(tokens[index + 1])
    return names


def test_the_shell_badge_hook_spawns_a_hot_subcommand() -> None:
    # The badge hook spawns ccc inside a `"$(...)"` substitution, so read the generated
    # form itself: it is the one this fast path exists for (once per `cd`, per shell).
    badge = shell_install.badges_hook("zsh")
    assert "ccc tab-symbol --print" in badge
    spawned = re.findall(r"\bccc ([a-z][a-z-]*)", badge)
    assert spawned == ["tab-symbol"]
    assert "tab-symbol" in cli._HOT_SUBCOMMANDS


def test_every_generated_hook_command_is_a_hot_subcommand() -> None:
    settings = install.build_hooks_settings({}, "/opt/ccc", uninstall=False)
    commands = [
        entry["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for entry in group["hooks"]
    ]
    assert len(commands) == len(hookspec.ALL_HOOK_ARGS)
    for command in commands:
        spawned = _ccc_subcommands(command)
        assert spawned == ["hook"], command
        assert spawned[0] in cli._HOT_SUBCOMMANDS


def test_the_installed_statusline_only_spawns_hot_subcommands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(install, "ccc_binary", lambda: "/opt/ccc")
    assert install.install_statusline() == 0
    direct = install.load_settings()["statusLine"]["command"]
    assert _ccc_subcommands(direct) == ["statusline"]
    # The chain script (foreign statusLine present) spawns the same one command.
    chain = install._render_chain_script("/opt/ccc", {"command": "bash /my/sl.sh"})
    assert _ccc_subcommands(chain) == ["statusline"]
    for name in _ccc_subcommands(direct) + _ccc_subcommands(chain):
        assert name in cli._HOT_SUBCOMMANDS


# ------------------------------ import hygiene ------------------------------ #
def test_the_hot_path_never_imports_llmrouting() -> None:
    """`llmrouting` pulls subprocess; it may not land on a 400-spawns-a-minute path."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCC")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "importtime",
            "-c",
            "import command_center.cli as c; c.build_parser(only='statusline')",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=120,
    )
    assert " command_center.llmrouting" not in proc.stderr
    assert " command_center.cli" in proc.stderr  # the probe really imported the module


def test_the_full_parser_still_embeds_the_routing_block_for_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from command_center import llmrouting

    monkeypatch.setattr(llmrouting, "render", lambda *a, **k: "ROUTING-BLOCK")
    monkeypatch.setattr(sys, "argv", ["ccc", "-h"])
    assert "ROUTING-BLOCK" in (cli.build_parser().epilog or "")
    monkeypatch.setattr(sys, "argv", ["ccc", "ls"])
    assert "ROUTING-BLOCK" not in (cli.build_parser().epilog or "")


# ------------------------------ the hook-event sources ------------------------------ #
def test_hook_events_is_the_unique_choices_source() -> None:
    """`ALL_HOOK_ARGS` repeats post-tool-use (two matchers) — `choices=` needs HOOK_EVENTS."""
    assert len(hookspec.HOOK_EVENTS) == len(set(hookspec.HOOK_EVENTS))
    assert len(hookspec.ALL_HOOK_ARGS) > len(hookspec.HOOK_EVENTS)
    assert set(hookspec.HOOK_EVENTS) == set(hookspec.ALL_HOOK_ARGS)
    assert frozenset(hookspec.ALL_HOOK_ARGS) == install._HOOK_EVENTS
    assert set(hookspec.HOOK_EVENTS) == install._HOOK_EVENTS
    # install re-exports both: doctor and the tests read them there.
    assert install.HOOK_SPEC is hookspec.HOOK_SPEC
    assert install.ALL_HOOK_ARGS is hookspec.ALL_HOOK_ARGS


def test_doctor_reports_the_hot_path_of_the_spawned_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor section is keyed off the same dict the dispatch uses."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(install, "ccc_binary", lambda: "/opt/ccc")
    install.install_hooks()
    install.install_statusline()
    statuses = {c.label: c.status for c in doctor._section_fast_path().checks}
    assert statuses == {"ccc statusline": doctor.OK, "ccc hook": doctor.OK}
