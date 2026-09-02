"""Per-tab badge assignment: idempotent, unique, recyclable."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import colors, repos, tabsymbol, terminal
from command_center.models import Session


@pytest.fixture(autouse=True)
def _tmp_symbol_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCC_TAB_SYMBOL_DIR", str(tmp_path / "iterm-tab-symbol"))


def test_palette_is_unique_and_ample() -> None:
    assert len(set(tabsymbol.PALETTE)) == len(tabsymbol.PALETTE)  # no duplicate badges
    assert len(tabsymbol.PALETTE) >= 20  # enough distinct badges for many concurrent tabs
    assert len(tabsymbol.PALETTE) == len(tabsymbol.BADGES)  # PALETTE derived from BADGES


def test_palette_front_loads_shape_and_color_diversity() -> None:
    """The first badges claimed must be maximally distinct (the same-folder case)."""
    badges = tabsymbol.BADGES
    shapes = [shape for _e, shape, _c in badges]
    colors = [color for _e, _s, color in badges]

    # First 6 cover all 6 shapes before any shape repeats.
    assert len(set(shapes[:6])) == 6
    # First 8 are 8 distinct colors before any hue repeats.
    assert len(set(colors[:8])) == 8
    # Only one red and one warm-yellow among the first six (avoid look-alikes early).
    assert colors[:6].count("red") == 1
    assert colors[:6].count("warm") == 1
    # No two adjacent badges share a shape or a color family.
    assert all(shapes[i] != shapes[i + 1] for i in range(len(shapes) - 1))
    assert all(colors[i] != colors[i + 1] for i in range(len(colors) - 1))


def _assign_many(prefix: str, folder: str, count: int) -> list[str]:
    badges = [tabsymbol.assign(f"{prefix}{i}:{prefix}{i}", folder=folder) for i in range(count)]
    assert all(b is not None for b in badges)
    return [b for b in badges if b is not None]  # narrow for the type checker


def test_same_folder_tabs_get_distinct_shapes_and_colors() -> None:
    """Six tabs in one folder span all six shapes and six distinct colors."""
    badges = _assign_many("w0tX", "/repo/x", 6)
    assert len(set(badges)) == 6  # all distinct badges
    assert len({tabsymbol._SHAPE[b] for b in badges}) == 6  # all six shapes
    assert len({tabsymbol._COLOR[b] for b in badges}) == 6  # six distinct colors


def test_different_folders_optimize_independently() -> None:
    folder_a = _assign_many("w0tA", "/repo/a", 3)
    folder_b = _assign_many("w0tB", "/repo/b", 3)
    # Each folder's tabs are internally shape-distinct ...
    assert len({tabsymbol._SHAPE[x] for x in folder_a}) == 3
    assert len({tabsymbol._SHAPE[x] for x in folder_b}) == 3
    # ... and no badge is shared across all live tabs (global uniqueness).
    assert len(set(folder_a + folder_b)) == 6


def _seed_badge(iterm_session_id: str, badge: str, folder: str) -> None:
    """Pre-write a tab's badge + folder sidecar, as if it claimed it earlier."""
    directory = tabsymbol.cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    own = tabsymbol.slug(iterm_session_id)
    (directory / own).write_text(badge, encoding="utf-8")
    (directory / f"{own}.dir").write_text(folder, encoding="utf-8")


def test_new_tab_avoids_shapes_and_colors_used_by_other_folders() -> None:
    """A new tab derives its badge from *all* open symbols, not just same-folder ones.

    Seed five tabs (each in its own folder) that together wear triangle/circle/square
    and red/purple/blue/green — but no star/diamond/heart and no warm/brown/cyan/white.
    A brand-new tab in an empty folder must therefore SKIP the palette-earliest free
    badge (🟢 circle/green — both already worn elsewhere) and instead claim one whose
    shape AND color are unused across every open tab. The old folder-only algorithm
    would have handed it 🟢, reusing a live shape and color.
    """
    seeded = {
        "w0tS0:S0": ("🔺", "/repo/0"),  # triangle / red   (occupies the palette head)
        "w0tS1:S1": ("🟣", "/repo/1"),  # circle   / purple
        "w0tS2:S2": ("🔵", "/repo/2"),  # circle   / blue
        "w0tS3:S3": ("🟩", "/repo/3"),  # square   / green
        "w0tS4:S4": ("🟦", "/repo/4"),  # square   / blue
    }
    for iid, (badge, folder) in seeded.items():
        _seed_badge(iid, badge, folder)

    chosen = tabsymbol.assign("w0tNEW:NEW", folder="/repo/new")
    assert chosen is not None
    used_shapes = {tabsymbol._SHAPE[b] for b, _f in seeded.values()}
    used_colors = {tabsymbol._COLOR[b] for b, _f in seeded.values()}
    assert tabsymbol._SHAPE[chosen] not in used_shapes  # a shape no open tab wears
    assert tabsymbol._COLOR[chosen] not in used_colors  # a color no open tab wears


