# AGENTS reference

Long-form invariants moved verbatim out of `AGENTS.md` so it stays small. `AGENTS.md` keeps the
one-line hard rules and a "Read before you..." index that points at the sections below; every
section keeps its original heading.

## Rule ledger

Each MUST/NEVER/ALWAYS/safety rule of the old `AGENTS.md` is classified `core` (a one-line
summary stays in `AGENTS.md`, full text below) or `enforced-elsewhere` (a test or gate enforces
it). Commit, secret, public-tree and launch-safety rules are always `core`.

| Rule                                                              | Class              | Enforced by / where in AGENTS.md  |
| :---------------------------------------------------------------- | :----------------- | :-------------------------------- |
| Install with uv; keep tests, ruff, mypy, pylint green             | core               | Environment & workflow            |
| Restart the TUI after a code change; no stale-column crash        | core               | Environment & workflow            |
| Secrets live in `.env`, never committed                           | core               | Environment & workflow            |
| Transcript facts persisted, never re-derived per process          | core               | Hard rules, Startup cost          |
| Hot subcommands build one subparser                               | core               | Hard rules, Startup cost          |
| TUI keys only in `commands.py`; unique `footer_pos`               | enforced-elsewhere | footer_pos unique test; Hard rules |
| Fixed-width cells, pinned by a test                               | core               | Hard rules                        |
| One billing pin in `accounts.py`; no hand-rolled launch env       | core               | Hard rules                        |
| ccc LLM calls only via `llm_custom_command`; add a PURPOSES row  | core               | Hard rules                        |
| Codex: no `-s`, explicit `-C`, stdin prompt, journal resume       | core               | Hard rules                        |
| Never `start-job` from a background shell; use `open-job`         | core               | Hard rules                        |
| No Terminal.app rung; do not relax `prune_headless`               | core               | Hard rules                        |
| `no_codex` / `launch_profile` propagate or refuse                 | enforced-elsewhere | tests/test_no_codex.py, tests/test_launch_profile.py; Hard rules |
| `await`, `continue-session`, staff close guard state machines     | core (pointer)     | Hard rules, Read before you...    |
| Inert defaults list in lockstep with `DEFAULTS` and wizard        | enforced-elsewhere | tests/test_inert_defaults.py; Hard rules |
| Public tree stays free of personal anchors                        | enforced-elsewhere | tests/test_public_tree.py; Hard rules |
| Package data via `importlib.resources`; entry points behind guard | enforced-elsewhere | tests/test_brokenpipe.py; Hard rules |

Totals: 17 rows, 11 core + 1 core (pointer) + 5 enforced-elsewhere; none dropped. Every old
section (21) is kept below unchanged.

Conventions for AI coding agents (and humans) working in this repo.

## What this is

`ccc` (`claude-command-center`) is a command center for your Claude Code sessions: it
tracks each session's **AIM** (done-condition), progress, next-step and status, groups them
by category, and lets you park work as **future jobs** to launch later. A single
self-contained `uv` package, `command_center/`, plus a Textual TUI, a flat `ccc ls`, a
daemon, hooks/status-line integration, and optional Obsidian + Codex integrations. Read the
[README](README.md) for the pitch and [docs/reference.md](docs/reference.md) for the full
feature reference.

## Environment & workflow

Python projects use **`uv`** (preferred over Homebrew/system installs):

```commands
uv sync --all-extras          # install deps (incl. dev extras)
uv run pytest -q              # run the tests (700+; keep them green)
uv run ruff format . && uv run ruff check .
uv run mypy .                 # whole repo, tests/ included (tp#233)
uv run pylint command_center/<files>
```

Install editable while developing (`uv tool install --editable . --reinstall`) — the TUI
loads its code (keys, footer, help) at launch, so restart it after a change
(`ccc restart-tui`). Two consequences of that long-lived process sharing one SQLite DB
with every other ccc process (do not regress): `store._row_to_session` must **drop
columns the running build does not know** (a newer ccc's `ALTER TABLE` under an older
TUI once raised inside its refresh worker and froze the table), and a wedged TUI must
**self-heal**, never sit on a stale frame — `watchdog.py` (a plain thread the 0.1 s poll
beats) dumps every thread's Python stack to `tui-watchdog.log` and re-execs/exits;
`kill -USR1 <pid>` dumps the same on demand.

## Startup / refresh cost (do not regress)

A cold `ccc ls` once took 45 s because every process re-parsed every stored transcript.
The invariants that keep it under a few seconds:

- **Transcript facts are persisted, never re-derived per process.** `core.reconcile_pass`
  bulk-loads `store.transcript_scans()`, asks `ClaudeAdapter.scan_transcript(cwd, sid,
  prior)` for each session and batches the changed rows back in one upsert. The adapter
  returns the *same* `prior` object when `(path, mtime_ns, size)` match — a frozen
  (done/parked) transcript costs one `stat()`. New per-transcript facts go into
  `models.TranscriptScan` + that table (a nullable column added through
  `Store._add_column`, which tolerates the duplicate-column race between two fresh
  builds), not into a fresh full-file read. `core.headless_leak_ids` (prune / daemon) and
  `mirrors._scan_for` (every `ccc sync-mirrors`) go through the same
  `scan_transcript(prior)` call — the per-file `is_oneshot_headless` / `observed_model`
  probes are the fallback for stub adapters only.
- **A persisted fact is a *decided* fact.** `first_record_is_queue_op` is tri-state
  (`None` = empty / unreadable / malformed first record — never persisted as `False`, and
  the next scan re-probes); `last_model_in_file` / `codex_marker_in_file` RAISE `OSError`,
  and `scan_transcript` then publishes no row (every caller contains the exception and
  retries next pass). Property P (`scan_transcript` docstring): every persisted row has
  `codex_scanned_to <= size` and any identity mismatch re-derives every fact from the
  current file (the one exception is `codex=True`, sticky for the session's life: a
  workflow use cannot un-happen, and an append-only file re-finds its marker anyway), so
  concurrent scanners need no ordering guard on the upsert. A read that fails or finds
  no file answers from the prior row (`core._transcript_facts`) — the row `build_rows`
  renders — so one pass never disagrees with its own rows.
- **Read the tail, not the file.** `last_model_in_file` walks backwards in 64 KiB blocks;
  `codex_marker_in_file` resumes `len(marker)-1` bytes before `codex_scanned_to`
  (transcripts are append-only, but a live one can be caught mid-write: the previous pass
  may have covered only the first half of a marker).
- **One registry read and one session read per build.** `build_rows(reconcile_first=True)`
  renders the `ReconcilePass` it just made: `discover()` once, `list_sessions()` once —
  taken AFTER the live loop, with the rows the parking loop wrote refreshed. That snapshot
  is the pass's consistency boundary (a row another process writes after it shows one
  build later). `store._row_to_session` reads positionally (`tuple(row)` + one
  `_session_columns` per SELECT): `row["name"]` on `sqlite3.Row` is a linear scan.
- **`config.load_config()` is memoized** on the file's `(path, mtime_ns, size)` and returns
  a deep copy; every writer of `config.toml` goes through `save_config` (which invalidates).
  Hot per-row paths pass the resolved `root` down (`tabsymbol.cell_for(..., root=)`).
