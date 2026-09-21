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
codex-in-claude.py set-model astra --for debate              # short names resolve to the slug
codex-in-claude.py get-model --for debate
codex-in-claude.py get-model astra                           # what a name means (exit 3 = unknown)
codex-in-claude.py alias [<name> <slug>] [-d <name>]         # list / define / delete short names
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
(`codex_homes_extra = ["de=~/.codex-de"]`) and it gains its own quota row — id
`codex:de`, shown as `codex-de`, which is also the shell alias that opens it.

Since 2026-09-04 the pin is the WEAKEST selector: it applies only while no explicit
**seat order** is configured (see the next section). With an order set, `home <path>`
still records the pin and says so — `(pin ignored: explicit order set)` — but nothing
reads it. `$CODEX_HOME` in the environment remains the one hard override: it pins ONE
seat with no fallback at all.

`--for all` is a real reset: it moves `default` **and** clears the per-command pins, which
would otherwise shadow it. A bare `set-model <slug>` (no `--for`) only moves `default`.

**Short names.** Every model argument — `set-model`, `get-model NAME`, `delegate -m`,
`run -m`, and through the last one `codex-review.py -m`, i.e. `/codex-debate <name>` —
accepts a short name as well as a slug. The catalog's own codenames are built in (`sol` →
`gpt-5.6-sol`, `astra` → `gpt-6-astra`, `terra`, `luna`: the trailing alphabetic segment
of a VISIBLE slug, dropped when two visible slugs share it; hidden models are reachable by
slug only), and the config's `aliases` map — written by `alias <name> <slug>` — wins over
them. Names are stored resolved (the config holds slugs, never names), an unknown name
exits `3` listing the slugs and short names, and `codex-in-claude.py models` prints the
current short-name table. `/codex-debate astra` runs ONE debate on GPT-6 Astra without
touching the standing `debate` default (`sol`); `/codex-model astra debate` changes the
default itself.

## Seat policy: fill (default) or order — next attempt and runtime fallback

Every Codex consumer — `delegate`, the machine `run` subcommand, ccc's own
`llm.run_codex`, `codex-review.py`, sdsc-automations' checker — goes through one runner
(`codex_in_claude.run_with_fallback`), so "which login is billed, and what happens when
it says no" has one behaviour and one set of tests. It **falls through at run time** when
a seat is held, exhausted, unpaid or refusing. The trigger was concrete: on 2026-09-04
every `codex exec` inherited `~/.codex` (a team seat out of credits, on an administrative
hold), failed with `codex exited 1`, and the two healthy paid logins on the same machine
were never tried.

*Which* seat leads is `codex_seat_policy`:

| policy           | ranking                                                                 |
| :--------------- | :---------------------------------------------------------------------- |
| `fill` (default) | the seat whose **weekly allowance resets soonest**; `codex_seat_order` is only the tiebreak and the display order |
| `order`          | strictly `codex_seat_order` — the 2026-09-04 behaviour, kept as the escape hatch |

```commands
codex-in-claude policy                       # which policy is active, in one sentence
codex-in-claude policy order                 # switch to the strict order (fill = back)
codex-in-claude order                        # the ranked table + next attempt + reasons
codex-in-claude order private de default     # set the order/tiebreak (also CLEARS the pin)
codex-in-claude order -c                     # back to the canonical default → private → extras
codex-in-claude order -j                     # {configured, order, unknown, policy, next_attempt, candidates, pin}
ai set codex-policy fill                     # the front door (delegates to `policy`)
ai set codex-order private de default        # the front door (delegates to `order`)
ai routing                                   # shows the policy, the order and the next attempt
```

```
policy: fill — spend the seat whose weekly allowance resets soonest; codex_seat_order is the tiebreak
1  private   ✅ available  5h 0% · wk 60%   you@example.org  ·  cohort 1: weekly resets in 1d 4h · 60% used  ← next attempt
2  de        ✅ available  5h 12% · wk 3%   you.second@example.org  ·  cohort 2: weekly resets in 5d 12h · 3% used
3  default   ⛔ blocked    hold: team seat reserved (unblocks in 2d 7h)
pin: de until 2026-09-30
```

### Why "fill", and how a cohort works

The rule (2026-09-09): *"the closer a seat comes to its weekly reset, the more we must
make sure it is used up — never waste tokens."* Taken literally, "spend the seat
that resets soonest" survives the obvious counterexample. A seat at **92 % used with 2 h
to go** has 8 % about to evaporate; a seat at **0 % used with 20 h to go** loses nothing
by waiting — so the 92 % seat leads, even though it is the more-used one.

