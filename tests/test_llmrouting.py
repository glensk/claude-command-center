"""Unit tests for the `ccc llm-routing` overview.

The table's job is to answer "what is spending my Codex seat?" and "what actually runs?"
correctly, so these pin the resolution rules (auto-expansion, the custom-router escape
hatch, the live `ai routing` handshake, the enabled flags) plus the two properties the
rendering must never lose: ANSI-safe column alignment and never crashing `ccc -h`.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from command_center import llmrouting
from command_center.config import Config

# What `ai routing -p …` answers for a purpose: (first rung, cost, full ladder).
_COPILOT_ROUTE = (
    "copilot(claude-haiku-4.5)",
    "GitHub Copilot seat",
    "copilot(claude-haiku-4.5) → claude-code@work(claude-opus-4-6)",
)


def _cfg(**overrides: object) -> Config:
    cfg = Config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _row(cfg: Config, purpose: str, routes: dict | None = None) -> llmrouting.Row:
    return next(r for r in llmrouting.rows(cfg, routes) if r.purpose == purpose)


# --------------------------------------------------------------------------- #
# who bills the Codex seat
# --------------------------------------------------------------------------- #
def test_disabled_codex_route_is_not_counted_as_a_spender() -> None:
    cfg = _cfg(short_aim=False, llm_custom_command="ai.py prompt -R judge")
    routes = {"short-aim": ("codex(gpt-5.6)", "ChatGPT/Codex seat", "codex(gpt-5.6)")}
    assert llmrouting.codex_spenders(llmrouting.rows(cfg, routes)) == []
    assert _row(cfg, "short-aim", routes).enabled is False


def test_bills_codex_matches_either_spelling() -> None:
    """ccc says "Codex/ChatGPT seat", ai.py says "ChatGPT/Codex seat" — both count."""
    assert llmrouting.bills_codex(llmrouting.CODEX_COST)
    assert llmrouting.bills_codex("ChatGPT/Codex seat")
    assert not llmrouting.bills_codex(llmrouting.COPILOT_COST)


def test_a_live_route_that_lands_on_codex_is_still_flagged() -> None:
    routes = {"aim-met": ("codex(gpt-5.6)", "ChatGPT/Codex seat", "codex(gpt-5.6)")}
    cfg = _cfg(llm_custom_command="ai.py prompt -R judge", assess_aim_on_turn=True)
    assert llmrouting.codex_spenders(llmrouting.rows(cfg, routes))[0].purpose == "aim-met"


# --------------------------------------------------------------------------- #
# the llm_custom_command escape hatch
# --------------------------------------------------------------------------- #
def test_live_route_replaces_the_opaque_router_description() -> None:
    """The whole point: show the resolved rung + ladder, not "ask ai routing"."""
    cfg = _cfg(llm_custom_command="ai.py prompt -i -R judge", assess_aim_on_turn=True)
    row = _row(cfg, "aim-met", {"aim-met": _COPILOT_ROUTE})
    assert row.provider == "ai.py → copilot(claude-haiku-4.5)"
    assert row.cost == "GitHub Copilot seat"
    assert row.ladder == _COPILOT_ROUTE[2]


def test_ai_router_says_so_when_the_route_query_failed() -> None:
    cfg = _cfg(llm_custom_command="ai.py prompt -i -R judge")
    row = _row(cfg, "aim-met")
    assert row.cost == "ai.py (route query failed)"
    assert row.ladder == ""


def test_unrecognised_router_is_not_claimed_to_be_ai_py() -> None:
    cfg = _cfg(llm_custom_command="/opt/mine/router --go")
    assert _row(cfg, "aim-met").cost == "external router"


def test_unset_router_is_reported_as_a_failed_call() -> None:
    cfg = _cfg(llm_custom_command="")
    row = _row(cfg, "aim-met")
    assert row.cost == "nothing — call fails"
    assert row.provider == "(router unset)"


def test_aim_score_uses_the_same_router_as_every_other_purpose() -> None:
    cfg = _cfg(llm_custom_command="/opt/mine/router --go")
    assert _row(cfg, "aim-score").cost == "external router"


# --------------------------------------------------------------------------- #
# fetch_routes: the `ai routing -p` handshake
# --------------------------------------------------------------------------- #
def test_fetch_routes_parses_the_tsv_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="aim-met\tfirst\tcost\tlad\nbroken-line\n")

    monkeypatch.setattr(llmrouting.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(llmrouting.subprocess, "run", fake_run)
    out = llmrouting.fetch_routes("ai.py prompt -R judge", ["aim-met", "short-aim"])
    assert out == {"aim-met": ("first", "cost", "lad")}  # malformed lines dropped
    assert captured["cmd"] == ["/usr/bin/ai.py", "routing", "-p", "aim-met,short-aim"]


def test_fetch_routes_ignores_a_router_that_is_not_ai_py() -> None:
    assert llmrouting.fetch_routes("/opt/mine/router", ["aim-met"]) == {}
    assert llmrouting.fetch_routes("", ["aim-met"]) == {}
    assert llmrouting.fetch_routes("ai.py routing", []) == {}


def test_fetch_routes_survives_a_wedged_or_failing_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llmrouting.shutil, "which", lambda name: f"/usr/bin/{name}")

    def timeout(*_a: object, **_k: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd="ai", timeout=1)

    monkeypatch.setattr(llmrouting.subprocess, "run", timeout)
    assert llmrouting.fetch_routes("ai routing", ["aim-met"]) == {}

    monkeypatch.setattr(
        llmrouting.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=2, stdout=""),
    )
    assert llmrouting.fetch_routes("ai routing", ["aim-met"]) == {}


def test_fetch_routes_asks_for_colour_only_when_our_stdout_is_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, dict[str, str]] = {}

    def fake_run(_cmd: list[str], **kwargs: object) -> SimpleNamespace:
        seen["env"] = dict(kwargs["env"])  # type: ignore[call-overload]
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(llmrouting.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(llmrouting.subprocess, "run", fake_run)

    monkeypatch.setattr(llmrouting.sys.stdout, "isatty", lambda: True)
    llmrouting.fetch_routes("ai routing", ["aim-met"])
    assert seen["env"]["FORCE_COLOR"] == "1" and "NO_COLOR" not in seen["env"]

    monkeypatch.setattr(llmrouting.sys.stdout, "isatty", lambda: False)
    llmrouting.fetch_routes("ai routing", ["aim-met"])
    assert seen["env"]["NO_COLOR"] == "1" and "FORCE_COLOR" not in seen["env"]


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def test_coloured_cells_do_not_break_column_alignment() -> None:
    """A painted rung must not push its column — widths are measured after stripping ANSI."""
    painted = "\x1b[38;2;163;113;247mcopilot(haiku)\x1b[0m"
    assert llmrouting._visible_len(painted) == len("copilot(haiku)")
    lines = llmrouting._table(("a", "b"), [(painted, "x"), ("plain", "y")])
    widths = {len(llmrouting._ANSI_RE.sub("", line)) for line in lines}
    assert len(widths) == 1


def test_render_is_help_safe_when_config_loading_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ccc -h` embeds this — a broken config must degrade, never raise."""

    def boom() -> Config:
        raise ValueError("bad toml")

    monkeypatch.setattr(llmrouting, "_load_cfg", boom)
    out = llmrouting.render()
    assert "unavailable" in out and "ValueError" in out


