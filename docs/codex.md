# Delegating implementation to OpenAI Codex

`ccc` can hand the **implementation** of a task to OpenAI Codex and have Claude only
*oversee* it — so the heavy generation runs on your Codex (ChatGPT) subscription rather
than on Anthropic tokens. This is entirely optional and requires the `codex` CLI on PATH.

## From a Claude Code session

```commands
/codex-implement-task-and-claude-review [--write] [--no-takeover] [model] <task>
```

It runs a bounded loop: an optional read-only **scout** round (plan) → Codex implements and
self-checks → Claude verifies by running the project's checks → on failure Claude gives
concrete feedback → Codex revises. If Codex still fails after round 3, Claude announces it
and takes over (unless `--no-takeover`). The **first output line is always the model**,
e.g. `model: gpt-5.6-sol (effort xhigh)`.

Two design points worth keeping:

- **Codex does the code discovery, not Claude.** Claude does not pre-read the repo to
  "build the task" — that would duplicate the reading Codex must do anyway and burn the
  very tokens this command saves. Claude supplies only intent + acceptance criteria; Codex
  (running `-C <repo>`) reads the code itself.
- **Event-driven hand-off.** Each Codex round runs in the background and the harness
  re-invokes Claude the instant it finishes — no fixed wait, no polling.

Modes:

- **Default (patch)** keeps Codex read-only: it returns a `git apply`-able diff that Claude
  applies and verifies — your global Codex read-only lockout is untouched.
- **`--write`** lets Codex edit files directly (`workspace-write`, that call only) and run
  the tests itself; Claude reviews the resulting git diff.

## The model / effort manager: `codex-in-claude.py`

One script governs the Codex **model + reasoning effort** for both the delegate command
and the adversarial `/codex-debate`. It is on PATH and called by bare name (so the repo can
move):

```commands
codex-in-claude.py models                                    # list models (* = configured)
codex-in-claude.py pick [--for debate]                       # interactive numbered picker
codex-in-claude.py set-model gpt-5.6-sol --for all           # or --for debate / delegate-review
codex-in-claude.py get-model --for debate
codex-in-claude.py set-effort high                           # low|medium|high|xhigh|default
codex-in-claude.py sync-skills [--check]                     # re-stamp the model into the help
codex-in-claude.py usage [--json]                            # Codex 5h + weekly quota
codex-in-claude.py headroom [--json]                         # learned optional-offload reserve
codex-in-claude.py delegate [--write] [--scout] -C <repo> "<task>"  # one round; prints model first
codex-in-claude.py home                                      # which CODEX_HOME (account) Codex bills now
codex-in-claude.py home -j                                   # machine-readable: {home, source, label, email, until, order, candidates, pin_active}
codex-in-claude.py home ~/.codex-private -u 2026-09-07       # pin ALL Codex use to the 2nd login until that date (inclusive)
codex-in-claude.py home -c                                   # drop the pin
```

