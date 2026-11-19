"""``ccc resume-job -A LABEL -P FILE -j`` — resume a parked session on a chosen account (tp#2165).

Everything is faked: the adapter (live registry + transcript lookup) and the terminal
(``_open_tab`` records the typed line, nothing opens). Two accounts live under tmp —
``private`` (the default, ``CLAUDE_CONFIG_DIR`` unset) and ``work`` — with a shared
``projects/`` tree unless a test says otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Any

import pytest

from command_center import accounts, automated, bridge_cli, cli, config, terminal
from command_center.models import LiveSession
from command_center.store import Store

SID = "5e1d0a2b-1111-4222-8333-944455556666"


class FakeAdapter:
    """The two adapter calls ``resume-job`` makes: ``discover`` and ``transcript_path``."""

    def __init__(self, live: list[LiveSession] | None = None) -> None:
        self.live = live or []

    def discover(self) -> list[LiveSession]:
        return self.live

    def transcript_path(
        self, cwd: str, session_id: str, config_dir: str | None = None
    ) -> Path | None:
        order = [config_dir] if config_dir else []
        order += [str(p) for p in config.claude_config_dirs().values()]
        slug = cwd.replace("/", "-")
        for home in order:
            path = Path(home) / "projects" / slug / f"{session_id}.jsonl"
            if path.is_file():
                return path
        return None


@pytest.fixture(name="env")
def env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Two accounts, a shared projects tree, a repo, a parked row and a recording terminal."""
    private = tmp_path / "private"
    work = tmp_path / "work"
    private.mkdir()
    work.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(private))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(automated.ENV, raising=False)
    dirs = {"private": private, "work": work}
    monkeypatch.setattr(config, "claude_config_dirs", lambda: dict(dirs))
    (work / ".claude.json").write_text("{}\n", encoding="utf-8")  # work has run once
    repo = tmp_path / "repo"
    repo.mkdir()
    (private / "projects").mkdir()
    (work / "projects").symlink_to(private / "projects")  # the shared transcript store
    transcript = private / "projects" / str(repo).replace("/", "-") / f"{SID}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "last-prompt"}) + "\n", encoding="utf-8")
    with Store() as store:
        store.ensure(SID, cwd=str(repo))
        store.update_fields(SID, config_dir=str(private), no_codex=True)
    adapter = FakeAdapter()
    monkeypatch.setattr(cli, "_adapter", lambda: adapter)
    typed: list[str] = []
    landed = {"rung": terminal.LAUNCHER_ITERM_APPLESCRIPT}

    def fake_open_tab(command: str, *, tmux_fallback: bool) -> str:
        del tmux_fallback
        typed.append(command)
        return landed["rung"]

    monkeypatch.setattr(terminal, "_launcher_mode", lambda: "iterm")
    monkeypatch.setattr(terminal, "_open_tab", fake_open_tab)
    focused: list[str] = []

    def fake_focus(target: str) -> bool:
        focused.append(target)
        return True

    monkeypatch.setattr(terminal, "focus_iterm_session", fake_focus)
    monkeypatch.setattr(terminal, "focus_tmux_window", fake_focus)
    return {
        "private": private,
        "work": work,
        "repo": repo,
        "adapter": adapter,
        "typed": typed,
        "landed": landed,
        "focused": focused,
        "tmp": tmp_path,
    }


def _run(*argv: str) -> int:
    return int(cli.main(["resume-job", SID, *argv]))


