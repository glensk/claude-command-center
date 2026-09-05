# AGENTS.md

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
uv run mypy command_center
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
- **One build at a time in the TUI.** `refresh_data` refuses to start a second
  `data-refresh` thread worker (Textual cannot interrupt a thread — `exclusive=True` only
  produced cancelled-but-running duplicates); a tick that lands mid-build sets
  `_refresh_pending` and `on_worker_state_changed` runs the single follow-up. The first
  worker run paints `build_rows(reconcile_first=False)` (stored rows + persisted facts)
  before the full reconcile, so rows appear within ~1 s of launch. The Codex usage
  snapshots are read in that worker too (`read_codex_usage` globs+stats every rollout file
  of a `CODEX_HOME`, ~0.3 s cold) and travel with the rows in ONE
  `call_from_thread(_apply_rows, rows, snapshots)`; `_update_usage` renders
  `self._codex_usage` and never reads.
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
that one rule. `sessions.config_dir` records the account a session last ran under; `""`
means **unknown and fails closed** on every launch-shaped surface (`cmd_resume`,
`cmd_resume_job`, `cmd_start_job`, and `jump._resume_selected`) when several accounts are
configured — the shared `accounts.live_conflict` also refuses an id live under two accounts
at once. ccc's own headless LLM calls must never bill ambiently: `llm._run_claude` pins its
env to the `llm_account` config, and `llm_custom_command` (with `CCC_LLM_PURPOSE` /
`CCC_LLM_NOTE` in the env) is the pluggable routing hatch. **`ccc llm-routing` (also embedded
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
`/codex-debate` (`short_aim_backend = "codex"` used to spend ~17.6k Codex tokens per ten-word
label). See the multi-account section of [docs/reference.md](docs/reference.md).

## The codex launch policy (do not regress)

`command_center/codex_launch.py` is the ONE place a `codex exec` command line is built, and
`codex_in_claude.run_with_fallback` is the ONE place ccc STARTS one — `delegate`, the machine
`run` subcommand and `llm.run_codex` all go through the runner, and nothing else may assemble
or spawn codex argv. External consumers (`codex-review.py`, sdsc-automations' checker) call
`codex-in-claude run -j` instead of `codex exec`, so they inherit the seat order, the run-time
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
   `codex_seat_order` + cooldown state before EVERY attempt; a run-time refusal (`quota` /
   `entitlement` / `auth`, classified only from the `--json` `error`/`turn.failed` events, never
   from item text or the prompt) records a block and hops to the next seat. A task failure, a
   timeout, a stall and a write-mode refusal that already touched the worktree do NOT hop.
   Zero eligible seats ⇒ **no process at all** and `error.kind = all_seats_unavailable`.
5. **Per-seat argv, always rebuilt.** `permission_args(write, codex_home=cand.home)` and
   `mcp_disable_args(cand.home)` are recomputed for each attempt with a fresh `-o` file — the
   first seat's profile or MCP flags must never leak onto the second.
6. **The prompt travels on stdin** (argv ends with `-`): a repo map plus a revision round
   exceeds `ARG_MAX`, and an argv prompt is readable by every `ps` on the machine.
7. **The process GROUP is swept on every exit**, leader alive or not, and the heartbeat carries
   `codex_pgid` + `runner_pid` so a consumer's last-resort timeout can kill the group directly.

## The `no_codex` job flag (do not regress)

`no_codex` on a session row is a promise that NOTHING in that session reaches the Codex seat.
`accounts.session_launch_env` (+ `session_apply_to_environ` / `session_launch_env_prefix`) is the
single place that renders it as `CCC_NO_CODEX=1`; **every** ccc-owned launch/resume surface must
use one of the three — `start-job`, `resume`, `resume-job`, the TUI `r` + undo-close, `jump`,
`fire-attached`, the halted-session auto-resume, and snapshot restore. A surface that calls the
bare `accounts.launch_env*` functions silently drops the flag; `tests/test_no_codex.py`
enumerates them. The flag is never *cleared* — an ambient `CCC_NO_CODEX` is preserved. It is
mutually exclusive with a `codex`/`codex-write` job type (`models.no_codex_conflict`), refused at
creation AND again at launch.

## Launching jobs: always a tab (do not regress)

**Agents: never run `ccc start-job` from a background/piped shell — use `ccc open-job <id>`.**
`start-job` and `resume` `execvp` claude *in place*; without a TTY the exec'd process gets no
stdin, runs its argv prompt as a headless one-shot and exits after one turn. Its transcript
then opens with a `queue-operation` record — the exact signature `is_oneshot_headless` matches
— so the daemon's `prune_headless` pass deletes the row and the job is gone from ccc with no
tab and no error anywhere. That is how job `42fc3505` was lost on 2026-08-28.

**A tab is never assumed, it is reported (tp#90).** `open-job` exits 0 whenever the job
launched — in an iTerm2 tab (`iterm_applescript` / `iterm_api`) OR a tmux window (`tmux`) —
and says which (`-j/--json` → `{"version": 1, "session_id", "launcher"}`); it exits 1 only when
nothing launched. Under launchd the AppleScript rung depends on the macOS Automation grant for
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
package; the repo-root `codex-in-claude.py` is a thin PATH-compat shim.

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
  read-only `ccc doctor` health check.
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
- **Packaging** — the wheel ships three console entry points (`ccc`, `codex-in-claude`,
  `claude-session-continue`) and the `command_center/assets/` package data.

## Private/local notes

`CLAUDE.md` is a gitignored shim that imports this file; `CLAUDE.local.md` (also gitignored)
holds machine-specific notes.
