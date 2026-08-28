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
loads its code (keys, footer, help) at launch, so restart it after a change.

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

+ worked example (`cachettl.CELL_WIDTH` / `cell_padding`): README § "Column alignment".

## Internal-style vs TUI commands

Not every CLI subcommand is a TUI key. **Internal-style** commands (e.g. `score-aim`,
`short-aim`, `check-drift`, `assess-aim`, `sync-future`, `sync-mirrors`, `copilot-usage`,
`claude-usage`, `tab-symbol`, `install-shell`, `demo`) are spawned by hooks/the daemon/other
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
`CCC_LLM_NOTE` in the env) is the pluggable routing hatch. See the multi-account section of
[docs/reference.md](docs/reference.md).

## Launching jobs: always a tab (do not regress)

**Agents: never run `ccc start-job` from a background/piped shell — use `ccc open-job <id>`.**
`start-job` and `resume` `execvp` claude *in place*; without a TTY the exec'd process gets no
stdin, runs its argv prompt as a headless one-shot and exits after one turn. Its transcript
then opens with a `queue-operation` record — the exact signature `is_oneshot_headless` matches
— so the daemon's `prune_headless` pass deletes the row and the job is gone from ccc with no
tab and no error anywhere. That is how job `42fc3505` was lost on 2026-08-28.

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
personal/private anchors and **must stay clean** (only the two documented hits in
`tools/SEED_STATE.json` are allowed):

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

+ `ccc demo [--ls] [--clean]` seeds a throwaway fake-data home (never the real
  `CLAUDE_HOME`) and opens the TUI/list — the fastest way to see a change in context.
+ `tools/gen_screenshots.py` regenerates `docs/img/*.svg` from that same demo data (driven
  headlessly via Textual's `run_test`), so the README screenshots never go stale.

## Where the plumbing lives

+ **Installer layer** — `command_center/install.py` owns the hook + status-line wiring
  merged into `$CLAUDE_HOME/settings.json` (`ccc install-hooks` / `install-statusline`;
  symlink-safe atomic writes with timestamped backups, idempotent). `doctor.py` is the
  read-only `ccc doctor` health check.
+ **Onboarding layer** — `wizard.py` (`ccc init`) is the first-run flow (env detection,
  consent checklist, minimal `config.toml`, then the installers, incl. `install-shell`).
  `install_commands.py` (`ccc install-commands`) copies the slash commands; `obsidian.py`
  (`ccc obsidian-setup`) seeds the vault folders, dashboards and shellcommands entries;
  `shell_install.py` (`ccc install-shell`) writes the opt-in shell rc block (AIM-at-startup
  wrapper + cross-terminal OSC tab badges).
+ **Platform seam** — `service.py` is the ONE place that decides launchd (macOS,
  `launchd.py`) vs systemd `--user` (Linux, `systemdunit.py`) for the `ccc daemon`
  service, so `cli.py`/`doctor.py` stay platform-agnostic. `notify.py`'s `"auto"` channel
  resolves to `osascript` (macOS) / `notify-send` (Linux). Deterministic per-repo tab
  symbols live in `tabsymbol.symbol_for_repo` / `cell_for` (the live iTerm cache still
  overrides where present); `tabcolor.dedupe_live` recolours open tabs that would share one
  id-chip colour, writing only the per-tab colour cache + its `.manual` marker (the two files
  the status line already honours). Linux hotkey samples: `assets/hotkeys-linux/` (keyd/xremap).
+ **Packaging** — the wheel ships three console entry points (`ccc`, `codex-in-claude`,
  `claude-session-continue`) and the `command_center/assets/` package data.

## Private/local notes

`CLAUDE.md` is a gitignored shim that imports this file; `CLAUDE.local.md` (also gitignored)
holds machine-specific notes.