def test_assign_is_idempotent_per_tab() -> None:
    first = tabsymbol.assign("w0t1p0:UUID-A")
    second = tabsymbol.assign("w0t1p0:UUID-A")
    assert first in tabsymbol.PALETTE
    assert first == second


def test_distinct_tabs_get_distinct_badges() -> None:
    a = tabsymbol.assign("w0t0p0:A")
    b = tabsymbol.assign("w0t1p0:B")
    c = tabsymbol.assign("w0t2p0:C")
    assert len({a, b, c}) == 3


def test_first_claim_takes_palette_head() -> None:
    assert tabsymbol.assign("w0t0p0:A") == tabsymbol.PALETTE[0]
    assert tabsymbol.assign("w0t1p0:B") == tabsymbol.PALETTE[1]


def test_read_returns_assigned_and_none_for_unknown() -> None:
    assert tabsymbol.read("w0t0p0:A") is None
    assigned = tabsymbol.assign("w0t0p0:A")
    assert tabsymbol.read("w0t0p0:A") == assigned


def test_read_and_assign_handle_empty_id() -> None:
    assert tabsymbol.read(None) is None
    assert tabsymbol.read("") is None
    assert tabsymbol.assign(None) is None


def test_slug_matches_zsh_transform() -> None:
    assert tabsymbol.slug("w0t1p0:ABCD-EF") == "w0t1p0_ABCD-EF"


def test_cell_is_fixed_width_badge_or_blank() -> None:
    assert tabsymbol.cell("w0t0p0:A") == "   "  # unassigned -> blank pad (3 cols)
    badge = tabsymbol.assign("w0t0p0:A")
    assert tabsymbol.cell("w0t0p0:A") == f"{badge} "


def test_cell_show_false_hides_badge() -> None:
    """A non-live session (parked/finished) renders blank even with a badge on file."""
    badge = tabsymbol.assign("w0t0p0:B")
    assert tabsymbol.cell("w0t0p0:B") == f"{badge} "  # live -> badge shown
    assert tabsymbol.cell("w0t0p0:B", show=False) == "   "  # not live -> same-width blank


# --------------------------------------------------------------------------- #
# deterministic per-repo symbol + cell_for (live cache override else deterministic)
# --------------------------------------------------------------------------- #
def test_symbol_for_repo_is_deterministic_and_in_palette() -> None:
    a = tabsymbol.symbol_for_repo("/Users/x/sdsc/runai-cscs")
    assert a in tabsymbol.PALETTE
    assert a == tabsymbol.symbol_for_repo("/Users/x/sdsc/runai-cscs")  # stable across calls


def test_symbol_for_repo_distinct_repos_usually_differ() -> None:
    repos = [f"/Users/x/cat/repo-{i}" for i in range(8)]
    symbols = {tabsymbol.symbol_for_repo(r) for r in repos}
    assert len(symbols) >= 6  # 24-slot palette → 8 repos almost always spread out


def test_symbol_for_repo_empty_is_blank() -> None:
    assert tabsymbol.symbol_for_repo("") == ""
    assert tabsymbol.symbol_for_repo("   ") == ""


def test_cell_for_falls_back_to_deterministic_without_cache() -> None:
    cwd = "/Users/x/repo-a"
    expected = tabsymbol.symbol_for_repo(cwd)
    # No iTerm cache entry for this id → deterministic per-repo symbol is shown.
    assert tabsymbol.cell_for("w0t0p0:NOPE", cwd, live=True) == f"{expected} "
    # Parked (live=False) also shows the deterministic symbol (repo identity, not a tab).
    assert tabsymbol.cell_for("w0t0p0:NOPE", cwd, live=False) == f"{expected} "


def test_cell_for_live_cache_overrides_deterministic() -> None:
    cwd = "/Users/x/repo-a"
    badge = tabsymbol.assign("w0t0p0:UUID-A")  # claims the palette head 🔺
    assert badge is not None
    # A LIVE row shows the claimed iTerm badge (the author's real assignment wins).
    assert tabsymbol.cell_for("w0t0p0:UUID-A", cwd, live=True) == f"{badge} "
    # A non-live row ignores the (now meaningless) tab cache and uses the deterministic one.
    assert tabsymbol.cell_for("w0t0p0:UUID-A", cwd, live=False) == (
        f"{tabsymbol.symbol_for_repo(cwd)} "
    )


def test_cell_for_blank_when_no_cwd() -> None:
    assert tabsymbol.cell_for(None, "", live=False) == "   "  # nothing to key on → blank pad


