#!/usr/bin/env python3
"""``ccc events --after CURSOR -j`` and ``ccc delivery -i ID -j`` — the problem stream.

``events`` first advances every open delivery (the transcript scanner is authoritative
for ``needs_input`` and completion, :mod:`command_center.bridge_delivery`), then returns
the event rows after *CURSOR*: ``{events: [{cursor, kind, session_id, at, detail}],
next_cursor}`` with ``kind`` one of ``stop_failure`` / ``needs_input`` /
``delivery_failed``. ``stop_failure`` rows come from the StopFailure hook
(:func:`record_stop_failure`), one per failed turn, whatever the error kind. Event
details carry ids, states and error kinds — never message text.
"""

from __future__ import annotations

if __name__ == "__main__" and not __package__:  # pragma: no cover - see _direct.py
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from command_center._direct import run as _direct_run

    _direct_run(__file__)

# pylint: disable=wrong-import-position,ungrouped-imports  # the direct-run shim comes first

import time
from typing import TYPE_CHECKING, Any

from .bridge_delivery import advance_all, public
from .bridge_json import BridgeError, iso

if TYPE_CHECKING:
    from .bridge_target import BridgeDeps

EVENTS_LIMIT = 200


def record_stop_failure(session_id: str, error: str, at_ms: int | None = None) -> None:
    """Persist one ``stop_failure`` event (called by the StopFailure hook; never raises)."""
    if not session_id:
        return
    try:
        from .store import Store

        with Store() as store:
            store.add_event(
                "stop_failure",
                session_id,
                at_ms if at_ms is not None else int(time.time() * 1000),
                {"error": error or "unknown"},
            )
    except Exception:  # pylint: disable=broad-exception-caught  # a hook must never fail
        pass


def run_events(deps: BridgeDeps, after: int, limit: int = EVENTS_LIMIT) -> dict[str, Any]:
    """Advance open deliveries, then the events after *after*."""
    if after < 0:
        raise BridgeError("invalid_cursor", "the cursor must be >= 0", exit_code=2)
    with deps.store() as store:
        advance_all(store, deps)
        rows = store.events_after(after, limit)
    events = [
        {
            "cursor": e.cursor,
            "kind": e.kind,
            "session_id": e.session_id,
            "at": iso(e.at),
            "detail": e.detail_obj(),
        }
        for e in rows
    ]
    return {"events": events, "next_cursor": rows[-1].cursor if rows else after}


def run_delivery(deps: BridgeDeps, delivery_id: str) -> dict[str, Any]:
    """Advance *delivery_id*'s session, then its current state."""
    with deps.store() as store:
        d = store.get_delivery(delivery_id)
        if d is None:
            raise BridgeError("unknown_delivery", f"no delivery {delivery_id}")
        advance_all(store, deps, [d.session_id])
        d = store.get_delivery(delivery_id)
    assert d is not None
    return public(d)