- **Restricted reconcile for the TUI.** `build_rows(..., restrict=True)` (the TUI worker only;
  `ccc ls`, the daemon and every other caller keep the full pass) loads and reconciles only
  the sessions a tick can show — `Store.list_relevant_sessions` selects them in SQL:
  not-done, drafts, every session `discover()` reports live, and (with finished rows shown)
  done rows inside `done_max_age_days`. 1.4k done rows were ~0.25 ms each per tick; done rows
  outside the set keep their persisted facts and the daemon's pass keeps them current.
- **Rebuild on change, never back to back** (`refreshgate.py`). The 1 s `_tick` is a change
  check, not a rebuild: `PRAGMA data_version` of the UI connection, the live registry entries
  and the live rows' transcript `(mtime_ns, size)`, or 6 × `usage_refresh_sec` without a
  signal. A build starts no sooner than `max(3 s, 2 × the last build)` after the previous one
  ended. The data-version baseline is taken when a build ends (its own commits are not a
  change); a foreign commit during a build is caught by the worker's own connection. Explicit
  actions still call `refresh_data()` and rebuild at once. The usage cards repaint every
  `usage_refresh_sec` regardless.
- **The table is diffed, not cleared.** `_apply_rows` collects rows into a `_RowPlan`
  (the `_add_*` helpers write into it) and `_draw_plan` updates only changed cells when the
  keys and their order equal the last plan (cells compared by plain text, spans AND base
  style — `Text.__eq__` ignores the base style); any other shape clears and re-adds.
- **One build at a time in the TUI.** `refresh_data` refuses to start a second
  `data-refresh` thread worker (Textual cannot interrupt a thread — `exclusive=True` only
  produced cancelled-but-running duplicates); an explicit call that lands mid-build sets
  `_refresh_pending` and `on_worker_state_changed` runs the single follow-up. The first
  worker run paints `build_rows(reconcile_first=False)` (stored rows + persisted facts)
  before the full reconcile, so rows appear within ~1 s of launch. The Codex usage
  snapshots are read in that worker too (`read_codex_usage` stats every rollout file
  of a `CODEX_HOME`; the directory listing is cached per home by directory mtime, so only
  the per-file stats remain — ~25 ms warm for ~1900 files vs 60-90 ms for the old glob) and travel with the rows in ONE
  `call_from_thread(_apply_rows, rows, snapshots)`; `_update_usage` renders
  `self._codex_usage` and never reads.
- **Idle jump poll = three `stat`s.** `_poll_jump_request` (0.1 s) returns after
  `jumpstate.any_pending()` (restart/toggle/request file existence) when nothing is
  pending; only a present file goes through the `peek_*` readers.
- **`Session(**data)` needs interned keywords.** `store._session_columns` interns the
  column names: sqlite returns fresh `str` objects and CPython matches ~100 keywords by
  string comparison instead of identity otherwise (290 µs → 33 µs per row).
- **Hot spawns build one subparser.** `cli.main` passes `argv[0]` to `build_parser(only=…)`
  when it is in `cli._HOT_SUBCOMMANDS` (`statusline`, `aim`, `hook`, `tab-symbol`: the
  status line spawns three ccc processes per render per live session every 3 s, every hook
  event one more, the shell badge hook one per `cd`). The same `_add_<name>(sub)` function
  builds the subparser in both modes, so every argv form of a hot subcommand is fast by
  construction (no form list to keep in sync); `llmrouting` is imported only where used.
  Hook events have one source, `hookspec.HOOK_EVENTS` (`HOOK_SPEC` wires `post-tool-use`
  twice, so `ALL_HOOK_ARGS` is a set source, not a `choices=` source). `ccc doctor` §
  "Spawn fast path" lists the subcommands the installed wiring spawns and whether each takes
  the short path.
- **`ccc aim` never imports `cli`.** `command_center/entrypoint.py` is the dispatcher of both
  the `ccc` console script (`[project.scripts]`) and `python -m command_center`. It answers
  `aim` with the standard library only: a read-only `sqlite3` URI connection (no schema DDL,
  no commit), the needed columns only, `aim_on_main_line` from one `tomllib` read of
  `config.toml`, and the pure bar helpers of `bars.py` (stdlib-only, re-exported by
  `models`/`cli`). It computes the whole output before printing and hands argv to
  `cli.main` only on defined "unsupported" conditions (unknown verb, any argv beyond the
  hand-parsed `--session X|=X` / `--format F|=F` forms, no session id, missing DB, any SQLite
  error such as a missing column) before any byte is written. It never imports argparse:
  on Python 3.14 argparse costs ~100 M instructions (`_colorize` pulls in dataclasses,
  inspect, ast), so `-h`, abbreviations and option errors are `cli`'s argparse answer.
  The status line spawns `ccc aim` per render per live session; this path costs ~226 M
  instructions (median of 5, 2026-11-06; ~300 M with argparse) vs ~560 M for a full `cli`
  start. `tests/test_aim_contract.py` pins the output bytes; `tests/test_entrypoint.py`
  proves in a subprocess that a served `aim` loads none of `cli`, `config`, `models`,
  `store`, argparse, rich or textual — keep any new import out of that path.
- **`ccc statusline --session <sid> --capture-usage` never imports `cli` either.** The same
  dispatcher answers it from the stdlib modules `cli` itself renders through (`slrender`,
  `rolemark`, `livestatus`, `usagecache`, `staffrole`, `bars`): read everything and render
  the whole output first, then the usage write, the role-mark sync and one stdout write. It
  falls through to `cli` before any write on the rules listed in the `entrypoint` docstring
  (other argv, missing DB or SQLite error, an effort level in the payload, a session live
  under two accounts, an unreadable config, any other read-phase exception);
  `CCC_NO_FASTPATH=1` sends every verb to `cli` (kill switch). Cost: ~290 M instructions
  per call (median of 5, 2026-11-07) vs ~815 M through `cli`.
  `tests/test_statusline_fast.py` pins byte parity with `cli` and the forbidden imports.
