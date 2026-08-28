"""``ccc demo`` — a self-contained fake-data command center.

Seeds ~10 deterministic sessions into a **throwaway demo home** (never the real
``CLAUDE_HOME``) so a newcomer can see the product without wiring anything, and so
screenshots are reproducible. The demo home is fully self-contained:

* ``<home>/command-center/state.db``   — the seeded store
* ``<home>/command-center/config.toml`` — repo tree + inert (no LLM/network/vault) config
* ``<home>/sessions/<key>.json``        — fake live registry (working / waiting / halted)
* ``<home>/projects/<cwd>/<id>.jsonl``  — fake transcripts (model, version, halt marker)
* ``<home>/settings.json``              — ``effortLevel`` for the fill-once effort capture
* ``<home>/vault/``                     — vault roots pointed here (mirrors stay off anyway)

Real-``CLAUDE_HOME`` safety: :func:`run` points ``CLAUDE_HOME`` at the demo dir before
anything reads config / the store / the adapter (all resolve the env at call time), and
:func:`seed` only ever writes under the *home* it is given. Live-process discovery reads
``$CLAUDE_HOME/sessions`` — in the demo home that holds only the fake registry files this
module writes, so no real session can leak into the demo. The fake "live" sessions use the
current process's own pid, which is always alive while the demo (or a test) runs, so
``reconcile`` derives their busy / waiting / halted status through the real pipeline.
"""

from __future__ import annotations

# Lazy imports of the heavy view/terminal layers (import-outside-toplevel) keep the demo
# seed cheap and avoid a cli → demo → views import cycle.
# pylint: disable=import-outside-toplevel
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from . import config
from .models import Status, aim_column_first, now_ms
from .store import Store

_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000
_MIN_MS = 60_000

# Stable namespace so every demo run mints the SAME session ids (reproducible screenshots).
_NS = uuid5(NAMESPACE_URL, "https://github.com/glensk/ccc#demo")

# Full ``claude --model`` ids to embed in fake transcripts (reverse-mapped to short labels
# by the observed-model reader). Kept local so the demo does not depend on model internals.
_MODEL_IDS = {
    "fable-5": "claude-fable-5",
    "opus-4.8": "claude-opus-4-8",
    "opus-4.8-1m": "claude-opus-4-8[1m]",
    "sonnet-5": "claude-sonnet-5",
    "haiku-4.5": "claude-haiku-4-5-20251001",
}
_VERSION = "2.1.193"


def sid_for(slug: str) -> str:
    """Deterministic session UUID for a demo *slug* (stable across runs)."""
    return str(uuid5(_NS, slug))


@dataclass
class DemoSession:
    """One seeded demo session (a real store row, optionally faked live)."""

    slug: str
    category: str
    repo: str
    aim: str
    short_aim: str
    aim_score: int
    subgoals: list[str] = field(default_factory=list)
    checked: int = 0
    live_status: str = ""  # "" = not live (parked); "busy" | "waiting" for a live row
    halted: bool = False
    done: bool = False
    aim_met: bool = False
    importance: int = 0
    deadline_days: int | None = None  # relative to today; negative = overdue
    blocked_on: str = ""
    next_step: str = ""
    manual_progress: int | None = None
    drift: str = ""  # "" | "low" | "medium" | "high"
    model: str = "fable-5"
    effort: str = "xhigh"
    age_min: int = 0  # minutes since last response (parked/done rows)


@dataclass
class DemoDraft:
    """One seeded future-job draft (FUTURE, or SCHEDULED when ``start_date`` is set)."""

    slug: str
    category: str
    repo: str
    aim: str
    short_aim: str
    aim_score: int
    job_type: str = "claude"
    overseer: str = "fable-5"
    executor: str = "fable-5"
    start_when: str = ""
    start_date: str = ""  # ISO YYYY-MM-DD; set → SCHEDULED bucket
    deadline_days: int | None = None