Seats whose weekly resets fall within **12 h** of each other are one **cohort**: they are
about equally urgent, so inside a cohort they are filled *equally* rather than ordered:

1. a **5-hour window renewing within the hour with ≥ 50 % unused** goes first (that
   allowance is about to be thrown away);
2. then the lowest **weekly usage, in 5 % buckets** — a 0.3 % difference must not pin
   every run onto one seat;
3. then the **oldest attempt** (`codex-seat-attempts.json`), which is what alternates two
   equal seats deterministically when two short runs leave no new measurement;
4. then the configured order.

Cohorts are ordered earliest-reset-first. Deliberately absent: any projection of a reset
that has already passed (it proves nothing about usage since), any `subscription_ends`
term (advisory only) and any `risky` demotion — the write floor below is the safety valve.

A seat with **no fresh weekly reading is UNMEASURED** and ranks last, with one exception:
one unmeasured seat per ranking is promoted to a single read-only **probe**, at most once
per 24 h per seat, claimed under a lock (`quota.claim_probe`) so two concurrent runners
never probe the same seat. That is how a brand-new login gets measured at all.

### The account pin, and enrolling a seat

Under `fill` an active pin leads — the order is only a tiebreak, so it cannot outrank an
explicit "use this seat". Under `order` an explicit `codex_seat_order` makes the pin inert
(two competing "use this seat" knobs is how a run ends up on a seat nobody chose).

Either way the pinned path **must be a registered seat**: one of `~/.codex`,
`codex_home_private`, or a `codex_homes_extra` login. `codex-in-claude home <path>`
refuses anything else and prints the key that enrolls it; a pre-existing unregistered pin
is reported and ignored. An unregistered home has no `codex:<label>` quota row, so its
refusals could be recorded nowhere and its usage ranked never — while the pin would
outrank three seats that ARE measurable.

The order is stored as `codex_seat_order` in ccc's `config.toml`, next to the seat
registry (`codex_home_private`, `codex_homes_extra`) that defines the labels. Empty (the
package default) means the canonical order `default → private → extras`. Unknown labels
are reported, never fatal; a configured login missing from the list is appended rather
than dropped. `order <labels>` and `policy <value>` refuse a label naming no login / an
unknown policy, and refuse to rewrite a `config.toml` that carries keys ccc does not know
(the writer re-emits only known keys and would delete them) — edit the key by hand then.

**Eligibility** is `ccc quota`'s verdict: a seat is skipped when it is BLOCKED (a 100 %
live window, a recorded refusal, an administrative hold) or DISABLED. UNKNOWN stays
runnable — failing to measure a seat must not delete it. Two advisory signals never
change the verdict: a `free` plan (`plan free — entitlement unproven`) and a
`subscription_ends` date that has passed. A refusal stapled from a rollout file **expires**
once its exhausted window's reset has passed (or after 5 h when no window is known), so a
week-old refusal can no longer hold a paid seat out of the ladder for good; the row then
falls back to its windows and is remeasurable.

**A recorded refusal yields to a newer healthy reading** (2026-09-14). The seat's own usage
is read BEFORE the cooldown store, and an `observed` entry whose `scope` is exactly `quota`
is superseded when that reading is newer than the refusal, carries no block of its own, is
well-formed and folds to `available`. The row then reports what the seat says today and its
`note` names what it overrode (`refusal 3d old superseded by a reading 2h old`). Until this,
`ai routing` printed `blocked observed-rejection: codex exec refused: quota… (unblocks in
18h 13m)` for three days while ccc's own usage card for the same login read `Session: idle
0% · Week: idle`. The narrowings are deliberate: an administrative `hold` is never
superseded (it is policy, not a measurement), `auth`/`entitlement`/scope-less blocks say
things a usage reading cannot refute, and stale, absent or malformed windows are `unknown`,
which is a measurement failure rather than evidence. Readers never write, so the entry stays
in `cooldowns.json` until its own deadline — and one healthy measurement buys AT MOST one
new attempt, because the runner records its next refusal with an `observed_at` newer than
that measurement. `ccc quota` shows the whole ladder, tagged with the policy that ranked it:

```
codex seats [fill]: 1 private ✅ → 2 de ✅ → 3 default ⛔ (hold)     next attempt: codex-priv
                    ⚠ private: renewal date 2026-09-30 passed · change: codex-in-claude order <label…>