- **One Copilot refresh per window, success or not.** `usage.claim_copilot_refresh()` (flock
  on a sidecar lock, staleness rechecked under it, atomic `copilot_usage.attempt` stamp) is
  called by the TUI usage tick and the daemon's `_refresh_copilot_usage` BEFORE they spawn
  `ccc copilot-usage` / fetch; `copilot_usage_stale` uses the newer of the cache mtime and
  the attempt stamp. A failing `gh` (it once failed only in ccc's environment) used to leave
  the cache stale, so the TUI respawned `ccc copilot-usage` + `gh` + `security` on every tick
  (~66 execs/min). Explicit `ccc copilot-usage` bypasses the claim; an attempt never touches
  the cache's mtime or `captured_at`.
- **One `git status` per 3 s, process-wide.** `gitstatus.cached(cwd)` only reads the last
  value; `gitstatus.refresh_one(cwds)` (called from the TUI refresh worker, never from
  `_add_session_row`) is the only place Git is spawned: at most once every
  `_SPACING_SECONDS`, for the least-recently-refreshed cwd older than `_MIN_AGE_SECONDS`.
  The old per-row 20 s TTL ran ~1 `git status`/s in bursts with ~18 session cwds. Stale
  glyphs stay displayed until their turn.
- **Measured and rejected (2026-11-06):** a parsed-`config.toml` JSON cache across processes
  and a `Store` fast-open that skips the schema scripts behind a `PRAGMA user_version`
  fingerprint saved 2.7 % and 1.2 % instructions on `ccc statusline` (median of 5; the bar
  was 10 %). Import cost dominates a hot ccc spawn, so neither was built; do not add them
  without a new measurement that clears the bar.

Secrets live in `.env` (never commit); see `.env.example`.

## The `commands.py` single-source rule (do not regress)

`command_center/views/commands.py` is the **single source of truth** for TUI keys. The key
bindings, the bottom footer hint line, the column-header mnemonics, and the help are all
derived from its `COMMANDS` registry. When you add or change a TUI command:

1. add/edit it in `commands.py` — never hard-code a keystroke or duplicate help text in
   `views/tui.py`;
2. give it a `footer_pos` so it appears in the footer (a command with no `footer_pos` is
   invisible there — a bug unless deliberate). `footer_pos` values must be unique (a test
   enforces it) and may be fractional to slot between neighbours;
3. add it to the README `## Commands` list (and, if it's substantial, docs/reference.md).

## Column alignment (do not regress)

Every cell in the TUI table and in `ccc ls` reserves a **fixed width — blank cells
included** — so the columns after it never shift between rows (live / waiting / parked /
done / future alike). Pad the raw text (ANSI + OSC 8 have zero width; `💤 😴 🏠 💼` are two
cells), and pin it with a test that asserts one identical offset across statuses. Full rule

- worked example (`cachettl.CELL_WIDTH` / `cell_padding`): README § "Column alignment".

## Internal-style vs TUI commands

Not every CLI subcommand is a TUI key. **Internal-style** commands (e.g. `score-aim`,
`short-aim`, `check-drift`, `assess-aim`, `sync-future`, `sync-mirrors`, `copilot-usage`,
`quota`, `claude-usage`, `codex-usage`, `tab-symbol`, `install-shell`, `demo`, `close-now`,
`switch-now`; `switch-account` is CLI-only too — a slash command arms it) are spawned
by hooks/the daemon/other
commands (or the shell integration) or run by hand; they have **no** `commands.py` entry and
no footer key. Only user-facing TUI actions belong in `commands.py`. `ccc jump` is a special
case: it's a global chord, not a TUI key, so it has no entry either.

## Multi-account invariants (do not regress)

`accounts.py` is the **one billing pin** — never hand-roll the launch env. Claude Code
hashes `CLAUDE_CONFIG_DIR` into its Keychain service name whenever the var is SET, so the
default account (the first `claude_accounts` entry) must have it **UNSET** and any other
account **SET** to its configured spelling; `CLAUDE_SECURESTORAGE_CONFIG_DIR` is always
stripped. Use `launch_env` / `apply_to_environ` / `launch_env_prefix` — three renderings of
that one rule. `accounts.relaunch_command` is the single renderer of the line `switch-now`
TYPES into a tab — including `switch-account -p/--prompt`'s positional prompt (bare `-p` =
`cli.SWITCH_CONTINUE_PROMPT`; `/cwork-to-cpriv-cache-write-hit` sends `-K`, which submits
that word only
when the session's work was really interrupted — a usage-limit halt per
`limitswitch.observe`, or a `-f`'d live turn — so a seat change never turns an idle session
into a running one): shell-quote there, and reject control characters at BOTH
CLI boundaries, since a newline in that string submits the typed line early.
`sessions.config_dir` records the account a session last ran under; `""`
means **unknown and fails closed** on every launch-shaped surface (`cmd_resume`,
`cmd_resume_job`, `cmd_start_job`, and `jump._resume_selected`) when several accounts are
configured — the shared `accounts.live_conflict` also refuses an id live under two accounts
at once. ccc's own headless LLM calls never pick a provider: `llm_custom_command` (normally
`ai prompt -R judge -p "$CCC_LLM_PURPOSE"`, with `CCC_LLM_PURPOSE` / `CCC_LLM_NOTE` in the env)
is the ONE route, and an unset or failing router is a logged failure — no `claude -p`,
Codex, Copilot or Gemini fallback inside ccc (removed 2026-09-23; the old `score_backends`,
`short_aim_backend`, `llm_account` and `*_model` keys are ignored when found in a config). **`ccc llm-routing` (also embedded
in `ccc -h`) resolves every one of those calls against the live config and names the quota it
bills** — `command_center/llmrouting.py`, the counterpart to `ai.py routing`; check it before
claiming which provider a ccc action costs, and add a row there (plus its label in
`llmrouting.PURPOSES`) whenever you add an LLM call. A row whose `llm_custom_command` is
`ai.py` is resolved for real through `ai routing -p <purposes>` — one subprocess for all of
them, ANSI-safe column widths, ai.py's own colours — so never hard-code what a router does.
That subprocess is gated behind `llmrouting.help_requested()`: the full `build_parser()` runs
on every non-hot invocation, an epilog is only printed for `--help`, and the hot subcommands
(`_HOT_SUBCOMMANDS`, see "Startup / refresh cost") build a single epilog-free subparser — so
keep the block out of the hot path.
It flags any action still billing the OpenAI Codex seat, which is reserved for
`/codex-debate` (the retired `short_aim_backend = "codex"` used to spend ~17.6k Codex tokens
per ten-word label). See the multi-account section of [docs/reference.md](docs/reference.md).

## The Codex seat switch (`codex_switch.py`)

`command_center/codex_switch.py` is the Codex twin of `switch-account -N`: a live Codex TUI
thread moves to another `CODEX_HOME` in the same tab, zero tokens, via a `UserPromptSubmit`
hook that pipes the payload into `ccc codex-switch`. Invariants (debated with Codex, 19
objections, all accepted — see `docs/reference.md`): the rollout is the hook's
`transcript_path` (never rediscovered by glob) and its `session_meta` must name the thread;
the process is the OUTERMOST exact-name `codex` ancestor in the foreground of its tty; argv
and environ come from `snapshot.procargs_full` (`KERN_PROCARGS2`), the options from a
COMPLETE allow-list (unknown → refuse, `--remote` → refuse, `-p` → per-home profile file
must match); the launch is a 0600 record executed by `ccc codex-switch-exec <token>` — no
value is ever typed into a shell; the TUI is quit with `/quit` only (no SIGTERM); the
rollout is PREFIX-MERGED (the target's own bytes kept, only the new tail's `rate_limits`
nulled — ccc's per-seat readers and Codex's byte-offset history projection both depend on
that; divergence is set aside and refused); a failure after the source exited relaunches
the SOURCE seat. Tests: `tests/test_codex_switch.py`.

## The codex launch policy (do not regress)