def _sessions() -> list[DemoSession]:
    """The deterministic active/parked/done demo sessions (order is cosmetic)."""
    return [
        DemoSession(
            slug="api-gateway",
            category="work",
            repo="api-gateway",
            aim="Ship rate-limit middleware: 429 above 100 req/min per key, covered by tests",
            short_aim="rate-limit middleware + tests",
            aim_score=84,
            subgoals=[
                "add token-bucket limiter keyed by API key",
                "return 429 with Retry-After header",
                "wire middleware into the request pipeline",
                "unit test: 101st request in a minute is rejected",
                "load test holds at 100 req/min",
            ],
            checked=3,
            live_status="busy",
            importance=2,
            next_step="finish the 429 Retry-After header path",
            model="fable-5",
            effort="xhigh",
        ),
        DemoSession(
            slug="billing-service",
            category="work",
            repo="billing-service",
            aim="Make Stripe webhook handling idempotent so no event double-charges a customer",
            short_aim="idempotent Stripe webhooks",
            aim_score=88,
            subgoals=[
                "store processed event ids",
                "skip an event id already seen",
                "wrap charge + record in one transaction",
                "handle out-of-order delivery",
                "integration test proves no double-charge on replay",
                "alert on unknown event types",
            ],
            checked=4,
            live_status="waiting",
            importance=3,
            deadline_days=5,
            model="opus-4.8",
            effort="xhigh",
        ),
        DemoSession(
            slug="data-pipeline",
            category="work",
            repo="data-pipeline",
            aim="Backfill 2025 events into the warehouse; final row count matches the source",
            short_aim="backfill 2025 events",
            aim_score=76,
            subgoals=[
                "page the source export API",
                "map legacy schema to the new columns",
                "idempotent upsert by event id",
                "checkpoint + resume on failure",
                "row count matches source",
                "spot-check 20 sampled rows",
                "document the rerun procedure",
            ],
            checked=2,
            live_status="busy",
            halted=True,
            importance=1,
            model="sonnet-5",
            effort="xhigh",
        ),
        DemoSession(
            slug="garden-irrigation",
            category="home",
            repo="garden-irrigation",
            aim="No water hammer after watering: valves close in a staged sequence, tested by ear",
            short_aim="stage valve close, no hammer",
            aim_score=71,
            subgoals=[
                "find each zone shutoff",
                "close valves in sequence, not all at once",
                "add a 2s delay between zones",
                "test staged close on zone 3",
            ],
            checked=3,
            blocked_on="check the outdoor tap at home",
            next_step="test the staged close on zone 3",
            importance=0,
            model="sonnet-5",
            effort="high",
            age_min=95,
        ),
        DemoSession(
            slug="tax-2025",
            category="home",
            repo="tax-2025",
            aim="File the 2025 return: every deduction entered and the e-file is accepted",
            short_aim="file 2025 return, e-file ok",
            aim_score=58,
            subgoals=[
                "gather all receipts",
                "enter income from every source",
                "enter deductions",
                "review with the checklist",
                "e-file and confirm acceptance",
            ],
            checked=1,
            deadline_days=-1,  # overdue → red badge
            importance=2,
            model="haiku-4.5",
            effort="medium",
            age_min=3 * 24 * 60,
        ),
        DemoSession(
            slug="backup-script",
            category="home",
            repo="backup-script",
            aim="Nightly backup of the photo library to the NAS, verified restorable weekly",
            short_aim="nightly verified NAS backup",
            aim_score=66,
            subgoals=[
                "rsync photos to the NAS nightly",
                "verify a sample restore weekly",
                "alert on a failed run",
            ],
            checked=2,
            manual_progress=80,  # manual override — bar reads 80% regardless of ticks
            importance=3,
            model="fable-5",
            effort="high",
            age_min=6 * 60,
        ),
        DemoSession(
            slug="recipe-site",
            category="home",
            repo="recipe-site",
            aim="Deploy the static recipe site; the homepage loads in under one second",
            short_aim="deploy recipe site <1s",
            aim_score=90,
            subgoals=[
                "build the static site",
                "compress and inline critical CSS",
                "deploy to the CDN",
                "Lighthouse LCP under 1s",
                "custom domain resolves over HTTPS",
            ],
            checked=5,
            done=True,
            importance=1,
            model="fable-5",
            effort="xhigh",
            age_min=8 * 60,
        ),
        DemoSession(
            slug="textual-ui",
            category="oss",
            repo="textual-ui",
            aim="Add a dark-mode toggle to the settings screen that persists across restarts",
            short_aim="dark-mode toggle, persisted",
            aim_score=85,
            subgoals=[
                "add the toggle widget to settings",
                "persist the choice to disk",
                "apply the theme on startup",
                "snapshot test both themes",
            ],
            checked=3,
            aim_met=True,  # impartial checker thinks it is done → red DONE stamped in the bar
            importance=0,
            model="opus-4.8-1m",
            effort="xhigh",
            age_min=40,
        ),
        DemoSession(
            slug="cli-tool",
            category="oss",
            repo="cli-tool",
            aim="Add --json output to every subcommand with a documented, stable schema",
            short_aim="--json output everywhere",
            aim_score=70,
            subgoals=[
                "add a --json flag to the root parser",
                "emit machine-readable output per subcommand",
                "rewrite the whole config format",  # scope creep → drift flag
                "document the JSON schema",
            ],
            checked=2,
            drift="medium",
            importance=0,
            model="fable-5",
            effort="high",
            age_min=2 * 60,
        ),
    ]


