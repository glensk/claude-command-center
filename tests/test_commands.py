"""The TUI command registry is the single source of truth for keys/commands.

These tests lock the wiring so a command added to the registry surfaces in the
bindings, footer, headers and help — and can never reference a missing action.
"""

from __future__ import annotations

from command_center.views import commands


def test_every_action_resolves_to_an_app_method() -> None:
    """Each command's action must resolve on the App (no dangling wiring).

    Textual resolves a binding action to ``action_<name>`` or a bare method
    ``<name>`` (e.g. ``refresh_data``); a typo'd / missing action fails here.
    """
    from command_center.views.tui import CommandCenterApp

    def resolves(name: str) -> bool:
        return callable(getattr(CommandCenterApp, f"action_{name}", None)) or callable(
            getattr(CommandCenterApp, name, None)
        )

    for cmd in commands.COMMANDS:
        if cmd.action is None:
            continue  # nav-only doc entry (↑/↓, ←/→)
        assert resolves(cmd.action), cmd.action


def test_binding_specs_cover_bindable_commands() -> None:
    """Every command with a `bind` (plus its aliases) appears in binding_specs once."""
    specs = commands.binding_specs()
    keys = [key for key, *_ in specs]
    for cmd in commands.COMMANDS:
        if cmd.bind is None:
            continue
        assert cmd.bind in keys
        for alias in cmd.aliases:
            assert alias in keys
    # The `tf` chord and nav-only commands are deliberately NOT plain bindings.
    assert "t" not in keys and "f" not in keys
    assert all(action for _, action, *_ in specs)  # never None


def test_footer_order_is_stable_and_unique() -> None:
    """The footer renders footer_pos commands in order, with no duplicate slots."""
    footer = commands.footer_commands()
    positions = [c.footer_pos for c in footer]
    assert all(p is not None for p in positions)
    ints = [p for p in positions if p is not None]
    assert ints == sorted(ints)
    assert len(set(ints)) == len(ints)
    assert [c.word for c in footer][:3] == ["/aim", "/next-step", "/done"]
    assert footer[-1].word == "quit"
    # `close` sits immediately after `/done`; its footer label is the ☾lose moon mnemonic.
    labels = [c.footer_word or c.word for c in footer]
    assert labels[labels.index("/done") + 1] == "☾lose"


def test_toggle_leader_is_in_footer_as_toggle() -> None:
    """The `t…` toggles surface in the footer once, as the `t` leader ("toggle", t gilded)."""
    footer = commands.footer_commands()
    toggle = next(c for c in footer if c.footer_word == "toggle")
    assert toggle.action == "toggle_finished"
    assert toggle.footer_key == "t"  # only the leader `t` is the footer mnemonic
    # The leader's chord menu lists every `t…` chord: the per-session account switches
    # tp/tw (first, in registry order), then the view toggles td/tf/ti, the five
    # usage-card render gates t1…t5, and the two nixos-overseer cards to/ta.
    menu = commands.chords_for_leader("t")
    assert [c.key for c in menu] == [
        "tp",
        "tw",
        "td",
        "tf",
        "ti",
        "t1",
        "t2",
        "t3",
        "t4",
        "t5",
        "to",
        "ta",
    ]
    assert {c.word for c in menu} == {
        "private",
        "work",
        "done",
        "future",
        "idle-alerts",
        "card-private",
        "card-work",
        "card-codex",
        "card-copilot",
        "card-codex-private",
        "card-nix-supervised",
        "card-nix-tier-a",
    }
    # Exactly one `t…` toggle carries a footer_pos (the leader is shown once, not many times).
    assert len([c for c in menu if c.footer_pos is not None]) == 1


def test_refresh_stays_bound_and_in_help_but_not_footer() -> None:
    """Refresh is a deliberate no-footer command: key/help stay, footer hint drops it."""
    refresh = commands.by_action("refresh_data")
    assert refresh.key == "R"
    assert refresh.bind == "R"
    assert refresh.word == "Refresh-now"
    assert refresh.footer_pos is None
    assert refresh in dict(commands.sections())[commands.GLOBAL]
    assert refresh not in commands.footer_commands()


def test_column_keys_match_headers() -> None:
    """Editable table columns expose their gold mnemonic; others do not."""
    assert commands.column_key("/aim") == "a"
    assert commands.column_key("/next-step") == "n"
    # /block & /deadline are no longer table columns (edit them via the b / D keys).
    assert commands.column_key("/block") is None
    assert commands.column_key("/deadline") is None
    assert commands.column_key("folder") is None


def test_toggle_chords_are_td_done_and_tf_future() -> None:
    assert commands.by_action("toggle_finished").chord == ("t", "d")
    assert commands.by_action("toggle_finished").key == "td"
    assert commands.by_action("toggle_future").chord == ("t", "f")
    assert commands.by_action("toggle_future").key == "tf"


def test_toggle_idle_chord_is_ti() -> None:
    """`ti` mutes/unmutes idle popups; it is a pure-menu chord (no footer slot)."""
    cmd = commands.by_action("toggle_idle")
    assert cmd.chord == ("t", "i")
    assert cmd.key == "ti"
    assert cmd.section == commands.GLOBAL
    assert cmd.footer_pos is None  # shown only via the `t` leader menu, like tf


def test_card_toggle_chords_are_t1_to_t5() -> None:
    """`t1`…`t5` toggle the five usage cards; each is a pure-menu chord (no footer slot)."""
    expected = {
        "1": "toggle_card_private",
        "2": "toggle_card_work",
        "3": "toggle_card_codex",
        "4": "toggle_card_copilot",
        "5": "toggle_card_codex_private",
    }
    for digit, action in expected.items():
        cmd = commands.by_action(action)
        assert cmd.chord == ("t", digit)
        assert cmd.key == f"t{digit}"
        assert cmd.key == "".join(cmd.chord)  # registry invariant
        assert cmd.section == commands.GLOBAL
        assert cmd.footer_pos is None  # shown only via the `t` leader menu
        assert cmd.explanation  # a real explanation, not blank