`command_center/codex_launch.py` is the ONE place a `codex exec` command line is built, and
`codex_in_claude.run_with_fallback` is the ONE place ccc STARTS one — `delegate` and the machine
`run` subcommand both go through the runner, and nothing else may assemble
or spawn codex argv. External consumers (`codex-review.py`, sdsc-automations' checker) call
`codex-in-claude run -j` instead of `codex exec`, so they inherit the seat POLICY, the run-time
fallback and the typed errors for free. Three invariants:

1. **Never emit `-s`/`--sandbox`.** Codex ≥ 0.150 uses NAMED permission profiles
   (`-c default_permissions="hardened-ro"` / `…="hardened-rw"`); the legacy flag overrides the
   profile and drops its deny rules (credential stores, workspace `.env`/`*.pem`, no network).
   A write round is emitted ONLY when the active `$CODEX_HOME/config.toml` really declares
   `[permissions.hardened-rw]` — else refuse (exit 2), never degrade to `workspace-write`.
2. **`-C` is always explicit and validated** by `resolve_workdir`: `$HOME` and any ancestor of
   `$HOME` are refused; an IMPLICIT cwd is accepted only inside a git work tree; an EXPLICIT
   non-git dir is accepted and is the only case that adds `--skip-git-repo-check`.
3. **`--resume` goes through the journal** `$CODEX_HOME/ccc-sessions.jsonl` (0600, one line per
   launch). `codex exec resume` inherits the old session's permissions and root while `--write`
   is recomputed from the new CLI, so a resume is honoured only when the session is journalled
   on this seat, the mode matches, and the recorded root still passes `resolve_workdir`.

Refusals raise `CodexLaunchError` → exit 2 at the CLI boundary (`CodexMissing` → exit 4). Tests:
`tests/test_codex_launch.py`. The profile TOML lives in the user's `config.toml`, documented in
[docs/reference.md](docs/reference.md) § "Codex launch policy".

Four more the runner owns (tests: `tests/test_codex_runner.py`, `tests/test_codex_order.py`):

4. **The seat is chosen per attempt, not per process.** `codex_homes_in_order()` re-reads the
   quota rows + cooldown state before EVERY attempt; a run-time refusal (`quota` /
   `entitlement` / `auth`, classified only from the `--json` `error`/`turn.failed` events, never
   from item text or the prompt) records a block and hops to the next seat. A task failure, a
   timeout, a stall and a write-mode refusal that already touched the worktree do NOT hop.
   Zero eligible seats ⇒ **no process at all** and `error.kind = all_seats_unavailable`.
   *Which* seat leads is `codex_seat_policy` (`quota.rank_codex_seats`, pure and injectable):
   `fill` (the default) spends the seat whose WEEKLY allowance resets soonest — seats resetting
   within 12 h of each other are one cohort, filled equally by 5 % usage bucket then by the
   `codex-seat-attempts.json` ledger — while `order` is the strict `codex_seat_order`. The
   account pin leads under `fill` but only enrolled paths may be pinned; under `order` an
   explicit order disarms it. A seat SHARED on alternating weeks (`codex_seat_rota`) is a
   COMPUTED block for the weeks that are somebody else's — absolute for automation (`-Q`, a
   registered `$CODEX_HOME` and a journal resume included, outcome `skipped:rota`), fail-closed
   when the entry is unreadable or does not name `codex_seat_rota_me`, and overridable only by
   a human `/switch <seat>!`. The TUI reads the SAME verdict (`quota.rota_verdict`, config +
   clock, no home touched) to fold that seat's usage card away for the week and mark its title
   ⛔ — view-locally, so no `usage_card_*` gate is written and the card returns by itself at
   the handover. Every round the runner makes is also written to the **run ledger**
   `codex-runs.jsonl` (`codex_ledger.py`, one line per physical attempt: seat, purpose,
   outcome, ms, tokens, cwd, session) — the only durable record of Codex spend from this
   machine (ephemeral runs leave no rollout); `ccc quota` shows each seat's `last_run` and
   `ai logs` (ai.py) merges the lines. See [docs/codex.md](docs/codex.md) §§ "Seat
   policy", "Sharing a seat on a weekly rota" and "The run ledger".
5. **Per-seat argv, always rebuilt.** `permission_args(write, codex_home=cand.home)` and
   `mcp_disable_args(cand.home)` are recomputed for each attempt with a fresh `-o` file — the
   first seat's profile or MCP flags must never leak onto the second.
6. **The prompt travels on stdin** (argv ends with `-`): a repo map plus a revision round
   exceeds `ARG_MAX`, and an argv prompt is readable by every `ps` on the machine.
7. **The process GROUP is swept on every exit**, leader alive or not, and the heartbeat carries
   `codex_pgid` + `runner_pid` so a consumer's last-resort timeout can kill the group directly.

## The session handoff — `ccc continue-session` (do not regress)

`continue_session.py` + `transfer_store.py` + `handoff_doc.py` + `inflight.py` +
`handoff_capped.py` + `handoff_close.py` + `handoff_sweep.py` (tp#1300; docs/reference.md § "Hand a session to a new one"). Invariants:

- **One in-flight detector.** `inflight.background_work` IS `cli._background_work` (the
  `switch-account` veto); `inflight.inventory` classifies the same evidence for the handoff.
  Unknown is never clear.
- **The successor is NEW**: a draft with a fresh UUID launched through `start-job`
  (`--session-id`), never `--resume`; `-n` writes nothing (not even the trust flag).
- **Ownership moves only on `accept`**, from the recorded target session under the recorded
  account, after EVERY `handoff_hooks` entry succeeded (argv-only, bounded, JSON `"ok":
  true`); the source is retired only after `accepted` (`finalize`: idempotent session
  writes first, the `accepted → finalized` CAS last). **`accept` is the primary finalizer**:
  once it has CASed `accepted` and the source is idle (or alive) it finalizes at once (marks
  the transfer finalized, writes the source's handed-off state) and spawns the token-scoped
  closer `ccc close-now --handoff-transfer <id> -s <src>`; `start` in the source is an
  idempotent race participant (first finalizer wins, the other is a no-op). A capped
  transfer is finalized as metadata only: the source is never closed. A handed-off row
  refuses every new prompt (`UserPromptSubmit` hook) and `ccc await` refuses arming on it.
  A `handed_off` session is terminal and
  refused by `limitswitch`, `resume.py` and the daemon's auto-resume (all keyed on
  `handed_off_to`, never on `done`): `core.reconcile_pass` closes it as done (one
  `handoff_done_sources` query per pass) once its transfer is `accepted`/`finalized` AND its
  process is gone, and `set_handoff_lineage` never lifts a done source back out.
- **The handoff closer is the only thing that closes a handed-off source**
  (`handoff_close.py`): it claims the source's close token, binds ONE live process (PID +
  start time), requires 10 s of stable idle (one transcript identity throughout), drains the
  Stop chain, revalidates, SIGTERMs and closes the terminal; any refusal hands the token
  back. `handoff_sweep.py` runs in EVERY `ccc daemon` pass, independent of reaping: it
  finalizes stranded `accepted` transfers and retries pending close tokens of finalized
  ones (one closer spawn per source per 10 min); a kept `handoff:` close debt (signal sent,
  pane still open) is retried by closing the pane only. It never closes a tab by age and
  never re-arms a completed close, so a tab the user reopened stays open. `ccc daemon
  --dry-run -v` shows `handoff_finalized=` / `handoff_closers=` / `handoff_reclaimed=`.