def _drafts() -> list[DemoDraft]:
    """The deterministic FUTURE + SCHEDULED demo drafts."""
    scheduled = (date.today() + timedelta(days=14)).isoformat()
    return [
        DemoDraft(
            slug="new-linter",
            category="oss",
            repo="new-linter",
            aim="Write a lint rule that flags a TODO comment with no linked issue",
            short_aim="lint TODO without issue link",
            aim_score=79,
            job_type="codex",  # → the OAI badge in the ver column, [codex] in `ccc jobs`
            overseer="fable-5",
            executor="opus-4.8",
            start_when="next sprint",
        ),
        DemoDraft(
            slug="q3-review",
            category="work",
            repo="q3-review",
            aim="Prepare the Q3 architecture-review deck: one slide per subsystem with risks",
            short_aim="Q3 architecture-review deck",
            aim_score=64,
            overseer="opus-4.8",
            executor="opus-4.8",
            start_date=scheduled,  # → SCHEDULED bucket at the very bottom
        ),
    ]


def _repo_root(home: Path) -> Path:
    return home / "repos"


def _cwd_for(home: Path, category: str, repo: str) -> str:
    return str(_repo_root(home) / category / repo)


def _write_config(home: Path) -> None:
    """Write the demo ``config.toml``: category tree + everything pointed inside the demo home.

    Every token/LLM/network/vault feature stays at its inert default (off), so a demo run
    spends nothing and touches nothing outside *home* — the vault roots are pointed here too
    as belt-and-suspenders even though the mirror flags never fire.
    """
    root = _repo_root(home)
    vault = home / "vault"
    lines = [
        f'repo_root = "{root}"',
        'folder_order = ["work", "home", "oss"]',
        f'vault_root = "{vault}"',
        f'future_dir = "{vault / "future"}"',
        f'delete_dir = "{vault / "delete"}"',
        f'future_pad = "{vault / "new-prompt.md"}"',
        f'running_dir = "{vault / "running"}"',
        f'done_dir = "{vault / "done"}"',
        f'sessions_dir = "{vault / "sessions"}"',
        "done_max_age_days = 0",
        'tab_title = "ccc demo"',
        'tab_color = ""',
    ]
    cc = home / "command-center"
    cc.mkdir(parents=True, exist_ok=True)
    (cc / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # A settings.json so the fill-once effort capture has a global default to read for live rows.
    (home / "settings.json").write_text(json.dumps({"effortLevel": "xhigh"}), encoding="utf-8")


def _write_usage(home: Path) -> None:
    """Seed the Claude Code + OpenAI Codex usage cards with deterministic live windows.

    Writes ``usage.json`` (the captured Claude account snapshot the card reads) and a Codex
    session rollout under the demo ``CODEX_HOME`` so the two subscription cards render with
    real-looking 5-hour + weekly bars instead of leaking the developer's actual usage.
    """
    now_s = int(time.time())
    claude = {
        "captured_at": now_s,
        "five_hour": {"used_percentage": 33.0, "resets_at": now_s + 2 * 3600 - 3 * 60},
        "seven_day": {"used_percentage": 20.0, "resets_at": now_s + 3 * 86400 + 11 * 3600},
    }
    cc = home / "command-center"
    cc.mkdir(parents=True, exist_ok=True)
    (cc / "usage.json").write_text(json.dumps(claude), encoding="utf-8")

    codex_block = {
        "type": "token_count",
        "payload": {
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 14.0, "resets_at": now_s + 2 * 3600 + 11 * 60},
                "secondary": {"used_percent": 10.0, "resets_at": now_s + 6 * 86400 + 5 * 60},
            }
        },
    }
    rollout = home / "codex" / "sessions" / "2026" / "07" / "08"
    rollout.mkdir(parents=True, exist_ok=True)
    (rollout / "rollout-demo.jsonl").write_text(json.dumps(codex_block) + "\n", encoding="utf-8")