def test_nixos_overseer_card_chords_are_to_and_ta() -> None:
    """`to`/`ta` toggle the two nixos-overseer cards; each is a pure-menu chord (no footer)."""
    expected = {
        "o": "toggle_card_nixos_overseer_supervised",
        "a": "toggle_card_nixos_overseer_tier_a",
    }
    for follower, action in expected.items():
        cmd = commands.by_action(action)
        assert cmd.chord == ("t", follower)
        assert cmd.key == f"t{follower}"
        assert cmd.key == "".join(cmd.chord)  # registry invariant
        assert cmd.section == commands.GLOBAL
        assert cmd.footer_pos is None  # shown only via the `t` leader menu
        assert cmd.bind is None  # a chord, never a plain binding
        assert cmd.explanation  # a real explanation, not blank
    # Chord followers are namespaced under the `t` leader (plain binds like `d`/`f`
    # coexist with td/tf), so the only real collision class is another t-chord.
    chord_followers = {
        c.chord[1]
        for c in commands.COMMANDS
        if c.chord and c.chord[0] == "t" and c.action not in expected.values()
    }
    assert chord_followers.isdisjoint({"o", "a"})


def test_registry_invariants_hold_with_new_chords() -> None:
    """The chord-key and footer-uniqueness invariants still hold after adding t1…t4."""
    for cmd in commands.COMMANDS:
        if cmd.chord is not None:
            assert cmd.key == "".join(cmd.chord)
    positions = [c.footer_pos for c in commands.COMMANDS if c.footer_pos is not None]
    assert len(positions) == len(set(positions))  # unique footer_pos
    # The new digit chords collide with no existing binding, alias, or chord.
    binds = {c.bind for c in commands.COMMANDS if c.bind}
    aliases = {a for c in commands.COMMANDS for a in c.aliases}
    assert binds.isdisjoint({"1", "2", "3", "4"})
    assert aliases.isdisjoint({"1", "2", "3", "4"})


def test_account_switch_chords_are_tp_and_tw() -> None:
    """`tp`/`tw` set the highlighted row's Claude account; per-session, pure-menu chords."""
    priv = commands.by_action("account_private")
    work = commands.by_action("account_work")
    assert priv.chord == ("t", "p") and priv.key == "tp"
    assert work.chord == ("t", "w") and work.key == "tw"
    for cmd in (priv, work):
        assert cmd.chord is not None
        assert cmd.key == "".join(cmd.chord)  # registry invariant
        assert cmd.section == commands.PER_SESSION  # acts on the highlighted session
        assert cmd.footer_pos is None  # shown only via the `t` leader menu, like ti/t1…t4
        assert cmd.explanation  # a real explanation, not blank
        assert cmd.bind is None  # a chord, never a plain binding
    # They collide with no plain binding or alias.
    binds = {c.bind for c in commands.COMMANDS if c.bind}
    aliases = {a for c in commands.COMMANDS for a in c.aliases}
    assert binds.isdisjoint({"tp", "tw"}) and aliases.isdisjoint({"tp", "tw"})


def test_oo_chord_is_open_obsidian() -> None:
    cmd = commands.by_action("open_obsidian")
    assert cmd.chord == ("o", "o")
    assert cmd.key == "oo"
    assert cmd.section == commands.PER_SESSION
    assert cmd.footer_pos is not None  # visible in the footer hint line


def test_undo_is_bound_and_in_footer() -> None:
    """`u` is the GLOBAL undo binding, sits right after ☾lose in the footer, no collision."""
    cmd = commands.by_action("undo")
    assert cmd.key == "u"
    assert cmd.bind == "u"
    assert cmd.section == commands.GLOBAL
    assert cmd.footer_pos is not None
    assert cmd.explanation.strip()  # a real explanation, not blank
    # Footer order: `undo` renders right after the ☾lose label.
    footer = commands.footer_commands()
    labels = [c.footer_word or c.word for c in footer]
    assert labels[labels.index("☾lose") + 1] == "undo"
    # `u` collides with no OTHER plain binding, alias, or chord follower.
    other_binds = {c.bind for c in commands.COMMANDS if c.bind and c.action != "undo"}
    aliases = {a for c in commands.COMMANDS for a in c.aliases}
    chord_keys = {ch for c in commands.COMMANDS if c.chord for ch in c.chord}
    assert other_binds.isdisjoint({"u"})
    assert aliases.isdisjoint({"u"})
    assert chord_keys.isdisjoint({"u"})


def test_edit_command_replaces_field_keys_in_footer() -> None:
    """`e` (edit) is the footer entry; /Deadline, /block, important & subgoal are off it.

    Those four keep their direct bindings (D / b / ! / space still work) — only their
    standalone footer slot is gone, subsumed by the `e` edit menu.
    """
    footer_words = {c.footer_word or c.word for c in commands.footer_commands()}
    assert "edit" in footer_words
    for dropped in ("/Deadline", "/block", "important", "subgoal"):
        assert dropped not in footer_words
    edit = commands.by_action("edit_session")
    assert edit.key == "e" and edit.bind == "e"
    for action in ("edit_deadline", "edit_blocked", "cycle_importance", "toggle_subgoal"):
        cmd = commands.by_action(action)
        assert cmd.bind is not None  # still directly bound
        assert cmd.footer_pos is None  # but no longer in the footer line
