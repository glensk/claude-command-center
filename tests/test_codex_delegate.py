"""Tests for the codex-in-claude engine and the future-job codex launch wiring.

The engine now lives inside the package as ``command_center.codex_in_claude`` (the
``codex-in-claude`` console entry point; the repo-root ``codex-in-claude.py`` is a thin
PATH-compat shim). Tests avoid any live Codex call: subprocess and the model catalog are
monkeypatched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from types import ModuleType

import pytest

from command_center.models import job_launch_prefix
from command_center.store import Store

_FAKE_CATALOG = [
    {"slug": "gpt-5.6-sol", "visibility": "list", "default_reasoning_level": "medium"},
    {"slug": "gpt-5.5", "visibility": "list", "default_reasoning_level": "xhigh"},
    {"slug": "gpt-5.4", "visibility": "list", "default_reasoning_level": "medium"},
    {"slug": "codex-auto-review", "visibility": "hide", "default_reasoning_level": "medium"},
]


def _load_engine() -> ModuleType:
    import command_center.codex_in_claude as engine

    return engine


@pytest.fixture()
def cic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The engine module, with config + Claude home pointed at tmp and the catalog faked.

    ``CLAUDE_CONFIG_DIR`` matters: ``set-model``/``set-effort`` re-stamp the model marker
    into the codex skill/command descriptions, and tests must never touch the real ones.
    ``CODEX_HOME`` matters too: ``codex_refusal`` scans the real rollout files for a
    recorded refusal, so without this a genuinely blocked seat on the developer's machine
    turns every headroom test's verdict into ``blocked``.
    """
    mod = _load_engine()
    monkeypatch.setenv("CODEX_IN_CLAUDE_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(mod, "list_models", lambda **_: list(_FAKE_CATALOG))
    return mod


# --------------------------- config / model resolution --------------------------- #
def test_resolve_model_defaults_to_newest(cic: ModuleType) -> None:
    assert cic.DEFAULT_MODEL == "gpt-5.6-sol"
    assert cic.resolve_model(None) == cic.DEFAULT_MODEL
    assert cic.resolve_model("delegate-review") == cic.DEFAULT_MODEL


def test_resolve_model_precedence(cic: ModuleType) -> None:
    cic.save_config({"default": "gpt-5.4", "delegate-review": "gpt-5.5", "debate": None})
    assert cic.resolve_model("delegate-review") == "gpt-5.5"  # per-command wins
    assert cic.resolve_model("debate") == "gpt-5.4"  # falls back to default
    assert cic.resolve_model(None) == "gpt-5.4"


def test_config_roundtrip_and_corrupt(cic: ModuleType, tmp_path: Path) -> None:
    path = cic.save_config({"default": "gpt-5.4", "delegate-review": None, "debate": "gpt-5.5"})
    assert Path(path).exists()
    assert cic.load_config()["debate"] == "gpt-5.5"
    Path(path).write_text("{not json", encoding="utf-8")  # corrupt → defaults, no crash
    assert cic.load_config()["default"] == cic.DEFAULT_MODEL


def test_set_model_for_all_clears_per_command_pins(cic: ModuleType) -> None:
    """--for all is a real reset: a stale per-command pin must not shadow the new default."""
    cic.save_config({"default": "gpt-5.4", "delegate-review": "gpt-5.5", "debate": "gpt-5.5"})
    assert cic.cmd_set_model(argparse.Namespace(slug="gpt-5.6-sol", for_command="all")) == cic.EX_OK
    assert cic.resolve_model("delegate-review") == "gpt-5.6-sol"
    assert cic.resolve_model("debate") == "gpt-5.6-sol"
    # a bare set-model (no --for) still only moves the default, leaving pins alone
    cic.save_config({"default": "gpt-5.4", "delegate-review": "gpt-5.5", "debate": None})
    assert cic.cmd_set_model(argparse.Namespace(slug="gpt-5.6-sol", for_command=None)) == cic.EX_OK
    assert cic.resolve_model("delegate-review") == "gpt-5.5"


def test_parse_models_list_and_dict(cic: ModuleType) -> None:
    assert cic._parse_models('[{"slug": "a"}]') == [{"slug": "a"}]
    assert cic._parse_models('{"models": [{"slug": "b"}]}') == [{"slug": "b"}]


def test_valid_slug_and_effort(cic: ModuleType) -> None:
    assert cic.valid_slug("gpt-5.5") is True
    assert cic.valid_slug("nope") is False
    assert cic.effort_of("gpt-5.5") == "xhigh"


# --------------------------- quota parsing / headroom --------------------------- #
_HEADROOM_NOW = 2_000_000_000
_HEADROOM_FIXTURES = Path(__file__).parent / "fixtures" / "codex_headroom"


def _install_headroom_fixture(
    cic: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout = tmp_path / "sessions" / "2033" / "05" / "18" / "rollout-fixture.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text((_HEADROOM_FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")


def _install_cost_fixture(
    cic: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> Path:
    path = tmp_path / "cost-history.jsonl"
    monkeypatch.setenv("CODEX_IN_CLAUDE_COST_LOG", str(path))
    path.write_text((_HEADROOM_FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(cic.time, "time", lambda: float(_HEADROOM_NOW))
    return path


@pytest.mark.parametrize(
    ("fixture_name", "state", "allowed", "durations"),
    [
        ("one_window.jsonl", "allowed", True, [300]),
        ("two_windows.jsonl", "allowed", True, [300, 10080]),
        ("reordered_windows.jsonl", "reserve", False, [300, 10080]),
        ("missing_window_duration.jsonl", "unknown", False, [300]),
        ("stale.jsonl", "unknown", False, [300, 10080]),
        ("no_data.jsonl", "unknown", False, []),
    ],
)
def test_codex_headroom_jsonl_fixtures(
    cic: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    state: str,
    allowed: bool,
    durations: list[int],
) -> None:
    _install_headroom_fixture(cic, tmp_path, monkeypatch, fixture_name)
    decision = cic.codex_headroom(now=_HEADROOM_NOW)
    assert decision["state"] == state
    assert decision["offload_allowed"] is allowed
    assert [row["window_minutes"] for row in decision["windows"]] == durations


def test_codex_windows_are_keyed_by_duration_not_position(
    cic: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_headroom_fixture(cic, tmp_path, monkeypatch, "reordered_windows.jsonl")
    windows = cic._codex_usage_windows(now=_HEADROOM_NOW)
    assert windows["five_hour"] == (10.0, 2_000_007_200)
    assert windows["seven_day"] == (80.0, 2_000_500_000)


def test_headroom_reset_within_ten_minutes_counts_as_fresh(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = cic._CodexRateSnapshot(
        captured_at=_HEADROOM_NOW,
        windows={
            300: cic._CodexRateWindow(
                used_percent=99.0,
                resets_at=_HEADROOM_NOW + 600,
                window_minutes=300,
            )
        },
    )
    monkeypatch.setattr(cic, "_codex_rate_snapshot", lambda: snapshot)
    decision = cic.codex_headroom(now=_HEADROOM_NOW)
    assert decision["state"] == "allowed"
    assert decision["windows"][0]["reset_fresh"] is True


@pytest.mark.parametrize(
    ("fixture_name", "expected_exit", "expected_state"),
    [
        ("one_window.jsonl", 0, "allowed"),
        ("reordered_windows.jsonl", 1, "reserve"),
        ("stale.jsonl", 3, "unknown"),
    ],
)
def test_headroom_cli_json_and_exit_codes(
    cic: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fixture_name: str,
    expected_exit: int,
    expected_state: str,
) -> None:
    _install_headroom_fixture(cic, tmp_path, monkeypatch, fixture_name)
    monkeypatch.setattr(cic.time, "time", lambda: float(_HEADROOM_NOW))
    assert cic.main(["headroom", "--json"]) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == expected_state
    assert payload["offload_allowed"] is (expected_exit == 0)


def test_usage_json_carries_window_duration(
    cic: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_headroom_fixture(cic, tmp_path, monkeypatch, "reordered_windows.jsonl")
    monkeypatch.setattr(cic.time, "time", lambda: float(_HEADROOM_NOW))
    assert cic.main(["usage", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["five_hour"]["window_minutes"] == 300
    assert payload["five_hour"]["used_percent"] == 10.0
    assert payload["seven_day"]["window_minutes"] == 10080
    assert payload["seven_day"]["used_percent"] == 80.0


def test_headroom_help_says_unknown_is_offload_only(cic: ModuleType) -> None:
    help_text = cic.build_parser()._subparsers._group_actions[0].choices["headroom"].format_help()
    normalized = " ".join(help_text.split())
    assert "Missing/stale data fails closed" in normalized
    assert "debates remain always allowed" in normalized


def test_learned_reserve_p95_and_exact_ten_sample_switchover(
    cic: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _install_cost_fixture(cic, tmp_path, monkeypatch, "learned_costs.jsonl")
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:9]) + "\n", encoding="utf-8")
    assert cic.headroom_reserve_percent(300) == 35.0

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # nearest-rank P95 of ten values is the maximum (4); 3 * 4 * 1.10 = 13.2
    assert cic.headroom_reserve_percent(300) == pytest.approx(13.2)
    assert cic._headroom_reserve(300)[1] == "learned"
    assert cic._headroom_reserve(10080) == (35.0, "bootstrap")


@pytest.mark.parametrize(("delta", "expected"), [(0.1, 5.0), (30.0, 60.0)])
def test_learned_reserve_clamps_both_ends(
    cic: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delta: float,
    expected: float,
) -> None:
    path = tmp_path / "cost-history.jsonl"
    monkeypatch.setenv("CODEX_IN_CLAUDE_COST_LOG", str(path))
    monkeypatch.setattr(cic.time, "time", lambda: float(_HEADROOM_NOW))
    rows = [
        {
            "ts": _HEADROOM_NOW - i,
            "purpose": "debate",
            "model": "m",
            "effort": "high",
            "before": {"300": {"used_percent": 10.0, "resets_at": _HEADROOM_NOW + 5000}},
            "after": {"300": {"used_percent": 10.0 + delta, "resets_at": _HEADROOM_NOW + 5000}},
        }
        for i in range(10)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert cic.headroom_reserve_percent(300) == expected


def test_cost_history_ignores_corrupt_partial_non_debate_and_reset_samples(
    cic: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_cost_fixture(cic, tmp_path, monkeypatch, "mixed_costs.jsonl")
    assert cic._debate_cost_deltas(300) == [1.5]
    assert cic.headroom_reserve_percent(300) == 35.0


def test_cost_history_prunes_rows_older_than_ninety_days_on_write(
    cic: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _HEADROOM_NOW
    path = tmp_path / "cost-history.jsonl"
    monkeypatch.setenv("CODEX_IN_CLAUDE_COST_LOG", str(path))
    old = {"ts": now - cic._COST_HISTORY_SECONDS - 1, "purpose": "debate"}
    boundary = {"ts": now - cic._COST_HISTORY_SECONDS, "purpose": "debate"}
    path.write_text(
        json.dumps(old) + "\n" + "corrupt\n" + json.dumps(boundary) + "\n",
        encoding="utf-8",
    )
    assert cic.record_codex_run(
        purpose="delegate", model="m", effort="high", before={}, after={}, ts=now
    )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["ts"] for row in rows] == [boundary["ts"], now]


def test_headroom_surfaces_reserve_source_in_json_and_human_output(
    cic: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_headroom_fixture(cic, tmp_path, monkeypatch, "one_window.jsonl")
    monkeypatch.setattr(cic.time, "time", lambda: float(_HEADROOM_NOW))
    decision = cic.codex_headroom(now=_HEADROOM_NOW)
    assert decision["windows"][0]["reserve_source"] == "bootstrap"
    cic.cmd_headroom(argparse.Namespace(json=False))
    assert "reserve_source bootstrap" in capsys.readouterr().out


# --------------------------- delegate prompt contract --------------------------- #
def test_patch_contract_demands_diff(cic: ModuleType) -> None:
    prompt = cic._build_delegate_prompt("add x", write=False, feedback=None, round_no=1)
    assert "READ-ONLY" in prompt and "### DIFF" in prompt and "add x" in prompt


def test_write_contract_allows_edits(cic: ModuleType) -> None:
    prompt = cic._build_delegate_prompt("add x", write=True, feedback="tests failed", round_no=2)
    assert "edit files" in prompt and "tests failed" in prompt and "REVISION (round 2)" in prompt


# --------------------------- delegate run (no live codex) --------------------------- #
def _ns(**kw: object) -> argparse.Namespace:
    base = dict(
        prompt="do x",
        write=False,
        scout=False,
        cwd=None,
        round=1,
        feedback=None,
        model=None,
        effort=None,
        timeout=600,
        idle_timeout=0,  # unit tests: no stall watchdog
        # 0 disables the flock concurrency gate (limit <= 0 short-circuits in
        # _concurrency_slot), so these unit tests never touch the real slot dir.
        max_concurrent=0,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_delegate_prints_model_first_and_assembles_cmd(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        out_path = cmd[cmd.index("-o") + 1]  # codex writes its final message here
        Path(out_path).write_text("### SELF-CHECK\nok\n### DIFF\n```diff\n```\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    rc = cic.cmd_delegate(_ns())
    out = capsys.readouterr().out
    assert rc == cic.EX_OK
    assert out.splitlines()[0] == "model: gpt-5.6-sol (effort xhigh)"  # guaranteed 1st line
    assert captured["cmd"][:3] == ["codex", "exec", "-s"]
    assert "read-only" in captured["cmd"] and "-m" in captured["cmd"]
    assert "gpt-5.6-sol" in captured["cmd"]


def test_delegate_records_cost_snapshots_and_purpose_without_stdout_preamble(
    cic: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshots = iter(
        [
            {"300": {"used_percent": 10.0, "resets_at": 2_000_100_000}},
            {"300": {"used_percent": 11.5, "resets_at": 2_000_100_000}},
        ]
    )

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic, "codex_cost_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    assert cic.cmd_delegate(_ns(purpose="debate")) == cic.EX_OK
    assert capsys.readouterr().out.splitlines()[0] == "model: gpt-5.6-sol (effort xhigh)"
    rows = [
        json.loads(line)
        for line in cic.codex_cost_history_path().read_text(encoding="utf-8").splitlines()
    ]
    assert rows == [
        {
            "ts": rows[0]["ts"],
            "purpose": "debate",
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
            "before": {"300": {"used_percent": 10.0, "resets_at": 2_000_100_000}},
            "after": {"300": {"used_percent": 11.5, "resets_at": 2_000_100_000}},
        }
    ]


def test_delegate_parser_purpose_defaults_and_override(cic: ModuleType) -> None:
    parser = cic.build_parser()
    assert parser.parse_args(["delegate", "x"]).purpose == "delegate"
    assert parser.parse_args(["delegate", "--purpose", "review", "x"]).purpose == "review"


def test_delegate_write_mode_uses_workspace_write(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("done", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    monkeypatch.setattr(cic, "_git_status", lambda _cwd: [])
    assert cic.cmd_delegate(_ns(write=True)) == cic.EX_OK
    assert "workspace-write" in captured["cmd"]


def test_delegate_scout_is_readonly_plan(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """--scout forces read-only (even with --write) and uses the PLAN contract, no diff."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("### PLAN\n1. ...", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    # scout wins over write → read-only sandbox, and the prompt is the scout contract
    assert cic.cmd_delegate(_ns(scout=True, write=True)) == cic.EX_OK
    assert "read-only" in captured["cmd"] and "workspace-write" not in captured["cmd"]
    prompt = captured["cmd"][-1]
    assert "SCOUTING" in prompt and "### PLAN" in prompt and "NOT write" in prompt


def test_delegate_effort(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    # no -e flag -> the config-default effort (xhigh) resolves, so the -c override IS
    # passed and the first line reflects it (shown_effort comes from config, not catalog).
    assert cic.cmd_delegate(_ns()) == cic.EX_OK
    assert "model_reasoning_effort=xhigh" in " ".join(captured["cmd"])
    assert capsys.readouterr().out.splitlines()[0] == "model: gpt-5.6-sol (effort xhigh)"
    # explicit -e high -> passed through and reflected in the first line
    assert cic.cmd_delegate(_ns(effort="high")) == cic.EX_OK
    assert "model_reasoning_effort=high" in " ".join(captured["cmd"])
    assert capsys.readouterr().out.splitlines()[0] == "model: gpt-5.6-sol (effort high)"


def test_set_get_effort(cic: ModuleType) -> None:
    """set-effort persists a valid level; a fresh config resolves to the xhigh base default.

    'default' clears the explicit key by writing an explicit ``"effort": null`` that
    overrides the xhigh base default in load_config's ``base.update(data)``, so
    resolve_effort then returns None (not xhigh).
    """
    assert cic.resolve_effort() == "xhigh"
    cic.cmd_set_effort(argparse.Namespace(level="high"))
    assert cic.resolve_effort() == "high"
    cic.cmd_set_effort(argparse.Namespace(level="default"))
    assert cic.resolve_effort() is None


def test_delegate_exit_codes(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_notfound(*_: object, **__: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(cic, "_exec_codex", raise_notfound)
    assert cic.cmd_delegate(_ns()) == cic.EX_NO_CODEX

    def raise_timeout(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired("codex", 1)

    monkeypatch.setattr(cic, "_exec_codex", raise_timeout)
    assert cic.cmd_delegate(_ns()) == cic.EX_TIMEOUT

    def raise_stalled(*_: object, **__: object) -> None:
        raise cic.CodexStalledError(120, "codex› last thing it said\n")

    monkeypatch.setattr(cic, "_exec_codex", raise_stalled)
    assert cic.cmd_delegate(_ns()) == cic.EX_TIMEOUT

    assert cic.cmd_delegate(_ns(model="bogus")) == cic.EX_INVALID_MODEL
    assert cic.cmd_delegate(_ns(prompt="   ")) == cic.EX_USAGE


# --------------------------- timeouts / budget / supervision --------------------------- #
def test_effective_timeout_scales_with_effort(cic: ModuleType) -> None:
    assert cic._effective_timeout(None, "low") == 600
    assert cic._effective_timeout(None, "medium") == 900
    assert cic._effective_timeout(None, "high") == 1500
    assert cic._effective_timeout(None, "xhigh") == 2700
    assert cic._effective_timeout(None, "unknown") == 900  # falls back to medium
    assert cic._effective_timeout(42, "xhigh") == 42  # explicit -t always wins
    assert cic._effective_timeout(0, "xhigh") == 0  # -t 0 = no wall limit


def test_prompt_unlimited_wall_note(cic: ModuleType) -> None:
    prompt = cic._build_delegate_prompt(
        "do x", write=True, feedback=None, round_no=1, budget_minutes=None, idle_minutes=15
    )
    assert "no hard wall-clock limit" in prompt
    assert "~15 minutes with NO output" in prompt


def test_prompt_repo_map_injected(cic: ModuleType) -> None:
    prompt = cic._build_delegate_prompt(
        "do x", write=True, feedback=None, round_no=1, repo_map="REPO MAP (test):\nbin/ (9 files)"
    )
    assert "REPO MAP (test):" in prompt
    assert prompt.index("REPO MAP") < prompt.index("TASK:")


def test_repo_map_prefers_repo_scope_short(cic: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "repo_scope_short.md").write_text("# myrepo\nDoes things.", encoding="utf-8")
    result = cic._repo_map(str(tmp_path))
    assert result is not None and "Does things." in result and "repo_scope_short" in result
    assert cic._repo_map(None) is None


def test_repo_map_git_fallback(cic: ModuleType, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "top.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    result = cic._repo_map(str(tmp_path))
    assert result is not None and "pkg/ (1 files)" in result and "top.py" in result


def test_session_id_of(cic: ModuleType) -> None:
    banner = "model: gpt-5.6-sol\nsession id: 019ff5b3-7bea-7c80-ad5e-21cc5b7c64bd\n----\n"
    assert cic._session_id_of(banner) == "019ff5b3-7bea-7c80-ad5e-21cc5b7c64bd"
    assert cic._session_id_of("no session here") is None


def test_delegate_resume_assembles_resume_cmd(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, list[str]] = {}
    uuid = "019ff5b3-7bea-7c80-ad5e-21cc5b7c64bd"

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_text("resumed ok", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", f"session id: {uuid}\n")

    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    assert cic.cmd_delegate(_ns(resume=uuid)) == cic.EX_OK
    assert captured["cmd"][:4] == ["codex", "exec", "resume", uuid]
    assert "-s" not in captured["cmd"]  # sandbox inherited from the resumed session
    out = capsys.readouterr().out
    assert "### SESSION" in out and uuid in out  # session id reported for the next round


def test_delegate_runs_lists_live_and_cleans_dead(
    cic: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cic, "RUNS_DIR", tmp_path)
    import json as _json

    live = {
        "pid": os.getpid(),
        "model": "m",
        "effort": "e",
        "repo": "/r",
        "elapsed_s": 61,
        "idle_s": 2,
        "lines": 10,
        "last_line": "editing",
    }
    (tmp_path / "live.json").write_text(_json.dumps(live), encoding="utf-8")
    dead = dict(live, pid=99999999)
    (tmp_path / "dead.json").write_text(_json.dumps(dead), encoding="utf-8")
    assert cic.cmd_runs(argparse.Namespace(json=False)) == cic.EX_OK
    out = capsys.readouterr().out
    assert f"pid {os.getpid()}" in out and "1m01s" in out and "editing" in out
    assert "99999999" not in out
    assert not (tmp_path / "dead.json").exists()  # stale heartbeat cleaned


def test_repo_map_explicit_file_wins(cic: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "repo_scope_short.md").write_text("auto map", encoding="utf-8")
    curated = tmp_path / "curated.md"
    curated.write_text("curated context only", encoding="utf-8")
    result = cic._repo_map(str(tmp_path), explicit=str(curated))
    assert result is not None and "curated context only" in result and "auto map" not in result
    # unreadable explicit file -> warning path, no map, no crash
    assert cic._repo_map(str(tmp_path), explicit=str(tmp_path / "missing.md")) is None


def test_delegate_show_prompt_dry_run(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise AssertionError("codex must not launch on --show-prompt")

    monkeypatch.setattr(cic, "_exec_codex", boom)
    assert cic.cmd_delegate(_ns(show_prompt=True, prompt="do the thing")) == cic.EX_OK
    out = capsys.readouterr().out
    assert "### DRY RUN" in out and "do the thing" in out and "command: codex exec" in out


def test_delegate_pointer_hint_for_xhigh(
    cic: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        Path(cmd[cmd.index("-o") + 1]).write_text("ok", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cic, "_exec_codex", fake_run)
    # config default resolves to xhigh, task has a file:line pointer -> hint
    assert cic.cmd_delegate(_ns(prompt="fix bin/tool.py:82 crash")) == cic.EX_OK
    assert "consider -e high" in capsys.readouterr().err
    # explicit effort chosen -> the caller decided; no second-guessing
    assert cic.cmd_delegate(_ns(prompt="fix bin/tool.py:82 crash", effort="xhigh")) == cic.EX_OK
    assert "consider -e high" not in capsys.readouterr().err


def test_exec_codex_writes_and_removes_heartbeat(cic: ModuleType, tmp_path: Path) -> None:
    import threading

    fake = _fake_codex(tmp_path, "sleep 2\nexit 0\n")
    hb = tmp_path / "hb.json"
    seen: list[bool] = []

    def watch() -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if hb.exists():
                seen.append(True)
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=watch)
    watcher.start()
    result = cic._exec_codex(
        [fake],
        env=dict(os.environ),
        timeout=0,
        idle_timeout=0,
        heartbeat_path=hb,
        heartbeat_meta={"model": "m"},
    )
    watcher.join()
    assert result.returncode == 0
    assert seen == [True]  # heartbeat existed while running (also proves -t 0 works)
    assert not hb.exists()  # and was removed on exit


def test_prompt_time_budget_note(cic: ModuleType) -> None:
    prompt = cic._build_delegate_prompt(
        "do x", write=True, feedback=None, round_no=1, budget_minutes=45, idle_minutes=15
    )
    assert "TIME BUDGET: ~45 minutes" in prompt
    assert "~15 minutes with NO output" in prompt
    assert "open those FIRST" in prompt
    # no budget -> no note (back-compat for direct callers)
    bare = cic._build_delegate_prompt("do x", write=True, feedback=None, round_no=1)
    assert "TIME BUDGET" not in bare


def _fake_codex(tmp_path: Path, body: str) -> str:
    """An executable stand-in for the codex CLI."""
    script = tmp_path / "fake-codex.sh"
    script.write_text("#!/bin/bash\n" + body)
    script.chmod(0o755)
    return str(script)


def _wait_gone(pids: list[int], timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive:
            return True
        time.sleep(0.1)
    return False


def test_exec_codex_wall_timeout_kills_process_tree(cic: ModuleType, tmp_path: Path) -> None:
    """On wall timeout the WHOLE group dies — including the fixer's own children."""
    pidfile = tmp_path / "pids"
    fake = _fake_codex(tmp_path, f'sleep 300 &\necho "$$ $!" > "{pidfile}"\nsleep 300\n')
    with pytest.raises(subprocess.TimeoutExpired):
        cic._exec_codex([fake], env=dict(os.environ), timeout=2, idle_timeout=0)
    pids = [int(p) for p in pidfile.read_text().split()]
    assert len(pids) == 2 and _wait_gone(pids)


def test_exec_codex_idle_watchdog_kills_stalled_run(cic: ModuleType, tmp_path: Path) -> None:
    """A run that goes silent is killed by the idle watchdog, tree and all."""
    pidfile = tmp_path / "pids"
    fake = _fake_codex(
        tmp_path,
        f'sleep 300 &\necho "$$ $!" > "{pidfile}"\necho "one line then silence"\nsleep 300\n',
    )
    with pytest.raises(cic.CodexStalledError):
        cic._exec_codex([fake], env=dict(os.environ), timeout=60, idle_timeout=1)
    pids = [int(p) for p in pidfile.read_text().split()]
    assert len(pids) == 2 and _wait_gone(pids)


def test_exec_codex_happy_path_captures_output(cic: ModuleType, tmp_path: Path) -> None:
    fake = _fake_codex(tmp_path, 'echo "reply on stdout"\necho "progress" >&2\nexit 0\n')
    result = cic._exec_codex([fake], env=dict(os.environ), timeout=30, idle_timeout=0)
    assert result.returncode == 0
    assert "reply on stdout" in result.stdout
    assert "progress" in result.stderr


# --------------------------- skill/command description markers --------------------------- #
_BLOCK_SKILL = """---
name: codex-debate
description: >-
  Run a bounded debate. Use in mode=plan to stress-test a plan (hooks: a PLAN*.md
  write) and in mode=diagnose otherwise.
---

# body
"""

_INLINE_CMD = """---
description: Delegate a task to Codex (Codex implements; Claude reviews) — saves tokens.
argument-hint: "[--write] <task>"
---

body
"""


def _surfaces(cic: ModuleType, tmp_path: Path) -> tuple[Path, Path]:
    """Create fake skill + command surfaces under the tmp CLAUDE_CONFIG_DIR."""
    home = cic.claude_home()
    skill = home / "skills" / "codex-debate" / "SKILL.md"
    cmd = home / "commands" / "codex-implement-task-and-claude-review.md"
    for path, text in ((skill, _BLOCK_SKILL), (cmd, _INLINE_CMD)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return skill, cmd


def test_marker_prefixes_block_and_inline_descriptions(cic: ModuleType, tmp_path: Path) -> None:
    """Both YAML shapes get the marker as a *prefix*; the inline one is quoted (YAML-safe)."""
    skill, cmd = _surfaces(cic, tmp_path)
    cic.save_config({"default": "gpt-5.6-sol", "effort": "xhigh"})
    rows = cic.sync_markers()
    assert {status for status, _, _ in rows if status != "missing"} == {"updated"}
    assert "  [codex gpt-5.6-sol effort=xhigh] Run a bounded debate." in skill.read_text()
    # inline value must be double-quoted, else a leading '[' reads as a YAML flow sequence
    line = next(ln for ln in cmd.read_text().splitlines() if ln.startswith("description:"))
    assert line == (
        'description: "[codex gpt-5.6-sol effort=xhigh] Delegate a task to Codex '
        '(Codex implements; Claude reviews) — saves tokens."'
    )


def test_marker_is_idempotent_and_replaces_stale(cic: ModuleType, tmp_path: Path) -> None:
    """Re-syncing never stacks markers; a model change rewrites the existing one in place."""
    skill, cmd = _surfaces(cic, tmp_path)
    cic.save_config({"default": "gpt-5.6-sol", "effort": "xhigh"})
    cic.sync_markers()
    first = (skill.read_text(), cmd.read_text())
    assert {s for s, _, _ in cic.sync_markers() if s != "missing"} == {"ok"}  # no-op re-run
    assert (skill.read_text(), cmd.read_text()) == first

    cic.save_config({"default": "gpt-5.4", "effort": "low"})
    assert {s for s, _, _ in cic.sync_markers() if s != "missing"} == {"updated"}
    for text in (skill.read_text(), cmd.read_text()):
        assert text.count("[codex ") == 1
        assert "[codex gpt-5.4 effort=low]" in text
        assert "gpt-5.6-sol" not in text


def test_set_model_syncs_descriptions(cic: ModuleType, tmp_path: Path) -> None:
    """The whole point: changing the model updates what /codex… help will show."""
    skill, _cmd = _surfaces(cic, tmp_path)
    assert cic.cmd_set_model(argparse.Namespace(slug="gpt-5.4", for_command="all")) == cic.EX_OK
    assert "[codex gpt-5.4 effort=xhigh]" in skill.read_text()
    assert cic.cmd_set_effort(argparse.Namespace(level="medium")) == cic.EX_OK
    assert "[codex gpt-5.4 effort=medium]" in skill.read_text()


def test_sync_check_reports_drift_without_writing(cic: ModuleType, tmp_path: Path) -> None:
    skill, _cmd = _surfaces(cic, tmp_path)
    before = skill.read_text()
    assert cic.cmd_sync_skills(argparse.Namespace(check=True)) == 1  # stale
    assert skill.read_text() == before  # --check never writes
    assert cic.cmd_sync_skills(argparse.Namespace(check=False)) == cic.EX_OK
    assert cic.cmd_sync_skills(argparse.Namespace(check=True)) == cic.EX_OK  # now current


def test_sync_tolerates_missing_and_unparsable_surfaces(cic: ModuleType, tmp_path: Path) -> None:
    """A file with no frontmatter is an error row, not a traceback; absent files are 'missing'."""
    skill, _cmd = _surfaces(cic, tmp_path)
    skill.write_text("no frontmatter here\n", encoding="utf-8")
    rows = dict((path, status) for status, path, _ in cic.sync_markers())
    assert rows[skill] == "error"
    assert cic.cmd_sync_skills(argparse.Namespace(check=True)) == cic.EX_USAGE
    missing = cic.claude_home() / "commands" / "codex-debate.md"
    assert rows[missing] == "missing"


def test_marker_falls_back_when_effort_unknown(cic: ModuleType) -> None:
    """An unknown slug has no catalog effort — the marker drops the effort= part, no '?'."""
    cic.save_config({"default": "mystery-model", "effort": None})
    assert cic.marker_for("debate") == "[codex mystery-model]"


def test_pick_refuses_without_a_tty(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cic.sys.stdin, "isatty", lambda: False, raising=False)
    ns = argparse.Namespace(for_command=None, refresh=False)
    assert cic.cmd_pick(ns) == cic.EX_USAGE


def test_pick_sets_the_chosen_model(cic: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cic.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "3")  # gpt-5.4 in _FAKE_CATALOG order
    assert cic.cmd_pick(argparse.Namespace(for_command="debate", refresh=False)) == cic.EX_OK
    assert cic.resolve_model("debate") == "gpt-5.4"
    # empty input keeps the current model
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert cic.cmd_pick(argparse.Namespace(for_command="debate", refresh=False)) == cic.EX_OK
    assert cic.resolve_model("debate") == "gpt-5.4"
    # out-of-range is a usage error, not a crash
    monkeypatch.setattr("builtins.input", lambda *_: "99")
    assert cic.cmd_pick(argparse.Namespace(for_command="debate", refresh=False)) == cic.EX_USAGE


# --------------------------- future-job launch wiring --------------------------- #
def test_job_launch_prefix() -> None:
    """A codex job prefixes its launch prompt with the slash command (claude job = no prefix)."""
    assert job_launch_prefix("claude") == ""
    assert job_launch_prefix("codex") == "/codex-implement-task-and-claude-review "
    assert job_launch_prefix("codex-write") == "/codex-implement-task-and-claude-review --write "


def test_create_draft_job_type_roundtrip_and_coercion(tmp_path: Path) -> None:
    """A draft stores its job_type, defaults to 'claude', and coerces unknown values."""
    store = Store(tmp_path / "s.db")
    assert store.create_draft("a", "/r", "aim", job_type="codex").job_type == "codex"
    assert store.create_draft("b", "/r", "aim").job_type == "claude"  # default
    assert store.create_draft("c", "/r", "aim", job_type="garbage").job_type == "claude"  # coerced
    got = store.get("a")
    assert got is not None and got.job_type == "codex"  # persisted across read


# --------------------------- refusal (credit) gate --------------------------- #
def _write_rollout(root: Path, name: str, ts: int, body: str) -> Path:
    """One rollout JSONL under a tmp CODEX_HOME, carrying a single rate_limits event."""
    sessions = root / "sessions" / "2026" / "08" / "29"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"rollout-{name}.jsonl"
    path.write_text(
        f'{{"type":"event_msg","timestamp":{ts},"payload":{{"type":"token_count",'
        f'"rate_limits":{body}}}}}\n',
        encoding="utf-8",
    )
    return path


_HEALTHY_BLOCK = (
    '{"limit_id":"codex","primary":{"used_percent":81.0,"window_minutes":300,'
    '"resets_at":2000007200},"secondary":null,'
    '"credits":{"has_credits":false,"unlimited":false,"balance":null},'
    '"rate_limit_reached_type":null}'
)
_REFUSED_BLOCK = (
    '{"limit_id":"premium","primary":null,"secondary":null,'
    '"credits":{"has_credits":false,"unlimited":false,"balance":null},'
    '"rate_limit_reached_type":"workspace_owner_credits_depleted"}'
)


def test_has_credits_false_alone_is_not_a_refusal(cic: ModuleType, tmp_path: Path) -> None:
    """A plan-covered seat reports has_credits:false while working perfectly."""
    _write_rollout(tmp_path / "codex", "healthy", 2000000000, _HEALTHY_BLOCK)
    assert cic.codex_refusal() is None


def test_refusal_detected_from_windowless_premium_block(cic: ModuleType, tmp_path: Path) -> None:
    """The refusal lives in exactly the block the window reader skips."""
    home = tmp_path / "codex"
    _write_rollout(home, "healthy", 2000000000, _HEALTHY_BLOCK)
    _write_rollout(home, "refused", 2000000100, _REFUSED_BLOCK)
    refusal = cic.codex_refusal()
    assert refusal is not None
    assert refusal.reached_type == "workspace_owner_credits_depleted"
    assert cic.refusal_label(refusal.reached_type) == (
        "included usage limit reached (no credit overflow)"
    )


def test_later_success_supersedes_an_earlier_refusal(cic: ModuleType, tmp_path: Path) -> None:
    """Only the NEWEST block decides — a topped-up seat must not stay marked blocked."""
    home = tmp_path / "codex"
    _write_rollout(home, "refused", 2000000000, _REFUSED_BLOCK)
    _write_rollout(home, "healthy", 2000000100, _HEALTHY_BLOCK)
    assert cic.codex_refusal() is None


def test_headroom_blocked_by_refusal_despite_window_headroom(
    cic: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ample window headroom must not produce ALLOWED while Codex refuses calls."""
    snapshot = cic._CodexRateSnapshot(
        captured_at=_HEADROOM_NOW,
        windows={
            300: cic._CodexRateWindow(
                used_percent=5.0, resets_at=_HEADROOM_NOW + 9000, window_minutes=300
            )
        },
    )
    monkeypatch.setattr(cic, "_codex_rate_snapshot", lambda: snapshot)
    _write_rollout(tmp_path / "codex", "refused", 2000000100, _REFUSED_BLOCK)
    decision = cic.codex_headroom(now=_HEADROOM_NOW)
    assert decision["state"] == "blocked"
    assert decision["offload_allowed"] is False
    assert decision["refused_by"] == "workspace_owner_credits_depleted"
    args = argparse.Namespace(json=False)
    assert cic.cmd_headroom(args) == 1


# ---- account pin (`codex-in-claude.py home`) -----------------------------------


def test_pinned_codex_home_honours_inclusive_expiry(tmp_path: Path) -> None:
    from datetime import date

    from command_center import codex_in_claude as cic

    cfg = {"codex_home": str(tmp_path), "codex_home_until": "2026-09-07"}
    assert cic.pinned_codex_home(cfg, today=date(2026, 9, 7)) == tmp_path  # inclusive
    assert cic.pinned_codex_home(cfg, today=date(2026, 9, 8)) is None  # lapsed
    assert cic.pinned_codex_home({"codex_home": str(tmp_path)}, today=date(2030, 1, 1)) == tmp_path
    assert cic.pinned_codex_home({"codex_home": None}) is None
    assert cic.pinned_codex_home({"codex_home": str(tmp_path), "codex_home_until": "soon"}) is None


def test_codex_home_resolution_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from command_center import codex_in_claude as cic

    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("CODEX_IN_CLAUDE_CONFIG", str(cfg_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cic._codex_home() == Path.home() / ".codex"  # pylint: disable=protected-access
    pinned = tmp_path / "private"
    cfg_path.write_text(json.dumps({"codex_home": str(pinned)}), encoding="utf-8")
    assert cic._codex_home() == pinned  # pylint: disable=protected-access
    assert cic.codex_exec_env({})["CODEX_HOME"] == str(pinned)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "explicit"))
    assert cic._codex_home() == tmp_path / "explicit"  # pylint: disable=protected-access
    assert cic.codex_exec_env()["CODEX_HOME"] == str(tmp_path / "explicit")


def test_cmd_home_sets_and_clears_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from command_center import codex_in_claude as cic

    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("CODEX_IN_CLAUDE_CONFIG", str(cfg_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "second"
    home.mkdir()
    ns = argparse.Namespace(path=str(home), until="2026-09-07", clear=False)
    assert cic.cmd_home(ns) == cic.EX_USAGE  # no auth.json yet
    (home / "auth.json").write_text("{}", encoding="utf-8")
    assert cic.cmd_home(ns) == cic.EX_OK
    out = capsys.readouterr().out
    assert str(home) in out and "until 2026-09-07" in out
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["codex_home"] == str(home) and saved["codex_home_until"] == "2026-09-07"
    bad = argparse.Namespace(path=str(home), until="next monday", clear=False)
    assert cic.cmd_home(bad) == cic.EX_USAGE
    assert cic.cmd_home(argparse.Namespace(path=None, until=None, clear=True)) == cic.EX_OK
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert saved["codex_home"] is None


def test_codex_home_fails_over_when_default_seat_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No pin + a hold on the team seat ⇒ delegation bills the private seat.

    The selector (quota.select_codex_account) is the ONE decider: holds/blocks exclude
    a seat before the pin or the default order get a say.
    """
    from command_center import config, quota, usage

    engine = _load_engine()
    private = tmp_path / "codex-private"
    private.mkdir()
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CODEX_IN_CLAUDE_CONFIG", str(tmp_path / "config.json"))  # no pin
    monkeypatch.setattr(config, "codex_home_private", lambda: private)
    monkeypatch.setattr(usage, "codex_account_email", lambda _h: "")
    monkeypatch.setattr(usage, "read_codex_usage", lambda _n=None, _h=None: None)
    quota.record_block(
        "codex",
        blocked_until=int(time.time()) + 3600,
        kind=quota.KIND_HOLD,
        reason="team seat reserved",
    )
    assert engine._codex_home() == private
