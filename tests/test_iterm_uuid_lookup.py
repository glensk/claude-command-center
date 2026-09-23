"""The tab-UUID → session lookup: SQL pre-filter, unchanged selection semantics (tp#70 S2).

:meth:`Store.session_for_tab_uuid` used to sweep the whole store (``list_sessions``) and
compare in Python — 44–128 ms on the real store, half of the ``ccc peek`` chord's latency
budget. It now narrows the rows with an SQL ``LIKE`` (:meth:`Store._tab_uuid_candidates`)
and keeps its own exact comparison and its live-then-recent ordering (commit 1d546b2).
These tests pin both: the candidates are a superset of the predicate, and the pick is the
one the sweep made.
"""

from __future__ import annotations

from pathlib import Path

from command_center import jump, peek
from command_center.models import Session
from command_center.store import Store


def _sweep_pick(store: Store, uuid: str) -> Session | None:
    """The pre-prefilter implementation, verbatim: full sweep, live then most recent."""
    want = uuid.split(":")[-1].strip().upper()
    matches = [
        s
        for s in store.list_sessions(include_archived=True)
        if s.iterm_session_id and s.iterm_session_id.split(":")[-1].strip().upper() == want
    ]
    if not matches:
        return None
    matches.sort(key=lambda s: (s.done, s.archived, -s.last_response_at, -s.updated_at))
    return matches[0]


def _seed(store: Store) -> None:
    store.ensure("old", cwd="/Users/x/repo")
    store.update_fields("old", iterm_session_id="w0t1p0:UUID-A", last_response_at=100)
    store.ensure("new", cwd="/Users/x/repo")
    store.update_fields("new", iterm_session_id="w9t9p9:uuid-a", last_response_at=200)
    store.ensure("newest-done", cwd="/Users/x/repo")
    store.update_fields(
        "newest-done", iterm_session_id="w0t1p0:UUID-A", last_response_at=900, done=True
    )
    store.ensure("other", cwd="/Users/x/o")
    store.update_fields("other", iterm_session_id="w0t1p0:UUID-B", last_response_at=999)
    store.ensure("untracked", cwd="/Users/x/u")  # no iterm_session_id at all (NULL)
    store.ensure("archived", cwd="/Users/x/a")
    store.update_fields(
        "archived", iterm_session_id="w0t1p0:UUID-C", last_response_at=50, archived=True
    )
    store.ensure("wild", cwd="/Users/x/w")
    store.update_fields("wild", iterm_session_id="w0t1p0:UUIDXA", last_response_at=5000)


def test_candidates_are_a_superset_of_the_exact_match(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store)
    ids = {s.session_id for s in store._tab_uuid_candidates("UUID-A")}  # pylint: disable=protected-access
    assert {"old", "new", "newest-done"} <= ids
    assert "other" not in ids and "untracked" not in ids
    assert store._tab_uuid_candidates("UUID-MISSING") == []  # pylint: disable=protected-access


def test_like_wildcards_in_the_uuid_are_escaped(tmp_path: Path) -> None:
    """``_`` must not match any character: ``UUID_A`` never pulls in ``UUIDXA``."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    ids = {s.session_id for s in store._tab_uuid_candidates("UUID_A")}  # pylint: disable=protected-access
    assert ids == set()
    assert store._tab_uuid_candidates("%") == []  # pylint: disable=protected-access


def test_pick_matches_the_old_sweep(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store)
    for uuid in ("UUID-A", "uuid-a", "w1t1p1:UUID-A", "UUID-B", "UUID-C", "UUID-Z", " "):
        got = store.session_for_tab_uuid(uuid)
        want = _sweep_pick(store, uuid)
        assert (got.session_id if got else None) == (want.session_id if want else None), uuid


def test_live_then_recent_ordering_is_unchanged(tmp_path: Path) -> None:
    """A newer DONE row never beats a live one; among live rows the newest wins."""
    store = Store(tmp_path / "state.db")
    _seed(store)
    got = store.session_for_tab_uuid("UUID-A")
    assert got is not None and got.session_id == "new"
    archived = store.session_for_tab_uuid("UUID-C")
    assert archived is not None and archived.session_id == "archived"


def test_peek_and_jump_both_route_through_it(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "state.db")
    _seed(store)
    session = peek._session_for_uuid(store, "UUID-A")  # pylint: disable=protected-access
    assert session is not None and session.session_id == "new"
    monkeypatch.setattr(jump, "Store", lambda: Store(tmp_path / "state.db"))
    assert jump._session_for_uuid("UUID-A") == "new"  # pylint: disable=protected-access