def test_passing_the_resolved_root_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """*root* is a pure optimisation: the SAME badge, minus one config read per row.

    The TUI and ``ccc ls`` resolve the repo tree once per listing and hand it down; a
    one-shot caller (the shell hook) still omits it. Both spellings must key identically,
    inside the tree AND outside it, or a row and its tab would disagree.
    """
    monkeypatch.setenv("GIT_BASE", "/repo-root")
    root = repos.repo_root()
    for cwd in ("/repo-root/cat/repo-a", "/repo-root/cat/repo-a/tickets", "/elsewhere/scratch"):
        assert tabsymbol.symbol_for_repo(cwd, root) == tabsymbol.symbol_for_repo(cwd)
        assert tabsymbol.cell_for("w0t0p0:NOPE", cwd, live=True, root=root) == tabsymbol.cell_for(
            "w0t0p0:NOPE", cwd, live=True
        )
        assert tabsymbol.cell_for("w0t0p0:NOPE", cwd, live=False, root=root) == tabsymbol.cell_for(
            "w0t0p0:NOPE", cwd, live=False
        )


def test_palette_exhaustion_reclaims_oldest() -> None:
    ids = [f"w0t{i}p0:ID{i}" for i in range(len(tabsymbol.PALETTE))]
    claimed = [tabsymbol.assign(i) for i in ids]
    assert set(claimed) == set(tabsymbol.PALETTE)  # every palette slot used
    overflow = tabsymbol.assign("w0t99p0:OVERFLOW")
    assert overflow in tabsymbol.PALETTE  # still gets a badge, no crash


# --------------------------------------------------------------------------- #
# title-sync: assign-if-missing + marker-preserving push to the iTerm tab
# --------------------------------------------------------------------------- #
def test_title_core_matches_zsh_hook_format() -> None:
    """The core is ``"<badge> <leaf>"`` — the same shape the zsh chpwd hook sets."""
    _cat, leaf = colors.folder_split("/Users/x/repo")
    assert tabsymbol.title_core("🟧", "/Users/x/repo") == f"🟧 {leaf}"


def test_wait_marker_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_WAIT_MARKER", raising=False)
    assert tabsymbol._wait_marker(None) == "🔴 "  # default
    monkeypatch.setenv("CLAUDE_WAIT_MARKER", "⏳ ")
    assert tabsymbol._wait_marker(None) == "⏳ "  # env override
    assert tabsymbol._wait_marker("X ") == "X "  # explicit arg wins over env


def test_seed_title_assigns_badge_and_pushes_core(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_WAIT_MARKER", raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        terminal,
        "set_session_titles_preserving",
        lambda cores, marker="🔴 ": captured.update(cores=cores, marker=marker),
    )
    badge = tabsymbol.seed_title("w0t1p0:UUID", "/Users/x/repo")
    assert badge in tabsymbol.PALETTE
    assert tabsymbol.read("w0t1p0:UUID") == badge  # persisted for the TUI row to read
    _cat, leaf = colors.folder_split("/Users/x/repo")
    assert captured["cores"] == {"w0t1p0:UUID": f"{badge} {leaf}"}
    assert captured["marker"] == "🔴 "  # default marker preserved on the push


def test_seed_title_noop_without_iterm_id(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(terminal, "set_session_titles_preserving", _fail)
    assert tabsymbol.seed_title(None, "/Users/x/repo") is None
    assert tabsymbol.seed_title("", "/Users/x/repo") is None
    assert called is False  # nothing to key on -> no AppleScript push


class _FakeStore:
    """Minimal store stand-in exposing only what sync_live iterates."""

    def __init__(self, sessions: list[Session]) -> None:
        self._sessions = sessions

    def list_sessions(self) -> list[Session]:
        return self._sessions


def test_sync_live_badges_every_live_session_and_skips_done_and_idless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        terminal,
        "set_session_titles_preserving",
        lambda cores, marker="🔴 ": captured.update(cores=cores, marker=marker),
    )
    store = _FakeStore(
        [
            Session("a", cwd="/Users/x/ra", iterm_session_id="w0t1p0:AA"),
            Session("b", cwd="/Users/x/rb", iterm_session_id="w0t2p0:BB"),
            Session("c", cwd="/Users/x/rc", iterm_session_id="w0t3p0:CC", done=True),  # skipped
            Session("d", cwd="/Users/x/rd"),  # no iterm id -> skipped
        ]
    )
    badged = tabsymbol.sync_live(store)  # type: ignore[arg-type]
    assert set(badged) == {"a", "b"}  # only live sessions with a tab id
    cores = captured["cores"]
    assert isinstance(cores, dict)
    assert set(cores) == {"w0t1p0:AA", "w0t2p0:BB"}
    # Each session now has a persisted badge that its core title is built from.
    for sid, iid in (("a", "w0t1p0:AA"), ("b", "w0t2p0:BB")):
        badge = tabsymbol.read(iid)
        assert badge in tabsymbol.PALETTE
        _cat, leaf = colors.folder_split(f"/Users/x/r{sid}")
        assert cores[iid] == f"{badge} {leaf}"


def test_sync_live_no_push_when_nothing_to_badge(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(terminal, "set_session_titles_preserving", _fail)
    store = _FakeStore([Session("d", cwd="/Users/x/rd")])  # no tab ids at all
    assert tabsymbol.sync_live(store) == []  # type: ignore[arg-type]
    assert called is False