def test_render_live_false_makes_no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("render(live=False) must not shell out")

    monkeypatch.setattr(llmrouting.subprocess, "run", forbidden)
    assert "ccc action" in llmrouting.render(_cfg(llm_custom_command="ai routing"), live=False)


def test_render_flags_the_codex_seat_only_when_router_says_it_bills_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(short_aim=True, llm_custom_command="ai.py prompt -R judge")
    monkeypatch.setattr(
        llmrouting,
        "fetch_routes",
        lambda *_a: {"short-aim": ("codex(gpt-5.6)", "ChatGPT/Codex seat", "codex")},
    )
    assert "external router currently sends" in llmrouting.render(cfg, live=True)
    assert "free for /codex-debate" in llmrouting.render(cfg, live=False)


def test_ladder_table_appears_only_when_a_route_resolved_one() -> None:
    cfg = _cfg(llm_custom_command="ai.py prompt -R judge")
    assert "Fallback ladder" not in llmrouting.render(cfg, live=False)
    rendered = llmrouting._ladder_block(llmrouting.rows(cfg, {"aim-met": _COPILOT_ROUTE}))
    assert rendered and any(_COPILOT_ROUTE[2] in line for line in rendered)


def test_every_action_names_a_config_key_to_change_it() -> None:
    assert all(r.switch.strip() for r in llmrouting.rows(_cfg()))


def test_purposes_constant_matches_the_rows_it_is_meant_to_resolve() -> None:
    """`PURPOSES` is what we ask `ai routing` about; a drift here silently loses a route.

    The rows carry their own purpose strings, so a renamed row that forgets the constant
    would still render — just permanently without a live route or ladder.
    """
    assert tuple(r.purpose for r in llmrouting.rows(_cfg())) == llmrouting.PURPOSES


# --------------------------------------------------------------------------- #
# the hot-path guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-h"], True),
        (["daemon", "--help"], True),
        (["statusline", "--session", "x"], False),
        ([], False),
    ],
)
def test_help_requested_gates_the_epilog(argv: list[str], expected: bool) -> None:
    """build_parser runs on every ccc call; the block may only be built for --help."""
    assert llmrouting.help_requested(argv) is expected