`home` is the **account pin**: `codex_home` + `codex_home_until` in the shared config. It
moves `delegate`, `usage`/`headroom` AND `codex-review.py` (the `/codex-debate` adversary)
to that login at once and lapses by itself after the date; an explicit `$CODEX_HOME` in the
environment still overrides it. Create the second login once with
`CODEX_HOME=~/.codex-private codex login` (same value as ccc's `codex_home_private`).
The pin must point at a home **ccc knows** — `~/.codex`, `codex_home_private`, or an entry
of `codex_homes_extra`. A path outside those maps to no seat label, so the selector treats
the pin as absent and ignores it; add the login to `codex_homes_extra` first
(`codex_homes_extra = ["de=~/.codex-de"]`) and it gains its own `codex:de` quota row.

Since 2026-09-04 the pin is the WEAKEST selector: it applies only while no explicit
**seat order** is configured (see the next section). With an order set, `home <path>`
still records the pin and says so — `(pin ignored: explicit order set)` — but nothing
reads it. `$CODEX_HOME` in the environment remains the one hard override: it pins ONE
seat with no fallback at all.

`--for all` is a real reset: it moves `default` **and** clears the per-command pins, which
would otherwise shadow it. A bare `set-model <slug>` (no `--for`) only moves `default`.

## Seat order, next attempt and runtime fallback

Every Codex consumer — `delegate`, the machine `run` subcommand, ccc's own
`llm.run_codex`, `codex-review.py`, sdsc-automations' checker — tries the configured
ChatGPT logins **in one user-defined order** and **falls through at run time** when a
seat is held, exhausted, unpaid or refusing. One runner
(`codex_in_claude.run_with_fallback`) implements it, so there is one behaviour and one
set of tests. The trigger was concrete: on 2026-09-04 every `codex exec` inherited
`~/.codex` (a team seat out of credits, on an administrative hold), failed with
`codex exited 1`, and the two healthy paid logins on the same machine were never tried.

```commands
codex-in-claude order                        # the ranked table + next attempt
codex-in-claude order private de default     # set it (this also CLEARS the account pin)
codex-in-claude order -c                     # back to the canonical default → private → extras
codex-in-claude order -j                     # {configured, order, unknown, next_attempt, candidates, pin}
ai set codex-order private de default        # the front door (delegates to the above)
ai routing                                   # shows the order + the next attempt
```

```
1  private   ✅ available  5h 0% · wk 60%   you@example.org  ← next attempt
2  de        ✅ available  5h 12% · wk 3%   you.second@example.org
3  default   ⛔ blocked    hold: team seat reserved (unblocks in 2d 7h)
pin: de until 2026-09-30  (ignored: explicit order set)
```

The order is stored as `codex_seat_order` in ccc's `config.toml`, next to the seat
registry (`codex_home_private`, `codex_homes_extra`) that defines the labels. Empty (the
package default) means the canonical order `default → private → extras`. Unknown labels
are reported, never fatal; a configured login missing from the list is appended rather
than dropped. `order <labels>` refuses a label naming no login, and refuses to rewrite a
`config.toml` that carries keys ccc does not know (the writer re-emits only known keys
and would delete them) — edit the key by hand in that case.

**Eligibility** is `ccc quota`'s verdict: a seat is skipped when it is BLOCKED (a 100 %
live window, a recorded refusal, an administrative hold) or DISABLED. UNKNOWN stays
runnable — failing to measure a seat must not delete it. Two advisory signals never
change the verdict: a `free` plan (`plan free — entitlement unproven`) and a
`subscription_ends` date that has passed. `ccc quota` shows the whole ladder:

```
codex seats: 1 private ✅ → 2 de ✅ → 3 default ⛔ (hold)     next attempt: codex:private
             ⚠ private: renewal date 2026-09-30 passed · change: codex-in-claude order <label…>
```

**Run-time fallback.** The runner re-reads the candidates before every attempt (so a hold
written mid-run removes a seat), rebuilds that seat's permission profile and MCP flags,
takes a fresh `-o` file, and enforces ONE wall-clock budget across all attempts. A
refusal is classified **only** from the `codex exec --json` `error` / `turn.failed`
events — the server's `codex_error_info` code when present, else its own message against
narrow allowlists; item text and the prompt are never read, so a task that merely
mentions "rate limit" cannot look like one. When the stream carries no failure event at
all, the last 40 stderr lines are the fallback. Anything unrecognised (e.g.
`server_overloaded`) is a **task** failure: no hop, no cooldown.

| classified | recorded block                       | hops? |
| :--------- | :----------------------------------- | :---- |
| quota      | until the exhausted window resets (else 1 h) | yes |
| entitlement| 24 h, scope `entitlement`            | yes   |
| auth       | 24 h, scope `auth`                   | yes   |
| (none)     | nothing                              | no    |

A success clears that seat's OBSERVED block (never an administrative hold). **Zero
eligible seats start no process at all** and report `all_seats_unavailable` with the
earliest reset — a refusal we can predict is not worth a round trip.

**Write mode and resume.** With `--write`, a refusal hops only when codex demonstrably
did nothing: no `item.completed` other than `agent_message`/`reasoning`, and a `git
status` that is readable and unchanged. Otherwise the round is journalled and reported as
`### SEAT-REFUSED-MIDRUN <seat> — review the worktree` (exit 6); a worktree state that
could not be read counts as "changed". `--resume <id>` searches every seat's journal and
BINDS to the home that recorded the session — a resume never hops, because there is
nothing to resume on another seat.

**Environment.** `CCC_NO_CODEX=1` in the runner's own environment is the kill switch:
zero candidates, zero processes, `error.kind = disabled`. A genuinely inherited
`$CODEX_HOME` makes exactly one candidate (label `explicit` when ccc does not know that
path, and then its refusals are recorded nowhere). Consumers pass their environment
through untouched; the runner sets `CCC_NO_CODEX=1 CCC_INTERNAL=1 AI_NO_AUTOCOMMIT=1` on
the **child** only.

### `codex-in-claude run` — the machine entry point

```commands
codex-in-claude run -j -C <repo> -m gpt-5.6-sol -e low -t 300 -p debate 'reply OK'
some-tool | codex-in-claude run -j -C <repo> -n 2 -      # prompt on stdin
```