- **A dead closer's in-flight token is reclaimed, a live one's never** (tp#1595). Every
  claim records its holder's `"pid:start"` (`close_holder`) and the claim time
  (`close_claimed_at`); the handoff closer probes its own identity BEFORE claiming and
  refuses unclaimed when `ps` is unreadable, and the Stop chain's holderless claim is filled
  in by the spawned `close-now` (`hold_close`). `restore_close`/`retire_close` from a
  closer CAS on its holder. The sweep reclaims an in-flight token only when it is at least
  `handoff_sweep.STALE_MS` (20 min) old AND holderless or its holder is provably dead
  (`probe_identity` → `PID_GONE` or `OK` with another start, one `ps_table()` per pass); a
  live holder (however old) and an unreadable `ps` are never stale, and a legacy token
  without a claim time is first stamped "seen now" (`mark_in_flight_seen`). The reclaim
  (`reclaim_stale_close`) CASes on exactly the values read, puts the token back PENDING and
  KEEPS `close_bound`, so a `handoff:` debt stays a debt; it runs after the live/idle/debt
  gate, spawns nothing (the pending branch does, paced, on a later pass) and writes nothing
  under `--dry-run`. **The debt is written before the signal**: after the revalidation
  the closer stores its `handoff:` debt on the still in-flight token (`bind_close_debt`,
  holder CAS; a failed write refuses unsignalled), so a closer killed after SIGTERM leaves
  a debt and the retry only closes the pane. The age floor only gates reverting an
  existing token of a candidate the query already returns: no tab is ever closed by age.
- **The Stop chain never destroys a handoff token** (tp#1595 B4). Close tokens on a
  handed-off row belong to the daemon sweep. `claim_after_turn`'s clearing branches leave the
  whole close arm (stamp, token, `close_bound`, holder, claim time) alone when
  `handed_off_to` is set; a fresh pending token is still claimed. When the row's transfer
  passes `handoff_close.lineage_refusal` (`keeps_close_token`, via
  `lineage_transfer_id`: finalized, not capped, `handed_off_to` = its target), every
  refusal of the generic `close-now` (any cause, also a TTL-expired transient one) and the
  hook's binding-mismatch or failed-spawn path hand the SAME in-flight token back pending
  with no TTL (`restore_close(ttl_ms=0)`, holder CAS; a `handoff:` debt is kept
  byte-for-byte), never `retire_close`; a lost CAS leaves the row alone. A capped
  transfer, an unfinalized one or a row that is not handed off retires exactly as before;
  a successful SIGTERM still retires. Nothing is minted, and a retired token is never
  re-armed.
- **The capped-seat summary goes through `llm_custom_command`** (`CCC_LLM_PURPOSE=
  handoff-summary`, a `llmrouting.PURPOSES` row) — never a provider of ccc's own — and its
  text passes the same `handoff_doc.validate` (scrub fail-closed) as a model-written one.
- **ccc stays tracker-free**: ticket ownership moves through the generic, inert
  `handoff_hooks` key only (public mirror).
- **The source's `ccc await` group moves with the transfer, by code** (`await_groups.
  transfer_id`; § "`ccc await`" has the store side): reserved in `create_transfer`'s
  transaction; a group the source arms LATER joins the reservation inside its arm
  transaction; `accept` runs the accept-prepare transaction (`prepare_accept`: sweep the
  source's unreserved groups in, refuse when the successor already holds a group, refuse
  retryably while a reserved group is in a non-stale `delivering`) BEFORE any
  `handoff_hooks` entry, so a refusal never follows a hook that moved ticket ownership;
  the rebind (successor's session, account dir, cwd, `no_codex`, launch profile; every
  fired group under a fresh `delivery_token`) runs in the `accepted` CAS's transaction,
  after every hook; a `failed` transfer releases the reservation in its own transaction.
- **Facts are code, collected once per transfer** (`handoff_facts.py`, `handoff_doc.compose`,
  tp#1479): six sections are filled from recorded facts, the model writes only `Current
  state`, `Decisions and constraints`, `First steps` (`validate_model_part`: ≤ 10 KB, no
  other `##`). `start` freezes ONE snapshot `handoffs/facts/<sid>/<snap12>.json` (id =
  sha256 of its canonical JSON, the transfer's `facts_snapshot` column); `prompt_sha256` =
  sha256(model part) + snapshot id, and a re-run on an active `prepared`/`opened` transfer
  reuses the stored snapshot and composed text without recollecting — never let a
  recomposition move that digest (it forks the `EXIT_WAITING` retry).
- **Providers never block a relay**: `handoff_facts` entries (default `[]`, inert) return
  typed records from a closed `kind` set, never markdown; a failing, slow or malformed
  provider becomes one `warning` fact. Every free text ccc inserts (to-do copy, assistant
  texts, router supplement, legacy notes, result capsules, provider strings) passes
  `escape_headings`, because `sections()` keeps the FIRST `##` and ignores fences.
- **Fail closed on scrub, never on size**: every persisted facts file (snapshot, capture,
  agent spec/result) goes through `mirror_scrub_cmd`; a scrub failure refuses `start`. Size
  is solved by priority cuts with a `continue-session facts` pointer (never the AIM, held
  ids, questions, restarts, spec paths, warnings). A code-generated model part is cut to
  fit (`… cut — full original:`); a model-written one is refused with the bytes it may have
  (tp#1835).
- **Context-relay files are read, never imported** (`relay_state.py`, tp#1536): a subagent
  with an unconsumed relaunch record (`<CC_RELAY_STATE_DIR>/relaunch/<sid>/<agent>.json`,
  version 1) hands over its capsule as `result` plus `capsule_path` and `relaunch` = the
  record's `call` VERBATIM (never re-rendered) and is listed as a continuation; a missing
  or corrupt record changes nothing. `accept` (at the `accepted` event) and `finalize`
  (idempotent) write the source's retired marker `<state dir>/retired/<sid>.json`
  `{transfer, successor, at}` (0600, dir 0700, temp + rename) only when the state dir
  exists; a write failure warns, never blocks the handoff.
- **`start -D` drains before anything exists; `-w` fits the tool cap**: `-D N` (0..60) runs
  the ONE detector (`_drain_source`, shared with `drain`; keep-warm ignored, nothing
  signalled) BEFORE `_compose`, so a busy source exits 1 with the inventory and no
  transfer row. `effective_wait` lowers `-w` to `min(-w, max(120, 540 − D − Σ
  handoff_facts timeout_s − 30))` and `start` prints it on stderr; callers pass the Bash
  tool `timeout: 600000`. Never let the clamp raise `-w`.
- **`--facts-only` / `--capped` share one path** (`_compose_generated`): a capped router that
  is unset, fails or returns anything but the strict JSON supplement falls back to
  facts-only, never to a refusal. `start -j` prints JSON on a dry run only.

## The `no_codex` job flag (do not regress)

`no_codex` on a session row is a promise that NOTHING in that session reaches the Codex seat.
`accounts.session_launch_env` (+ `session_apply_to_environ` / `session_launch_env_prefix`) is the
single place that renders it as `CCC_NO_CODEX=1`; **every** ccc-owned launch/resume surface must
use one of the three — `start-job`, `resume`, `resume-job`, the TUI `r` + undo-close, `jump`,
`fire-attached`, the halted-session auto-resume, snapshot restore, and the handoff successor
of `continue-session start` (its draft carries the source's `no_codex` into `start-job`, on
the model-written and the `--capped` path alike). A surface that calls the
bare `accounts.launch_env*` functions silently drops the flag; `tests/test_no_codex.py`
enumerates them. The flag is never *cleared* — an ambient `CCC_NO_CODEX` is preserved. It is
mutually exclusive with a `codex`/`codex-write` job type (`models.no_codex_conflict`), refused at
creation AND again at launch.

## Launch profiles (`launch_profile`) — billing route, fail closed (do not regress)

A `launch_profile` on a session row is an opaque label (config `claude_launch_profiles`,
`["<label>=<one executable>"]`, default `[]`) that replaces the bare `claude` in every
interactive launch of that session. It is billing-relevant identity, so a surface either
**propagates it or refuses before any state change — never a bare `claude`**:

- `launch_argv.profile_argv` / `claude_argv(launch_profile=)` / `claude_command` swap argv[0] for
  `config.resolve_launch_profile(label)` (re-resolved at EVERY launch; raises `LaunchProfileError`);
  `accounts.session_env_flags` exports ONLY `CCC_LAUNCH_PROFILE=<label>`, and the three env
  renderings unset an ambient label for a profile-less session. `LaunchTarget` / `SnapPane` /
  `StoreRow` carry it like `no_codex`.
- **Propagate:** `start-job` (resolves BEFORE `claim_draft`), `resume`, `resume-job`, TUI `r` +
  undo-close, `jump` (all via `terminal.resume_in_new_tab(launch_profile=)`, which resolves
  before trust or a tab), `fire-attached` (before the claim), snapshot restore.
- **Refuse:** `switch-account` / `switch-now` / `limitswitch` (an account switch does not
  change the billing route), the halted-session auto-resume (before the reap),
  `resume_halted_in_new_tab`, `ccc await` arm and `fire-await`, `continue-session start`.
- The store is the authority: the job file carries `launch_profile` but a file edit never
  re-routes an existing job. Machine contracts (`new-job -J`, `jobs -j`, `sessions -j`,
  `inspect -j`) and the `-K` conflict check include it. ccc stays public: labels are opaque,
  no personal executable or vault item is ever named in this tree. `tests/test_launch_profile.py`
  enumerates the surfaces; a new launch surface must join it.

## `ccc await` — event-triggered resume (do not regress)

`await_store.py` (schema + every transition, a `Store` mixin), `await_probes.py`,
`await_eval.py` (`run_pass` — the ONE evaluation both the 60 s poller `ccc await -r`
and the daemon backstop call), `await_delivery.py`, `await_prompt.py`, `await_cli.py`.
Invariants:

- **State machine** (`await_groups.state`): `armed → grace → expired`; the first fired
  source wins `armed|grace → fired` in ONE `BEGIN IMMEDIATE` (`Store.fire_group`, which
  also disarms every sibling); `fired → delivering → delivered` is an OUTBOX keyed by
  `delivery_token` (CAS on every step); `blocked` only via a failed preflight, exhausted
  delivery retries, or no viable source left; `-R` re-opens it (a fresh token, the
  payload and every watermark kept). One ACTIVE group per session (partial UNIQUE index).
  A permanent probe failure blocks the SOURCE, never the group directly.
- **Delivery guarantees.** Live idle tab: type-in, at-least-once. Closed session:
  `ccc fire-await <group> <token>` claims `delivering → delivered` BEFORE the exec,
  at-most-once; a failed launch/exec hands the row back (`revert_delivery`, ≤ 3 tries,
  then `blocked`); a `delivering` row older than 15 min is reclaimed. An unreadable
  live-session registry delivers NOTHING that pass (a live session misread as closed
  would be resumed twice). A pending attached prompt (`fire_at > 0`) makes delivery
  WAIT — it is never overwritten.
- **Handoff reservation** (`continue-session`): a group whose `transfer_id` names a
  `prepared`/`opened` transfer is RESERVED — it polls and may fire, but `mark_delivering`
  never claims it (delivery defers it) until accept rebinds it to the successor or a
  failed transfer releases it to the source. Arming on the source of a pending transfer
  JOINS it (`handoff_join`, inside the arm transaction); arming is REFUSED
  (`AwaitRefused`) on the source of an `accepted` transfer (retired) and on the target of
  a `prepared`/`opened` one (accept moves a group there; one per session). The rebind
  rotates the `delivery_token` of every fired group, so a pass that loaded the row before
  the rebind loses its `fired → delivering` CAS. Accept-prepare refuses while a reserved
  group is `delivering` and younger than the 15 min stale limit (`AwaitBusy`).
- **Trust rule.** Trust is ensured at ARM time (`accounts.ensure_trusted`, a deliberate
  act of the session) and only CHECKED at delivery (`accounts.is_trusted`);
  `fire-await` pins the account with `accounts.pin_environ`, which never grants trust.
  The transcript must exist under the snapshot account's OWN projects dir
  (`await_delivery.account_transcript`, no cross-account fallback).
- **The store connection never crosses a thread**: probes run in the pool, every
  outcome is recorded by the pass's own thread. Probe CLIs go through
  `checks.run_structured` (argv; `shell=True` only for the user-authored `cmd` kind;
  output capped; the process GROUP killed at the timeout; stderr sanitized).
- **Content-free notifications**, one per state (`notified_at`); the event text reaches
  only the prompt, framed as untrusted and defanged (`await_prompt.build_payload`).
- **Live-tab typing is a ladder**: `terminal.send_text_via` tries the iTerm2 Python API,
  then AppleScript (text passed as an `osascript` argv argument, never in the script
  source; a half-sent API paste never falls back). The poller records both channels'
  health (single-row `await_channel_health`, ≤ every 5 min); the TUI, `ccc ls` and the
  detail pane only READ it — rendering never probes iTerm. Tests never probe it either
  (`conftest._stub_channel_probe`).
- `await` is a hot subcommand (`cli._HOT_SUBCOMMANDS`): `await_cli` imports nothing
  heavy at load time, and `-r` returns after one indexed query when nothing is armed.

## The staff close guard in `release-locks` (do not regress)

`command_center/staff_close_guard.py` refuses a Stop of a session that released an
allowlisted llm-staff lease (`staff lease -r` writes `$STAFF_STATE/released/<agent>/<sid>.json`)
without arming `mark-done --close`. Full behaviour: [docs/hooks.md](docs/hooks.md) § "The staff
close guard". Invariants:

- **It runs LAST in `release-locks`**, after the lock lease and the close/switch claim, so it
  can never keep `close-now` from firing; `staff_close_guard.guard` swallows every exception
  (fail open = no block).
- **No marker = one `lstat()` per guarded agent** (`CC_STAFF_CLOSE_GUARD_AGENTS`, default
  `mac-automator`) — no subprocess, no store write. Only a marker hit may run the read-only
  probe `staff lease -s -j` (2 s, `STAFF_BIN` → `$PATH` via `external_deps`). ccc never reads
  llm-staff's SQLite: that subprocess contract is the only coupling.
- **Pass** only on a claimed `CloseClaim`, a done session, or a probe that PROVES
  `held_by_owner` (then the stale marker is deleted). Probe failure or timeout blocks.
- **Block every Stop** (`{"decision": "block", "reason": …}` on stdout, exit 0), counted per
  marker nonce in the `staff_close_guard` table inside one `BEGIN IMMEDIATE`
  (`Store.staff_guard_block`), the block printed BEFORE the commit (a kill under-counts, never
  suppresses). After 5 blocks one Stop passes and `escalated` is logged; any other pass
  resets the count. `events.log` gets ids and the decision only.
- Kill switch `CC_NO_STAFF_CLOSE_GUARD=1`. Tests (`tests/test_staff_close_guard.py`) use a
  stand-in `staff` on `STAFF_BIN` that records its calls; `tests/conftest.py` pins
  `STAFF_STATE` under `tmp_path` and points `STAFF_BIN` at a missing file for every test.

## Launching jobs: always a tab (do not regress)

**Agents: never run `ccc start-job` from a background/piped shell — use `ccc open-job <id>`.**
`start-job` and `resume` `execvp` claude *in place*; without a TTY the exec'd process gets no
stdin, runs its argv prompt as a headless one-shot and exits after one turn. Its transcript
then opens with a `queue-operation` record — the exact signature `is_oneshot_headless` matches
— so the daemon's `prune_headless` pass deletes the row and the job is gone from ccc with no
tab and no error anywhere. That is how job `42fc3505` was lost on 2026-08-28.

**A tab is never assumed, it is reported (tp#90).** `open-job` exits 0 whenever the job
launched — in an iTerm2 tab (`iterm_applescript` / `iterm_api`) OR a tmux window (`tmux`) —
and says which (`-j/--json` → `{"version": 2, "session_id", "launcher"}`); it exits 1 only when
nothing launched. A tmux landing's JSON adds `"tmux": {"session", "window", "pane",
"attach_argv"}` (e.g. `{"session": "ccc", "window": 7, "pane": "%12", "attach_argv": ["tmux",
"attach", "-t", "ccc:7"]}`), built by `terminal.tmux_landing` from the same `list-panes` data
`tmux_pane_for_session` uses and polled for up to `TMUX_LOCATE_WAIT_S` (5 s) while `start-job`
execs claude; `"tmux": null` when the pane is not found in that time (exit status unchanged —
the job runs). Other launchers have no `tmux` key. Version 2 added the field (open-job has its
own `OPEN_JOB_JSON_VERSION`; `new-job`/`jobs`/`terminal-probe` stay at 1). Under launchd the AppleScript rung depends on the macOS Automation grant for
the launchd job's *executable path* (a new python build = a new prompt), and the Python-API
rung is NOT an escape from that gate (the `iterm2` package fetches its cookie via AppleScript,
with no timeout — so it runs only behind iTerm2's `disable-automation-auth` switch). To prove
a context can reach a tab, run `ccc terminal-probe -j` from it (no job, no Claude session) and
look for its marker in a NEW iTerm2 session; `ccc doctor` (Terminal) shows the grant for ccc's
own interpreter. Never add a Terminal.app rung back, and never bound the API rung with a
thread — a blocked `osascript` keeps running and can open a tab after ccc moved on.

`cli.has_terminal()` now gates both commands *before any state mutation*: no TTY → open a real
tab and return 0; no tab available → exit 1 without exec-ing (an `--auto` dispatch also disarms
`fire_at`). Keep that check ahead of `claim_draft`/`archive_file` so a refusal changes nothing.
`CCC_START_JOB_HEADLESS=1` is the only opt-out (set suite-wide by a `tests/conftest.py` autouse
fixture). Do **not** relax `prune_headless` to "fix" a vanished job — deliberately headless
subagent runs (`claude -p`) are *supposed* to stay invisible; the tab is what makes a real job
visible. Tests: `tests/test_start_job_tty.py`.

## Assets / package data

Installable assets (slash commands, Obsidian dashboards/templates, plugin manifests) live
under `command_center/assets/` and ship as wheel package data. Installers read them via
`importlib.resources`, **never** by resolving a path relative to `__file__` — so a
non-editable install keeps working. Stdlib-only helper scripts that must survive a
non-editable install (`codex_in_claude.py`, `session_continue.py`) live *inside* the
package; the repo-root `codex-in-claude.py` is a thin PATH-compat shim. It re-execs into
the repo `.venv`, else the uv-tool venv (`command_center._direct.reexec_into_env`), so a
foreign `python3` first on `PATH` (direnv/nix) cannot break it; with neither venv it exits
70 with one stderr line (tp#394).

The codex assets ship **marker-free**; `install_commands._codex_stamped()` injects the
`[codex <model> effort=<e>]` prefix into their `description:` at plan time (see
`codex_in_claude.sync_markers`), because Claude Code shows that description as the
slash-command help. Two invariants when touching this: an inline description must come out
**double-quoted** (a leading `[` would otherwise read as a YAML flow sequence), and the
stamped files are written **in place** — never temp + `os.replace` — so a dotfiles hard link
to a tracked working copy is not broken. `marker_surfaces()` keys off `$CLAUDE_CONFIG_DIR`,
so tests must set it (the `cic` fixture does) or they will edit the developer's real skills.

## The inert-defaults contract (do not regress)

A fresh install must do **nothing** until the user opts in: no LLM tokens, no network calls
(`gh`/`codex`/`claude -p`/the resume watcher), no auto-close, and writes only under
`CLAUDE_HOME`. Every such feature key defaults to `False` and is listed in
`config.INERT_DEFAULT_KEYS`. Keep that list and `config.DEFAULTS` in lockstep — a test
(`test_inert_defaults.py`) asserts every member is present in `DEFAULTS` and is `False`, and
the `ccc init` wizard's consent groups (`GROUP_A_CHECKERS` / `GROUP_B_VAULT` / `GROUP_C` /
`UNMAPPED_INERT`) must union to exactly `INERT_DEFAULT_KEYS` (a drift test enforces it). If
you add an opt-in feature, add its key to both and to the wizard grouping.

## The public-tree gate (do not regress)

This is a public mirror of a private repo. `tools/check_public_tree.py` scans the tree for
personal/private anchors and **must stay clean** — the only tolerated hits are the entries
in `tools/public_tree_allowlist.txt`, and `tests/test_public_tree.py` runs the same scan
under plain `pytest`, so a new anchor fails the suite rather than only the manual gate:

```commands
python3 tools/check_public_tree.py     # exit 0 == clean
```

Never introduce a personal path, host, org, or machine name. When you port content from the
private repo, de-personalize it (generic `repo_root` instead of a real tree, "your vault",
"a GitHub Copilot seat", etc.). Genuinely-unavoidable references get an entry in
`tools/public_tree_allowlist.txt`; do not add anchors casually.

`tools/smoke_matrix.py` is the pre-publish acceptance battery: it `uv build`s the wheel,
installs it into a scratch sandbox (temp `HOME`/`CLAUDE_HOME`), runs the acceptance commands
(`--help`, `ls`, `demo --ls`, `doctor`, `daemon --dry-run`, `install-hooks` +
idempotent-rerun + `--uninstall`, `init --minimal`, non-TTY `init` → exit 3, the two other
console entry points), and *proves* the real `~/.claude` was untouched (settings.json
byte-identical; only the developer's own live-daemon runtime files may churn). It prints a
✅/❌ matrix and exits non-zero on any failure; `tests/test_smoke_matrix.py` (marked `slow`)
runs it in CI. `tools/seed_from_private.py`, `tools/SEED_STATE.json` and any
`tools/PUBLISH_REVIEW.md` are **build-only** — delete them before the publish squash.

## Trying it / screenshots

- `ccc demo [--ls] [--clean]` seeds a throwaway fake-data home (never the real
  `CLAUDE_HOME`) and opens the TUI/list — the fastest way to see a change in context.
- `tools/gen_screenshots.py` regenerates `docs/img/*.svg` from that same demo data (driven
  headlessly via Textual's `run_test`), so the README screenshots never go stale.

## Where the plumbing lives

- **Installer layer** — `command_center/install.py` owns the hook + status-line wiring
  merged into `$CLAUDE_HOME/settings.json` (`ccc install-hooks` / `install-statusline`;
  symlink-safe atomic writes with timestamped backups, idempotent). `doctor.py` is the
  read-only `ccc doctor` health check. Foreign hooks are preserved by contract, so ccc's
  entries must be the ONLY path to `ccc hook`: `hookroutes.py` answers "does this foreign
  hook command also spawn `ccc hook`?" for both sides — `install-hooks` refuses to install
  next to such a forwarder (`-f/--force` overrides) and doctor reports it as ❌ `duplicate
  hook path`. It must never import `install` or `doctor` (the caller passes the commands
  and the ownership predicate); doctor's "hooks wired" check compares a `Counter` of the
  installed wiring with `HOOK_SPEC`, since a set cannot see a double-wired entry.
- **Onboarding layer** — `wizard.py` (`ccc init`) is the first-run flow (env detection,
  consent checklist, minimal `config.toml`, then the installers, incl. `install-shell`).
  `install_commands.py` (`ccc install-commands`) copies the slash commands; `obsidian.py`
  (`ccc obsidian-setup`) seeds the vault folders, dashboards and shellcommands entries;
  `shell_install.py` (`ccc install-shell`) writes the opt-in shell rc block (AIM-at-startup
  wrapper + cross-terminal OSC tab badges).
- **Platform seam** — `service.py` is the ONE place that decides launchd (macOS,
  `launchd.py`) vs systemd `--user` (Linux, `systemdunit.py`) for the `ccc daemon`
  service, so `cli.py`/`doctor.py` stay platform-agnostic. `notify.py`'s `"auto"` channel
  resolves to `osascript` (macOS) / `notify-send` (Linux). Deterministic per-repo tab
  symbols live in `tabsymbol.symbol_for_repo` / `cell_for` (the live iTerm cache still
  overrides where present); `tabcolor.dedupe_live` recolours open tabs that would share one
  id-chip colour, writing only the per-tab colour cache + its `.manual` marker (the two files
  the status line already honours). Linux hotkey samples: `assets/hotkeys-linux/` (keyd/xremap).
- **iTerm2 Python-API seam** — `snapshot.py`'s executor and `terminal.py`'s API rung.
  Two gotchas of the `iterm2` package: an object it has *just created* answers `None`
  for `Window.current_tab` / `Tab.current_session` (their ids arrive with a later layout
  notification) — address a fresh tab through `window.tabs[0]` / `tab.sessions[0]`
  (`snapshot._sole_session`), never through `current_*`; and `Window.async_create_tab`
  asserts on `Window.delegate`, which only `iterm2.async_get_app(connection)` installs —
  call it before creating tabs. Text may be sent to a fresh pane immediately; the tty
  queues it until the shell's startup files are done (verified, multi-second `.zshrc`).
- **TUI liveness** — `watchdog.py` is the self-heal for a wedged TUI (stalled timers, or
  an exit that hangs): heartbeat + exit-grace verdicts, the `tui-watchdog.log` wedge
  report with every thread's Python stack, terminal restore, capped in-place re-exec;
  `views/tui.py` beats it from the fast poll and starts it only on a real tty.
- **Resident panel server** — `panelserver.py` (`ccc panel-server`, tp#70): opt-in
  LaunchAgent that serves the q+p / s+p chords warm. File protocol (claim-by-rename,
  `panel_ack.<nonce>`), pidfile state machine and the dispatcher (`Core`) are plain Python
  and tested headlessly; `AppKitHost` is the AppKit shell (default + modal-mode timers,
  modal panels, guarded focus hand-back). Chord side: `assets/panel-poke.sh`, installed by
  `panelpoke.py`. Seams it relies on: `peek.Frontmost`/`build_panel`/`PanelLoop`,
  `park.grab`/`GrabOptions`, `parkpanel.capture_prompt(on_shown=…)`,
  `iterm_api.CookieItermLink`/`LinkThread` (the `iterm2` package never authenticates by
  itself there), `transcript_cache.TranscriptCache`. Plist + install in `launchd.py`
  (`panel_server_*`), doctor row `_panel_server_check`, daemon self-heal
  `_restart_stale_panel_server`.
- **Codex run log** — `codex_runlog.py`: one directory per physical `codex exec` attempt
  (argv, seat, exit, cause, events, stderr, reply), opened in `_run_with_fallback` and
  closed on every exit path (ok, task failure, refusal, kill, missing binary); listed by
  `codex-in-claude runlog`. A failure's `error.cause` comes from `failure_cause` and its kind
  from `classify_task_failure` (only for failures `classify_codex_failure` does NOT pin on
  the seat); `_KIND_EXIT` is the ONE kind → exit-code table for `run` and `delegate`. Never
  let the run log raise into the runner — every entrance swallows its own errors.
- **Packaging** — the wheel ships three console entry points (`ccc`, `codex-in-claude`,
  `claude-session-continue`) and the `command_center/assets/` package data. Every one of
  those `main()`s runs behind `brokenpipe.guard` so a reader that closes our stdout
  (`ccc ls | head -3`, `codex-in-claude --help | head -1`) exits 141 silently instead of
  tracebacking; the guard flushes in a `finally` because argparse's `--help` leaves by
  `SystemExit`, past any success-path-only flush. SIGPIPE stays ignored on purpose — these
  CLIs also write to subprocess stdin pipes whose own handlers catch `BrokenPipeError`.
  A fourth entry point must go through the same guard (`tests/test_brokenpipe.py`).

## Private/local notes

Claude Code loads this file directly (global `instructionFiles` setting); `CLAUDE.local.md` (gitignored)
holds machine-specific notes.