```

`ccc quota -j` carries the same, machine-readable: a top-level `codex_seat_policy`, and
per `codex_seat_order` row the additive `cohort`, `measured`, `probe`, `rank_reason` and
`malformed`. `codex_pin` appears whenever the pin actually governs.

### Sharing a seat on a weekly rota

One ChatGPT login often belongs to two people on alternating weeks. An administrative hold
(`ccc quota -m codex -H -U …`) reserves it exactly once — somebody has to re-arm it every
Monday, and the week nobody does is the week ccc bills a colleague's seat. `codex_seat_rota`
turns that into a COMPUTED block: during the other person's week the seat is simply not a
candidate, and on Monday 00:00 it comes back by itself.

```toml
codex_seat_rota    = ["default=2026-09-14@Europe/Zurich:alice,bob"]
codex_seat_rota_me = "bob"
```

An entry is `label=YYYY-MM-DD@IANA_ZONE:name,name[,name…]`. The date is the **Monday** of the
week the FIRST name holds the seat, and the names take turns week by week in that order,
forever — forwards AND backwards from that Monday, so a rota set in March still answers for
last week. The zone is **required**: weeks are the aware intervals `[Monday 00:00, next
Monday 00:00)` in THAT zone, never the process's, because a laptop that travels (or another
consumer running under a different `TZ`) must not move the day a seat changes hands. Names
match `^[a-z0-9][a-z0-9_-]*$`, at least two, no duplicates. `codex_seat_rota_me` says which
of them this machine is; membership is decided PER SEAT, so a rota that does not list you is
never yours.

```commands
codex-in-claude rota                                    # whose week is it, and who is next
codex-in-claude rota me bob                             # which name THIS machine is
codex-in-claude rota set default -s 2026-09-14 alice bob   # -z defaults to this machine's zone
codex-in-claude rota set default -s 2026-09-14 -z Europe/Zurich alice bob
codex-in-claude rota clear default                      # the seat is ours again, always
codex-in-claude rota show -j                            # {schema_version, me, seats, errors}
```

```
me: bob
default    14.9.–20.9. used by alice  ·  yours from Mon 21.9.
```

**What the block does and does not override.** It is ABSOLUTE for automation: the ranking
under `fill` and `order`, the account pin, the daily probe, `headroom`, `-Q/--ignore-quota`,
an explicit **registered** `$CODEX_HOME` and a journal resume all refuse the seat
(`skipped:rota`; with nothing else eligible the runner starts no process and reports
`all_seats_unavailable`, naming the rota). `-Q` deliberately does not waive it: that flag
accepts a REFUSAL, and no amount of accepting refusals makes billing somebody else's week
ours. The only overrides are a human `/switch <seat>!` — whose note then records what it
overrode, `forced past ccc's seat oracle on 'default' (rota: 14.9.–20.9. used by alice)` —
and editing the config. Documented exception: an UNREGISTERED explicit `$CODEX_HOME` has no
label, so no rota can name it.

**In the TUI the seat's card folds away for that week.** A blocked seat's usage card
would spend the week showing bars nothing may spend, so it collapses to its own title
line and that line carries the block: `╭─ t3:⛔Codex op.ac@example.org ─╮`. Nothing is
persisted — the card's `usage_card_codex` / `usage_card_codex_private` /
`usage_card_codex_extra_collapsed` gate is left exactly as you set it and decides again
the Monday the seat is ours, which is also why the week costs no config write. The card's
own chord (`t3`/`t5`/`t6`…`t8`) still opens it for the CURRENT view when you want to look,
and closes it again; the `t` menu names that state (`collapsed (rota week)`). Set
`usage_card_codex_rota_collapse = false` to render rota weeks like any other — the ⛔ in
the title stays either way, because it reports a fact rather than a behaviour.

**Unusable rota ⇒ the seat is BLOCKED, not free.** An entry naming a configured seat that
ccc cannot read (bad date, not a Monday, missing/unknown zone, fewer than two names, a
duplicate, a bad name) blocks that seat with `reason = "rota: invalid entry (<why>)"`, and so
does a valid entry whose names do not contain `codex_seat_rota_me` (`"rota:
codex_seat_rota_me 'carol' is not one of alice,bob"`). Failing open would resolve "we cannot
tell whose week it is" to "ours", which is the one answer that costs a colleague their week.
An entry naming no configured seat is reported and ignored — there is no seat to block. Every
parse problem appears in `ccc quota -j` as `codex_seat_rota_errors: [{entry, label, error}]`
and once on stderr, and `codex-in-claude rota` lists them; every `rota` verb keeps working
while one entry is unreadable, so a broken rota never locks you out of the command that
repairs it.