def _write_live(home: Path, session_id: str, cwd: str, spec: DemoSession) -> None:
    """Fake a live session: a registry entry for our own (alive) pid + a small transcript.

    The registry file makes ``adapter.discover`` return the session as live; the transcript
    gives it an observed model + Claude Code version, and — for a halted row — the
    rate-limit marker ``is_halted`` keys on. Uses ``os.getpid()`` so the pid is alive for as
    long as the demo (or a test) runs; the process's own subtree carries none of the
    background-task / subagent signatures, so no spurious SNOOZED.
    """
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    ts = now_ms()
    registry = {
        "sessionId": session_id,
        "pid": os.getpid(),
        "cwd": cwd,
        "kind": "interactive",
        "entrypoint": "cli",
        "status": spec.live_status or "idle",
        "agent": "claude",
        "startedAt": ts - 20 * _MIN_MS,
        "updatedAt": ts,
        "statusUpdatedAt": ts,
    }
    (sessions / f"demo-{session_id[:8]}.json").write_text(json.dumps(registry), encoding="utf-8")

    model_id = _MODEL_IDS.get(spec.model, _MODEL_IDS["fable-5"])
    records: list[dict] = [
        {
            "type": "user",
            "version": _VERSION,
            "message": {"role": "user", "content": spec.aim},
        },
        {
            "type": "assistant",
            "version": _VERSION,
            "isApiErrorMessage": False,
            "message": {
                "role": "assistant",
                "model": model_id,
                "content": [{"type": "text", "text": f"Working on: {spec.short_aim}."}],
            },
        },
    ]
    if spec.halted:
        records.append(
            {
                "type": "assistant",
                "version": _VERSION,
                "isApiErrorMessage": True,
                "message": {
                    "role": "assistant",
                    "model": model_id,
                    "content": [
                        {
                            "type": "text",
                            "text": "You've hit your usage limit · resets in 2h 11m",
                        }
                    ],
                },
            }
        )
    proj = home / "projects" / cwd.replace("/", "-")
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _seed_session(store: Store, home: Path, spec: DemoSession) -> str:
    """Insert one active/parked/done demo session and return its id."""
    session_id = sid_for(spec.slug)
    cwd = _cwd_for(home, spec.category, spec.repo)
    now = now_ms()
    store.ensure(session_id, cwd=cwd)
    store.set_aim(session_id, spec.aim)  # records AIM history + a lexical score
    store.set_short_aim(session_id, spec.short_aim)
    fields: dict[str, object] = {
        "aim_score": spec.aim_score,
        "importance": spec.importance,
        "model": spec.model,
        "effort": spec.effort,
        "version": _VERSION,
        "prompt_count": max(1, spec.checked + 2),
        "last_response_at": now - spec.age_min * _MIN_MS,
        "created_at": now - (spec.age_min + 30) * _MIN_MS,
    }
    if spec.next_step:
        fields["next_step"] = spec.next_step
        fields["next_step_source"] = "user"
    if spec.blocked_on:
        fields["blocked_on"] = spec.blocked_on
    if spec.deadline_days is not None:
        fields["deadline"] = (date.today() + timedelta(days=spec.deadline_days)).isoformat()
    if spec.manual_progress is not None:
        fields["manual_progress"] = spec.manual_progress
    if spec.done:
        fields["done"] = True
        fields["done_at"] = now - spec.age_min * _MIN_MS
        fields["status"] = Status.DONE.value
    else:
        fields["status"] = Status.PARKED.value
    store.update_fields(session_id, **fields)

    if spec.subgoals:
        store.set_subgoals(session_id, spec.subgoals, source="auto", model="claude-haiku-4-5")
        subs = store.list_subgoals(session_id)
        for sub in subs[: spec.checked]:
            store.set_subgoal_checked(sub.id, True)
    if spec.done:
        store.check_all_subgoals(session_id)  # a done row reads 100%
    if spec.aim_met:
        store.set_aim_met(session_id, True, "all sub-goals met and verified", now)
    if spec.drift:
        store.set_drift(
            session_id, spec.drift, "sub-goal 'rewrite the whole config format' widens scope"
        )

    if spec.live_status:
        _write_live(home, session_id, cwd, spec)
    return session_id