Read-only, ephemeral (`-P/--persist` keeps and journals the session), no delegate
contract and no repo map — the thinnest wrapper around the runner, for tools that would
otherwise call `codex exec` themselves. Text mode prints `model:` then `seat:` then the
reply; `-j` prints ONE JSON object:

```json
{"schema_version": 1, "model": "gpt-5.6-sol", "effort": "low", "ok": true,
 "runner_pid": 4242, "seat": {"label": "de", "id": "codex:de", "home": "…", "email": "…"},
 "attempts": [{"seat": "private", "home": "…", "elapsed_s": 3.2, "outcome": "refused:quota"},
              {"seat": "de", "home": "…", "elapsed_s": 41.0, "outcome": "ok"}],
 "reply": "…", "error": null, "session_id": "…"}
```

Exit codes match `delegate`: 0 ok, 2 usage, 3 unknown model, 4 no codex, 5 timeout/stall,
6 codex failed / refused mid-run, 8 no eligible seat (or `-n` budget spent). On failure
`error` is `{"kind", "message", "earliest_reset"}` with `kind` one of `disabled`,
`all_seats_unavailable`, `attempts_exhausted`, `codex_failed`, `timeout`, `stalled`,
`seat_refused_midrun`, `no_codex`.

`delegate` prints the same `seat: <label> (<email>)` line as its SECOND stdout line
(`[fallback]` appended on a hop), right after the guaranteed `model:` line.

### Killing a run from outside (last resort)

The runner starts codex in its own process group and sweeps that group (SIGTERM, ≤2 s,
SIGKILL) on every exit — including when codex itself already exited, which is how a
forked background child used to survive. A consumer with its own outer timeout should:

1. `SIGTERM` the runner (it relays into codex's group) and wait ≤ 5 s;
2. if it is still alive, read `codex_pgid` from the runner's heartbeat
   (`~/.config/codex-in-claude/runs/<runner_pid>.json`, refreshed every 5 s; `runner_pid`
   is also in the `-j` envelope) and `os.killpg(codex_pgid, SIGKILL)`;
3. then `SIGKILL` the runner.

Launch the runner with `start_new_session=True` so step 1 can `killpg` its group too.

### The model is visible in the slash-command help

Claude Code renders each command's one-line help from the `description:` frontmatter of its
skill/command markdown, and reads it **at session start**. So `set-model` / `set-effort` /
`pick` also stamp a `[codex <model> effort=<e>]` **prefix** into those descriptions
(`sync-skills` does it on demand; `--check` reports drift and exits 1). A prefix, because the
listing truncates long descriptions on the right. `ccc install-commands --codex` writes the
same marker into the copies it installs, so a re-install cannot silently revert it — the
shipped assets themselves stay marker-free. The stamped files are rewritten **in place**
(never temp + rename) so a dotfiles hard link to a tracked working copy survives.

`delegate` is the single engine both the skill and the slash command drive. It prints
`model: <slug> (effort <e>)` as its guaranteed first stdout line, caps simultaneous Codex
runs with a cross-process semaphore (tapered from live quota), and preflights the quota —
a run that would start ≥100% used exits with a distinct code and the reset time, *without*
launching Codex. Environment kill-switches: `CCC_NO_CODEX=1` disables all Codex use for
the session/shell.

Every launched delegate writes one before/after quota snapshot to
`~/.config/codex-in-claude/cost-history.jsonl` and prunes entries older than 90 days.
`delegate --purpose debate` labels debate rounds; after 10 valid rounds for a quota-window
duration, `headroom` reserves `3 × P95` of their measured cost plus a 10% margin (bounded
to 5–60%) instead of the 35% bootstrap. An external debate runner can use the same
`codex_cost_snapshot()` and `record_codex_run()` Python functions around its `codex exec`.

Configuration lives in `~/.config/codex-in-claude/config.json` (override with
`$CODEX_IN_CLAUDE_CONFIG`). Resolution is per-command → `default` → the latest Codex model;
the effort is a single global key. Keep the engine **read-only by default** — `--write` is
the only path that overrides Codex's global read-only lockout, per call.

## As a future job

The same selector powers **future jobs**. A draft created with `-j codex` (a `git apply`
patch Claude verifies) or `-j codex-write` (Codex edits directly) launches straight into
`/codex-implement-task-and-claude-review` when you start it — so a parked task gets done by
Codex and verified by Claude:

```commands
ccc new-job -a "add retry with backoff to the fetch client" -c work/api-gateway -j codex
```

Codex-workflow sessions are marked in `ccc` with an inverse **`OAI`** badge in the version
column (including manually-invoked ones detected from the transcript). A Codex-workflow
session that is idle while its Codex quota window is exhausted shows a `😴` status until the
window resets.