**In the payloads.** `ccc quota -j` carries a `rota` object per `codex_seat_order` row
(`null` for a seat on no rota): `holder`, `mine`, `me`, `names`, `anchor`, `tz`,
`week_start`, `week_end_exclusive`, `label` (`14.9.–20.9. used by alice`), `next_mine_at` /
`next_mine_label`, `next_holder`, `next_other_at` / `next_other_label`, and — while the seat
is somebody else's — `underlying` (`state`, `blocked_by`, `reason`, `resets_at`,
`resets_label`), the verdict the rota wrapper replaced. The two `*_label` fields are rendered
in the ENTRY's zone so no consumer re-interprets an epoch in its own and prints a Sunday.
`resets_at` on a rota-blocked row is `max(our next Monday, the underlying block's own reset)`:
a hold that outlasts our next week is not promised away.

### The offload gate is asked of the whole seat pool

`codex-in-claude headroom` answers "may optional work be offloaded to Codex at all"
(CLAUDE.md's Codex offload gate; debates deliberately bypass it). Since 2026-09-09 it
evaluates **every eligible seat**, not one home's rollout files — the bug it fixes is
exact: a completely fresh team seat at 0 %/0 % was invisible, so the gate answered
`DENIED (unknown — newest rate_limits event is older than 6h)` while a whole paid seat sat
idle. Each seat is judged from the same quota row the ranking used (live snapshot +
rollout + cooldowns), and the pool takes the best verdict: any `allowed` allows and names
that seat, else `reserve`, else `unknown`, else `blocked`.

```
seat: default (openai.account@example.org)
5h: 0% used, resets in 4h 12m, reserve 35% (reserve_source bootstrap), ALLOWED
7d: 0% used, resets in 5d 3h, reserve 35% (reserve_source bootstrap), ALLOWED
offload: ALLOWED
private: blocked — included usage limit reached (no credit overflow)
de: reserve — at least one live window is inside its reserve
```

Exit codes are unchanged (0 allowed, 1 reserve/blocked, 3 unknown) and the JSON payload is
additive: `seat` (the label the verdict is about) and `seats[]` (every seat's own verdict).
It still fails **closed** per seat — no fresh window, a snapshot older than 6 h, or a
window whose duration could not be read are all `unknown`. Routing itself keeps failing
**open** on the same row; the asymmetry is the point.

The same verdict is available as an EXECUTION mode: `run -H` / `delegate -H` filter every
candidate through it before each attempt and record `skipped:reserve` / `skipped:unknown`
instead of billing the seat.

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
codex-in-claude run -H -C <repo> 'reply OK'             # only seats outside their reserve
codex-in-claude delegate -w -F 15 -C <repo> 'fix it'    # write: need 15% left on every window
codex-in-claude run -E -C <repo> 'reply OK'             # force --ephemeral (leaves no measurement)
```

Read-only, no delegate contract and no repo map — the thinnest wrapper around the runner,
for tools that would otherwise call `codex exec` themselves. Text mode prints `model:`
then `seat:` then the reply; `-j` prints ONE JSON object:

```json
{"schema_version": 1, "model": "gpt-5.6-sol", "effort": "low", "ok": true,
 "runner_pid": 4242, "seat": {"label": "de", "id": "codex:de", "home": "…", "email": "…"},
 "attempts": [{"seat": "private", "home": "…", "elapsed_s": 3.2, "outcome": "refused:quota"},
              {"seat": "de", "home": "…", "elapsed_s": 41.0, "outcome": "ok"}],
 "reply": "…", "error": null, "session_id": "…"}
```

Exit codes match `delegate`: 0 ok, 2 usage, 3 unknown model, 4 no codex, 5 timeout/stall,
6 codex failed / refused mid-run, 8 no eligible seat (or `-n` budget spent), 9 network
dead / machine slept (killed after codex stopped making progress). On failure `error` is
`{"kind", "message", "earliest_reset"}` with `kind` one of `disabled`,
`all_seats_unavailable`, `attempts_exhausted`, `codex_failed`, `timeout`, `stalled`,
`network`, `slept`, `startup_timeout`, `seat_refused_midrun`, `no_codex`.

`delegate` prints the same `seat: <label> (<email>)` line as its SECOND stdout line
(`[fallback]` appended on a hop), right after the guaranteed `model:` line.

**Session files are the measurement.** The `codex exec --json` stream carries no
`rate_limits` event at all (verified live 2026-09-09), so an ephemeral run leaves nothing
that says what it cost the seat it just billed — and the `fill` policy needs that to route
the next one. So while `codex_usage` is **off**, `run` and `llm.run_codex` keep their
session file (no `--ephemeral`) and the rollout's `rate_limits` block does the measuring;
they stay UNJOURNALLED either way (only `-P/--persist` journals, which is what `--resume`
needs). With `codex_usage` **on** they are ephemeral again and the runner fetches the live
figures itself: every stale candidate once before selection and the attempted seat once
after, inside hard budgets (20 s aggregate, 15 s per fetch, and no post-attempt fetch
without 15 s of the call's budget left) and best-effort throughout. `run -E/--ephemeral`
forces the old behaviour back.

**Write runs hold a floor.** A write round that dies half-way leaves a worktree to review
(`### SEAT-REFUSED-MIDRUN`), so `delegate --write` refuses to START on a seat that is one
round from empty: an UNMEASURED seat is skipped (`skipped:unmeasured` — the floor cannot be
checked on it; `-F 0` waives the floor and that skip together, which is how a fresh install
with no usage data anywhere still starts its first write run), and so is any seat
with less than `-F/--min-remaining` percent left on a live window (default: the learned P95
cost of one round once ten debate samples exist, else 10 %; `-F 0` disables it). Write runs
also never promote a probe — an unmeasured seat is a read-only experiment. A `--resume`
binds to exactly one seat, so it gets NO floor and no headroom filter: there is no second
seat to move to, and the documented trade-off is a possible `SEAT-REFUSED-MIDRUN` review.

### The run ledger — `codex-runs.jsonl`

Every round that goes through the runner (`run`, `delegate`, the short-AIM helper — every
Codex call in ccc) appends one line per **physical** attempt to
`~/.claude/command-center/codex-runs.jsonl` once it has returned (`codex_ledger.py`,
2026-09-21). A skipped seat is not spend and is not written; a refusal IS a round trip the
seat was asked to bill and is written with its outcome. Each line carries the seat label
and oracle id, the caller's `-p` purpose, `outcome`/`ok`, wall `ms`, model and effort, the
prompt size, the `usage` codex reported on `turn.completed` (`tokens_in`/`tokens_out`),
the working directory, the Claude session (`CLAUDE_CODE_SESSION_ID`) and, on the last
attempt of a failed round, the runner's message — enough to answer *"did this Mac spend
the shared seat at 10:33, and on what?"*, which before the ledger only file mtimes could,
because an `--ephemeral` run leaves no rollout and `codex-seat-attempts.json` keeps one
stamp per seat.

```json
{"ts":"2026-09-21T10:33:39+02:00","seat":"default","id":"codex","purpose":"checker",
 "outcome":"ok","ok":true,"ms":6100,"model":"gpt-5.6-sol","effort":"xhigh",
 "prompt_chars":4321,"write":false,"cwd":"/…/sdsc-automations","session":"",
 "tokens_in":15175,"tokens_out":5}
```

Two readers: **`ccc quota`** stamps each Codex row with its `last_run` (`ts`, `age_s`,
`purpose`, `outcome`, `ok`, `ms`, `runs_24h`) and prints it in the windows column —
`last run 58m ago · checker · 6s (3 in 24h)`, a failed attempt suffixed with its outcome —
and publishes the file's path as `llm_runs_log` on `-j` (plus the first-day alias
`codex_runs_log`, same path); **`ai logs`** (ai.py) reads that path and merges the lines
under the seat names it already prints, so `ai logs codex-work` lists them next to ai.py's
own calls. Append-only and best-effort: an unwritable ledger never fails or delays the
Codex call it would have described.

`run -N/--note TEXT` puts the caller's context on every attempt of the round (`"note":
"#255"` — the sdsc-automations checker names the ticket it is judging; whitespace and
control characters collapsed, 120 chars at most), and `ai logs` prints it in the detail
column instead of the seat, which the provider column already names.

**Claude rows — `ccc record-run`.** The same file is the ledger for the headless
`claude -p` turns an external caller wants next to its Codex ones (the checker pair judges
every ticket command with Opus AND Codex; `ai logs` used to show only the Codex half).
The caller pipes ONE JSON object or an array of them — one per physical attempt — to
`ccc record-run` (`-f/--file PATH` instead of stdin, `-q` for no confirmation line):

```json
{"schema_version": 1, "provider": "claude", "seat": "work", "purpose": "checker",
 "note": "#255", "requested_model": "opus", "model": "claude-opus-5",
 "outcome": "ok", "ok": true, "ms": 6100, "llm_ms": 5400, "prompt_chars": 34000,
 "tokens_in": 12, "tokens_out": 40, "tokens_cache_read": 30000,
 "cwd": "/…/sdsc-automations"}
```

`provider` is `codex` | `claude` (a row without the key predates the field and is Codex);
`seat` is the caller's label (`work` / `private`), `id` defaults to the ccc oracle id
(`claude:work`); `requested_model` is the alias handed to `--model` (`opus`) and `model`
the one Claude Code resolved it to; `outcome` is free text with `ok` the verdict
(`usage-limit`, `timeout`, `error:exit1`, …). The verb is STRICT — it never claims a
record it did not make: exit 0 only when every row was appended, 2 for malformed input
(the whole batch is validated first, nothing is written), 1 when the append failed. The
"telemetry must not break the call" policy is the caller's: it wraps the subprocess with a
timeout and ignores a non-zero exit. Claude rows never stamp a Codex row's `last_run`, and
the file keeps its `codex-runs.jsonl` name because a long-lived older runner process may
still append to it — a rename would split the history.

### Progress watchdog and the sleep guard

Why: on 2026-09-07 an unattended debate round hung seven hours — the laptop idle-slept
seven minutes after launch (one minute after the display-on assertion dropped: `pmset -g
custom` → `sleep 1`, and the runner held nothing) and the woken codex 0.152.1 looped on a
dead connection all morning, too loudly for the old any-output watchdog to notice.

**Progress** — the only thing that resets the idle clock — is a non-`error`
`codex exec --json` event on stdout (`item.*` / `turn.completed` / `turn.failed` also
count as *work*: proof the model answered), a stderr line that is not an `ERROR`/`WARN`
log line (`Reading additional input from stdin...`), or a stdout line that is not JSON at
all (unknown output is never a reason to kill). **Trouble** is codex's own `error` events
(`{"type":"error","message":"Reconnecting... waiting for network (Connection failed:
error sending request)"}` — the seven-hour stream), an `item` of type `error` (`Falling
back from WebSockets to HTTPS transport…`), and `ERROR`/`WARN` `tracing` logs on stderr
(`… ERROR codex_models_manager::manager: failed to refresh available models: …`).

Allowances are AWAKE seconds: 900 s of no progress normally (`-i/--idle-timeout`, clamped
to the wall), 120 s once codex names the network, 180 s after the machine was suspended,
240 s for the first model response (`thread.started` / `turn.started` are emitted locally
and prove nothing). A 1 s supervision tick that took more than 30 s means the machine
slept — the gap is charged to nothing (a tick credits at most 5 s of awake time), so
suspended time eats no allowance, it only shrinks the one that applies after the wake.
Knobs: `CODEX_IN_CLAUDE_NET_IDLE`, `CODEX_IN_CLAUDE_POST_SLEEP_IDLE`,
`CODEX_IN_CLAUDE_STARTUP_TIMEOUT` (`0` disables each); `-i 0` disables every idle-based
kill. A kill is reported as `network`, `slept` or `startup_timeout` with its own cause and
fix, so a transcript says "the laptop slept" instead of "Codex failed".

On macOS the runner holds `caffeinate -i -w <codex pid>` for exactly the run and releases
it on every exit path; other `caffeinate` holders are neither reused nor touched. `-i`
blocks IDLE sleep only — a closed lid or a battery sleep still suspends the round, which
is what the `slept` watchdog is for. Opt out with `CODEX_IN_CLAUDE_NO_CAFFEINATE=1`; a
platform without `caffeinate` gets none. The heartbeat's `idle_s` is now awake seconds
since the last progress line, beside `slept_s`, `trouble` (trouble lines since the last
progress) and `caffeinate_pid`, and `runs` appends `· caffeinated`, `· slept 2m05s` and
`· 3 trouble line(s) since progress` to the row — a quiet round explains itself.

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