def _envelope(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, out
    doc = json.loads(out[0])
    assert isinstance(doc, dict)
    return doc


def _error_code(capsys: pytest.CaptureFixture[str]) -> str:
    doc = _envelope(capsys)
    assert doc["ok"] is False
    return str(doc["error"]["code"])


def _row_dir() -> str:
    with Store() as store:
        row = store.get(SID)
    assert row is not None
    return row.config_dir


def _prompt_file(env: dict[str, Any], text: str = "answer: ship it", mode: int = 0o600) -> Path:
    path = Path(env["tmp"]) / "answer.txt"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _stdin_file(command: str) -> Path:
    match = re.search(r"\} < (\S+)$", command)
    assert match, command
    return Path(shlex.split(match.group(1))[0])


# --------------------------------------------------------------------------- #
# cross-account resume: the pin, the flags, the store, the next listing
# --------------------------------------------------------------------------- #
def test_private_to_work_pins_work_and_carries_flags(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("-A", "work", "-j") == 0
    doc = _envelope(capsys)
    assert doc["ok"] is True
    assert doc["data"] == {"session_id": SID, "result": "resumed", "account": "work"}
    [command] = env["typed"]
    assert f"export CLAUDE_CONFIG_DIR={shlex.quote(str(env['work']))};" in command
    assert "unset CLAUDE_SECURESTORAGE_CONFIG_DIR;" in command
    assert "export CCC_NO_CODEX=1;" in command  # no_codex travels across the account
    assert f"--resume {SID}" in command
    assert accounts.is_trusted(str(env["work"]), env["repo"])  # ensure_trusted ran
    assert accounts.same_config_dir(_row_dir(), str(env["work"]))


def test_work_to_private_unsets_both_vars(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    with Store() as store:
        store.update_fields(SID, config_dir=str(env["work"]))
    assert _run("-A", "private", "-j") == 0
    assert _envelope(capsys)["data"]["account"] == "private"
    [command] = env["typed"]
    assert "unset CLAUDE_SECURESTORAGE_CONFIG_DIR CLAUDE_CONFIG_DIR;" in command
    assert "export CLAUDE_CONFIG_DIR" not in command
    assert accounts.same_config_dir(_row_dir(), str(env["private"]))


def test_launch_profile_travels_across_accounts(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config, "resolve_launch_profile", lambda label: f"/opt/bin/{label}-x")
    with Store() as store:
        store.update_fields(SID, launch_profile="team")
    assert _run("-A", "work", "-j") == 0
    assert _envelope(capsys)["ok"] is True
    [command] = env["typed"]
    assert "export CCC_LAUNCH_PROFILE=team;" in command
    assert "/opt/bin/team-x --" in command or "/opt/bin/team-x " in command


def test_next_sessions_listing_reports_the_new_account(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("-A", "work", "-j") == 0
    capsys.readouterr()
    [command] = env["typed"]
    # The resumed claude registers under the dir its env pins: read it off the typed line.
    match = re.search(r"export CLAUDE_CONFIG_DIR=(\S+);", command)
    assert match
    pinned = shlex.split(match.group(1))[0]
    live = LiveSession(
        pid=4242, session_id=SID, cwd=str(env["repo"]), alive=True, config_dir=pinned
    )
    with Store() as store:
        entries = bridge_cli.collect_sessions(
            adapter=FakeAdapter([live]),  # type: ignore[arg-type]
            store=store,
            panes_reader=lambda: [],
            ps_reader=dict,
            agents_reader=list,
        )
    assert [(e["session_id"], e["account"]) for e in entries] == [(SID, "work")]


def test_same_account_label_resumes_like_no_flag(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("-A", "private", "-j") == 0
    assert _envelope(capsys)["data"]["account"] == "private"
    assert "unset CLAUDE_SECURESTORAGE_CONFIG_DIR CLAUDE_CONFIG_DIR;" in env["typed"][0]


def test_plain_text_mode_names_the_account(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("-A", "work") == 0
    assert f"resuming in a new tab: {SID} (account: work)" in capsys.readouterr().out
    assert env["typed"]


# --------------------------------------------------------------------------- #
# refusals: one error code each, nothing typed, the row unchanged
# --------------------------------------------------------------------------- #
def test_unknown_account(env: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("-A", "nope", "-j") == 1
    assert _error_code(capsys) == "unknown_account"
    assert not env["typed"]


def test_disabled_account_refused(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """tp#2197: ``-A`` naming a seat in ``claude_accounts_disabled`` is refused, exit 1."""
    monkeypatch.setattr(config, "claude_disabled_accounts", lambda: {"work"})
    assert _run("-A", "work", "-j") == 1
    assert _error_code(capsys) == "disabled_account"
    assert _run("-A", "work") == 1
    err = capsys.readouterr().err
    assert "account 'work' is disabled (claude_accounts_disabled)" in err
    assert "enabled accounts: private" in err
    assert not env["typed"]
    with Store() as store:
        row = store.get(SID)
    assert row is not None and row.config_dir == str(env["private"])


@pytest.mark.usefixtures("env")
def test_new_job_disabled_account_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """tp#2197: ``new-job --account`` refuses a disabled seat the same way (no row made)."""
    monkeypatch.setattr(config, "claude_disabled_accounts", lambda: {"work"})
    assert cli._account_config_dir("work") == (
        "",
        "error: account 'work' is disabled (claude_accounts_disabled) — enabled accounts: private",
    )
    assert cli._account_config_dir("private")[1] is None
    with Store() as store:
        before = len(store.list_sessions(include_archived=True))
    assert cli.main(["new-job", "--aim", "x", "--account", "work"]) == 1
    assert "is disabled" in capsys.readouterr().err
    with Store() as store:
        assert len(store.list_sessions(include_archived=True)) == before


@pytest.mark.usefixtures("env")
def test_continue_session_target_refuses_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """tp#2197: ``continue-session start -A`` (cc-relay ``-t`` on a live tab) refuses too."""
    from command_center import continue_session

    monkeypatch.setattr(config, "claude_disabled_accounts", lambda: {"work"})
    with pytest.raises(continue_session.StartRefused, match="'work' is disabled"):
        continue_session._target_dir("work")


def test_not_shared_when_each_account_has_its_own_copy(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    work_projects = Path(env["work"]) / "projects"
    work_projects.unlink()  # break the shared store: work gets its own copy
    slug = str(env["repo"]).replace("/", "-")
    (work_projects / slug).mkdir(parents=True)
    (work_projects / slug / f"{SID}.jsonl").write_text("{}\n", encoding="utf-8")
    assert _run("-A", "work", "-j") == 1
    assert _error_code(capsys) == "not_shared"
    assert not env["typed"]
    assert accounts.same_config_dir(_row_dir(), str(env["private"]))


def test_not_shared_when_the_target_lacks_the_transcript(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    (Path(env["work"]) / "projects").unlink()
    assert _run("-A", "work", "-j") == 1
    assert _error_code(capsys) == "not_shared"


def test_untrusted_when_the_target_account_never_ran(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    (Path(env["work"]) / ".claude.json").unlink()  # nothing to write the trust flag into
    assert _run("-A", "work", "-j") == 1
    assert _error_code(capsys) == "untrusted"
    assert not env["typed"]


def test_live_session_refused_under_account_without_focus(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    with Store() as store:
        store.update_fields(SID, iterm_session_id="w0t1p0:UUID")
    env["adapter"].live = [LiveSession(pid=7, session_id=SID, cwd=str(env["repo"]), alive=True)]
    assert _run("-A", "work", "-j") == 1
    assert _error_code(capsys) == "live"
    assert not env["typed"] and not env["focused"]


def test_live_session_without_account_still_focuses(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    with Store() as store:
        store.update_fields(SID, iterm_session_id="w0t1p0:UUID")
    env["adapter"].live = [LiveSession(pid=7, session_id=SID, cwd=str(env["repo"]), alive=True)]
    assert _run("-j") == 0
    assert _envelope(capsys)["data"] == {
        "session_id": SID,
        "result": "focused",
        "account": "private",
        "focused": "focused live tab",
    }
    assert env["focused"] == ["w0t1p0:UUID"] and not env["typed"]


def test_no_transcript(env: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    slug = str(env["repo"]).replace("/", "-")
    (Path(env["private"]) / "projects" / slug / f"{SID}.jsonl").unlink()
    assert _run("-A", "work", "-j") == 1
    assert _error_code(capsys) == "no_transcript"


def test_no_tab_keeps_the_row_and_removes_both_prompt_files(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    env["landed"]["rung"] = ""  # every launcher rung failed
    answer = _prompt_file(env)
    assert _run("-A", "work", "-P", str(answer), "-j") == 1
    assert _error_code(capsys) == "no_tab"
    assert not answer.exists()
    [command] = env["typed"]
    assert not _stdin_file(command).exists()  # the private temp copy is gone too
    assert accounts.same_config_dir(_row_dir(), str(env["private"]))


def test_automated_env_exits_75_with_code(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(automated.ENV, "1")
    answer = _prompt_file(env)
    assert _run("-A", "work", "-P", str(answer), "-j") == automated.EXIT_SKIPPED
    assert _error_code(capsys) == "automated"
    assert not env["typed"] and not answer.exists()


# --------------------------------------------------------------------------- #
# -P: the first prompt rides on stdin from a private file, never in the typed line
# --------------------------------------------------------------------------- #
def test_prompt_file_becomes_stdin_and_is_unlinked(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    text = "line one\nline two: keep going"
    answer = _prompt_file(env, text)
    assert _run("-A", "work", "-P", str(answer), "-j") == 0
    assert _envelope(capsys)["ok"] is True
    assert not answer.exists()
    [command] = env["typed"]
    assert "line one" not in command and "keep going" not in command
    stdin_file = _stdin_file(command)
    assert f"{{ rm -f -- {shlex.quote(str(stdin_file))}; " in command
    assert stdin_file.read_text(encoding="utf-8") == text
    assert stat.S_IMODE(os.stat(stdin_file).st_mode) == 0o600
    stdin_file.unlink()  # the tab's shell would have removed it


def test_prompt_file_with_wide_mode_is_refused_and_unlinked(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    answer = _prompt_file(env, mode=0o644)
    assert _run("-A", "work", "-P", str(answer), "-j") == 2
    assert _error_code(capsys) == "prompt_file"
    assert not answer.exists() and not env["typed"]


def test_prompt_file_refuses_a_live_session(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    env["adapter"].live = [LiveSession(pid=7, session_id=SID, cwd=str(env["repo"]), alive=True)]
    answer = _prompt_file(env)
    assert _run("-P", str(answer), "-j") == 1
    assert _error_code(capsys) == "live"
    assert not answer.exists() and not env["focused"]


# --------------------------------------------------------------------------- #
# -P: the input-ledger row of the first prompt (tp#2170 B3d)
# --------------------------------------------------------------------------- #
def _ledger_rows() -> list[dict[str, Any]]:
    """Every ``inputs`` row of the test store (``CLAUDE_HOME`` is pinned under tmp)."""
    with Store() as store:
        cur = store.conn.execute("SELECT * FROM inputs ORDER BY rowid")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def test_prompt_file_is_ledgered_before_the_tab_opens(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One ``resume_prompt`` / ``caller`` row of the prompt's sha, written before the tab
    opens; no column, output or log line carries the text."""
    from command_center.input_ledger import content_sha

    text = "first prompt: feed the wombat"
    seen_at_open: list[int] = []
    recording = terminal._open_tab  # the env fixture's fake

    def open_tab(command: str, *, tmux_fallback: bool) -> str:
        seen_at_open.append(len(_ledger_rows()))
        return str(recording(command, tmux_fallback=tmux_fallback))

    monkeypatch.setattr(terminal, "_open_tab", open_tab)
    answer = _prompt_file(env, text)
    assert _run("-P", str(answer), "-j") == 0
    assert seen_at_open == [1]
    (row,) = _ledger_rows()
    assert (row["kind"], row["origin"], row["state"]) == ("resume_prompt", "caller", "pending")
    assert row["session_id"] == SID and row["content_sha"] == content_sha(text)
    assert row["alt_sha"] == "" and row["cwd"] == str(env["repo"])
    assert accounts.same_config_dir(row["config_dir"], str(env["private"]))
    assert str(row["transcript_path"]).endswith(f"{SID}.jsonl")
    out = capsys.readouterr()
    for blob in [*map(str, row.values()), out.out, out.err, *env["typed"]]:
        assert "wombat" not in blob
    _stdin_file(env["typed"][0]).unlink()  # the tab's shell would have removed it


def test_prompt_file_ledger_row_failed_when_no_tab(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    env["landed"]["rung"] = ""  # every launcher rung failed
    answer = _prompt_file(env, "never sent: the wombat")
    assert _run("-P", str(answer), "-j") == 1
    assert _error_code(capsys) == "no_tab"
    (row,) = _ledger_rows()
    assert (row["kind"], row["state"], row["reason"]) == ("resume_prompt", "failed", "not_typed")


def test_resume_without_prompt_writes_no_ledger_row(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("-j") == 0
    assert _envelope(capsys)["ok"] is True
    assert env["typed"] and not _ledger_rows()


def test_namespace_without_new_flags_still_works(env: dict[str, Any]) -> None:
    """Callers that build the Namespace by hand (older tests, the TUI) keep working."""
    assert cli.cmd_resume_job(argparse.Namespace(session_id=SID)) == 0
    assert "unset CLAUDE_SECURESTORAGE_CONFIG_DIR CLAUDE_CONFIG_DIR;" in env["typed"][0]


# --------------------------------------------------------------------------- #
# helpers: the one-file check and the usable-account listing
# --------------------------------------------------------------------------- #
def test_transcripts_are_one_file(env: dict[str, Any]) -> None:
    slug = str(env["repo"]).replace("/", "-")
    found = Path(env["private"]) / "projects" / slug / f"{SID}.jsonl"
    assert accounts.transcripts_are_one_file(found, SID, str(env["private"]), str(env["work"]))
    assert not accounts.transcripts_are_one_file(found, SID)
    assert not accounts.transcripts_are_one_file(found, SID, str(env["tmp"] / "elsewhere"))


@pytest.mark.usefixtures("env")
def test_job_account_json_reports_usable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home_json = Path.home() / ".claude.json"  # the default account's file
    home_json.write_text(
        json.dumps({"oauthAccount": {"emailAddress": "a@example.org"}}), encoding="utf-8"
    )
    monkeypatch.setattr(config, "claude_account_email_map", dict)
    assert cli.main(["job-account", "-j"]) == 0
    doc = json.loads(capsys.readouterr().out)
    rows = {r["label"]: r for r in doc["accounts"]}
    assert rows["private"]["usable"] is True and rows["private"]["default"] is True
    assert rows["work"] == {
        "label": "work",
        "default": False,
        "usable": False,
        "reason": "logged_out",
        "pick": rows["work"]["pick"],
    }
    monkeypatch.setattr(config, "claude_account_email_map", lambda: {"private": "b@example.org"})
    assert accounts.account_unusable_reason("private") == "identity_mismatch"
    assert accounts.account_unusable_reason("nope") == "unknown"


@pytest.mark.usefixtures("env")
def test_disabled_account_is_not_usable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A retired seat that is still logged in is reported ``disabled``, not usable."""
    (Path.home() / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "a@example.org"}}), encoding="utf-8"
    )
    monkeypatch.setattr(config, "claude_account_email_map", dict)
    assert accounts.account_unusable_reason("private") == ""
    monkeypatch.setattr(config, "claude_disabled_accounts", lambda: {"private"})
    assert accounts.account_unusable_reason("private") == "disabled"
    assert cli.main(["job-account", "-j"]) == 0
    rows = {r["label"]: r for r in json.loads(capsys.readouterr().out)["accounts"]}
    assert rows["private"]["usable"] is False and rows["private"]["reason"] == "disabled"


def test_parse_claude_accounts_disabled() -> None:
    assert config.parse_claude_accounts_disabled(["work", " x ", "Bad/1", 3]) == {"work", "x"}
    assert config.parse_claude_accounts_disabled("work") == set()
    assert not config.DEFAULTS["claude_accounts_disabled"]
