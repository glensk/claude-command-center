# AGENTS.md

Conventions for AI coding agents (and humans) working in this repo. Long-form invariants live
in [docs/AGENTS_REFERENCE.md](docs/AGENTS_REFERENCE.md) (moved verbatim); read the section the
index below names before you do that kind of work.

## What this is

`ccc` (`claude-command-center`) is a command center for your Claude Code sessions: it tracks
each session's **AIM** (done-condition), progress, next-step and status, groups them by
category, and lets you park work as **future jobs** to launch later. A single self-contained
`uv` package, `command_center/`, plus a Textual TUI, a flat `ccc ls`, a daemon, hooks and
status-line integration, and optional Obsidian + Codex integrations. See the
[README](README.md) for the pitch and [docs/reference.md](docs/reference.md) for the full
feature reference.

## Environment & workflow

```commands
uv sync --all-extras          # install deps (incl. dev extras)
uv run pytest -q              # run the tests (700+; keep them green)
uv run ruff format . && uv run ruff check .
uv run mypy .                 # whole repo, tests/ included
uv run pylint command_center/<files>
```

Install editable while developing (`uv tool install --editable . --reinstall`); the TUI loads
its code at launch, so restart it after a change (`ccc restart-tui`). Secrets live in `.env`
(never commit); see `.env.example`.

## Hard rules (do not regress)

- **TUI keys have one source**: `command_center/views/commands.py` (`COMMANDS`). Never
  hard-code a keystroke or help text in `views/tui.py`; give each command a unique `footer_pos`
  and add it to the README `## Commands` list.
- **Fixed-width cells**: every TUI/`ccc ls` cell reserves its width, blanks included; pin it
  with a test.
- **Startup cost**: transcript facts are persisted, never re-derived per process
  (`core.reconcile_pass` / `scan_transcript(prior)`); `store._row_to_session` drops unknown
  columns; a wedged TUI self-heals (`watchdog.py`); hot subcommands build one subparser.
- **Billing pin**: `accounts.py` is the ONE launch-env pin; never hand-roll `CLAUDE_CONFIG_DIR`
  (default account UNSET, others SET; `CLAUDE_SECURESTORAGE_CONFIG_DIR` always stripped). ccc's
  own LLM calls go only through `llm_custom_command`, never a provider fallback; add a
  `llmrouting.PURPOSES` row for every new LLM call.
- **Codex**: never emit `-s/--sandbox`; `codex_launch.py` builds and `codex_in_claude.
  run_with_fallback` starts every `codex exec`; `-C` always explicit and validated; the prompt
  travels on stdin; resume only via the journal. The seat switch never types values into a
  shell.
- **Launching jobs**: agents never run `ccc start-job` from a background/piped shell; use
  `ccc open-job <id>`. Never add a Terminal.app rung back; never relax `prune_headless`.
- **Flags that fail closed**: `no_codex` and `launch_profile` are propagated by every
  ccc-owned launch surface or the surface refuses before any state change (never a bare
  `claude`); labels stay opaque.
- **`ccc await` / `continue-session` / staff close guard**: state machines with CAS
  transitions and delivery guarantees; read the reference before touching them.
- **Inert defaults**: a fresh install does nothing until opt-in; every such key defaults to
  `False`, lives in `config.INERT_DEFAULT_KEYS` and in the `ccc init` wizard grouping.
- **Public mirror**: this repo is public. `tools/check_public_tree.py` (and
  `tests/test_public_tree.py`) must stay clean; never write a personal path, host, org or
  machine name. Port private content de-personalized.
- **Packaged assets** are read via `importlib.resources`, never relative to `__file__`; every
  console `main()` runs behind `brokenpipe.guard`.

## Read before you...

- touch startup/refresh, the TUI refresh loop, `store`, `config.load_config`:
  docs/AGENTS_REFERENCE.md § "Startup / refresh cost (do not regress)".
- add or change a TUI command or a table column: § "The `commands.py` single-source rule",
  § "Column alignment", § "Internal-style vs TUI commands".
- touch accounts, launch env or LLM routing: § "Multi-account invariants".
- touch Codex seats, the runner or launch policy: § "The Codex seat switch",
  § "The codex launch policy".
- touch `continue-session`: § "The session handoff". `no_codex`/`launch_profile`: §
  "The `no_codex` job flag", § "Launch profiles". `ccc await`: § "`ccc await`".
- touch the staff close guard in `release-locks`: § "The staff close guard".
- launch jobs/tabs, `start-job`, `open-job`: § "Launching jobs: always a tab".
- add package data, slash commands or console entry points: § "Assets / package data",
  § "Where the plumbing lives".
- add an opt-in feature: § "The inert-defaults contract".
- port content from the private repo or publish: § "The public-tree gate".
- see a change in context: § "Trying it / screenshots" (`ccc demo`).

## Private/local notes

Claude Code loads this file directly (global `instructionFiles` setting); `CLAUDE.local.md`
(gitignored) holds machine-specific notes.