def _seed_draft(store: Store, home: Path, spec: DemoDraft) -> str:
    """Insert one future-job draft (FUTURE, or SCHEDULED when a start_date is set)."""
    session_id = sid_for(f"draft/{spec.slug}")
    cwd = _cwd_for(home, spec.category, spec.repo)
    deadline = None
    if spec.deadline_days is not None:
        deadline = (date.today() + timedelta(days=spec.deadline_days)).isoformat()
    store.create_draft(
        session_id,
        cwd,
        spec.aim,
        prompt=None,
        deadline=deadline,
        start_when=spec.start_when or None,
        start_date=spec.start_date or None,
        job_type=spec.job_type,
        llm_overseer=spec.overseer,
        llm_exec=spec.executor,
    )
    store.set_short_aim(session_id, spec.short_aim)
    store.update_fields(session_id, aim_score=spec.aim_score)
    return session_id


def seed(home: Path) -> list[str]:
    """Populate a fresh demo home under *home*; return the seeded session ids.

    Idempotent-ish: writes over any existing demo state under *home* (the ids are stable),
    so re-running produces the same content. Writes ONLY under *home*.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    _write_config(home)
    _write_usage(home)
    ids: list[str] = []
    with Store(home / "command-center" / "state.db") as store:
        for spec in _sessions():
            ids.append(_seed_session(store, home, spec))
        for draft in _drafts():
            ids.append(_seed_draft(store, home, draft))
    return ids


def default_dir() -> Path:
    """Default demo home: ``$XDG_CACHE_HOME/ccc-demo`` (or ``~/.cache/ccc-demo``)."""
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else Path.home() / ".cache"
    return base / "ccc-demo"


def run(args: object) -> int:
    """``ccc demo`` entry point: seed a throwaway home and open the TUI (or ``--ls``).

    ``-d/--dir`` overrides the demo home; ``-x/--clean`` deletes it. Points ``CLAUDE_HOME``
    at the demo home *before* anything reads config/store/adapter, so the real state is never
    touched.
    """
    home = Path(os.path.expanduser(getattr(args, "dir", None) or str(default_dir())))
    if getattr(args, "clean", False):
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)
            print(f"removed demo dir: {home}")
        else:
            print(f"nothing to clean (no demo dir at {home})")
        return 0

    os.environ["CLAUDE_HOME"] = str(home)
    # Isolate the Codex usage read too, so the demo's Codex card shows seeded windows rather
    # than the developer's real Codex usage (read from $CODEX_HOME/sessions/**).
    os.environ["CODEX_HOME"] = str(home / "codex")
    # A demo must not depend on (or spawn) Codex/gh; make that explicit for the subprocess env.
    os.environ.setdefault("CCC_NO_CODEX", "1")
    seed(home)
    cfg = config.load_config()

    if getattr(args, "ls", False):
        from .adapters import ClaudeAdapter
        from .views import ls as ls_view

        with Store() as store:
            print(
                ls_view.render(
                    store,
                    ClaudeAdapter(),
                    warn_days=cfg.deadline_warn_days,
                    folder_order=tuple(cfg.folder_order),
                    aim_threshold=cfg.aim_score_threshold,
                    aim_first=aim_column_first(cfg.aim_column),
                )
            )
        print(f"\n(demo home: {home} — reset with `ccc demo --clean`)")
        return 0

    from . import terminal

    try:
        from .views import tui
    except ImportError:
        print("Textual is required for the demo TUI (or use `ccc demo --ls`).")
        return 1
    terminal.set_tab("ccc demo", terminal.color_rgb("magenta"))
    try:
        return tui.run()
    finally:
        terminal.reset_tab_color()
