---
name: codex-implement-task-and-claude-review
description: >-
  Delegate a coding/output task to OpenAI Codex (which does the implementation AND the
  code discovery) and oversee it yourself — a bounded loop where Codex implements +
  self-checks, you verify by running the project's checks, feed back on failure, and take
  over only if Codex still fails after round 3. Codex runs in the background and hands back
  when done. Saves Anthropic tokens. Invoked by the /codex-implement-task-and-claude-review
  command; model/effort are chosen via codex-in-claude.
---

# codex-implement-task-and-claude-review — Codex implements, Claude oversees

You hand the *implementation* to Codex and act only as the reviewer/overseer, to save
Anthropic tokens. **Codex reads the repo and writes the code; you supply intent and verify.**
The engine is one script, **`codex-in-claude`**, on your `PATH` (call it by bare name so it
survives the repo moving; if it's missing, the first call fails loudly — don't pre-verify it).

## The one rule that makes this worth doing

**Do NOT read the codebase to "prepare the task."** Codex discovers the structure itself —
it runs `codex exec -C "$REPO"` in a sandbox that reads files and runs read-only commands. If
*you* read the files first, that work is **duplicated** (Codex still has to read them to
implement) and the Claude half is exactly the tokens this command exists to save. If you catch
yourself opening file after file, stop.

What only *you* can supply (it lives in this conversation, not the repo) and therefore *must*
provide:

- the **intent + acceptance criteria** — what "done" means, in the user's words;
- any **pointers the user already gave** (forward them; don't go *find* new ones by reading);
- **constraints** — style, "don't break X", which checks to run.

That's the whole task string `$TASK`. Writing it needs zero file reads.

**Exception — surgical tasks in a big repo.** When the fix is small and its location is
known or cheaply findable, put exact pointers — file paths, line numbers, function names —
INTO `$TASK`. Discovery is what times rounds out: an xhigh round hunting for one spawn site
in a large repo can burn its whole wall clock exploring, while the same round with pointers
finishes in minutes. One targeted grep on your side to pin the site is not "reading the
codebase" — it is the cheapest token you'll spend all round. For genuinely surgical work
also consider `-e high`: xhigh mostly buys depth of exploration you just made unnecessary.
(The engine reminds you: a pointered task at config-default xhigh prints a `consider -e
high` hint on stderr.)

## Decision rules — pick BEFORE round 1 (not negotiable mid-loop)

| Situation                                        | Do                                                        |
| :----------------------------------------------- | :-------------------------------------------------------- |
| Task names exact files/lines (surgical)          | pointers in `$TASK` + `-e high`; skip the scout            |
| Big, vague, or UI/behaviour-described task       | one `--scout` round first, then implement from its PLAN    |
| Duration unknown, no real deadline (the default) | `-t 0` — the idle watchdog still guards stalls             |
| Hard deadline exists                             | keep the effort-scaled `-t` default or set one explicitly  |
| Round 2+ (feedback or post-kill retry)           | `-R <session>` + `-f` — never restart discovery from zero  |
| Repo map noisy/missing for this task             | `-P <file>` to inject a curated map, or `-M` for none      |

## Modes (from the command's flags)

- **patch** (default) — Codex is read-only; returns `### SELF-CHECK` + a git-apply-able `### DIFF`. You apply + verify.
- **`--write`** (preferred for real code tasks) — Codex edits files directly (`workspace-write`, that call only; the global read-only lockout is untouched) and **runs the tests itself**, so your verify leans on "did its checks pass" rather than reading.

## Step 0 — NO preamble: the engine announces and gates programmatically

Do **NOT** pre-check anything with separate calls (`get-model`, `get-effort`, `usage`,
`command -v`, …) — every check is built into `delegate` itself, and a Claude-side preamble is
exactly the token overhead this command exists to avoid:

- **Model announcement is programmatic.** The engine's guaranteed FIRST stdout line is
  `model: <slug> (effort <e>)` — printed before anything else, even on a quota-skip or while
  queued for a slot. Relay that line verbatim as the first line of your report when the
  background task hands back; never reconstruct it with `get-model`.
- **Quota gating is programmatic.** `delegate` runs a quota preflight and exits `8`
  (`EX_QUOTA`) WITHOUT launching codex when a live window is ≥100% used — its error line
  already contains the used % and the reset time. Concurrency is likewise usage-tapered
  automatically (see the fan-out section). You never need to check quota before delegating;
  you only *react* to an exit `8`.

Go straight to Step 1 (scout, if warranted) or Step 2 (delegate round 1).

## Step 1 — (optional) scout round for large or ambiguous tasks

If the task is big, vague, or described in UI/behaviour terms (so you're unsure Codex will aim
right), run **one read-only scout** — Codex explores and returns a *plan*, no code:

```bash
codex-in-claude delegate --scout -C "$REPO" "$TASK"   # run in BACKGROUND (see below)
```

When it returns, read the `### PLAN` (it's short prose — cheap) and either green-light it or
sharpen `$TASK`'s intent. **All code-reading stayed on Codex's side**; you only judged a plan.
Skip this for small/clearly-located tasks.

## Step 2 — the implement loop (max 3 rounds)

For `round` = 1, 2, 3:

1. **Delegate one round — in the BACKGROUND, unbounded by default:**

   ```bash
   codex-in-claude delegate -t 0 -C "$REPO" -r <round> [--write] [-f "<feedback>"] [-R <session>] "$TASK"
   ```

   Prefer **`-t 0`** (no wall limit): the ground assumption is that Codex finishes — it's just
   unclear how long it takes, and that's fine. The idle watchdog (`-i`, default 900s of total
   silence) still kills genuinely hung runs, so `-t 0` cannot hang you forever. Set a wall
   timeout only when the result has a real deadline. Run this as a **background** Bash task
   (`run_in_background: true`). **Do not sleep, poll, or guess a wait time** — a backgrounded
   task runs across turns and the harness **re-invokes you the moment Codex exits** (that IS
   the "hand back when done" signal). When re-invoked, read its captured output (the `model:`
   line + `### SELF-CHECK`, `### SESSION`, and `### DIFF`/edits).

   **Checking on a long run (infrequently, cheaply):** `codex-in-claude runs` prints one line
   per in-flight delegate — elapsed, idle seconds, output volume, last output line — from a
   tiny heartbeat file, no transcript reading. Alternatively `tail -3` the background task's
   output file (codex progress streams there as `codex›` lines). Idle near 0 = working;
   idle climbing toward 900 = about to be culled as stalled. Don't loop on it — once or
   twice on a very long run is plenty.

   **Rounds keep context:** every run ends with `### SESSION <uuid>`. Pass `-R <uuid>` on the
   next round (with `-f` feedback) so Codex resumes that session and keeps everything it
   already discovered instead of re-exploring the repo from zero. A timed-out/stalled run
   also prints its session id — resume it rather than restarting.

   The prompt is automatically prefixed with a repo map (`repo_scope_short.md`, else a git
   top-level summary; `-M` disables) so Codex starts oriented. On non-zero exit branch on
   the code: `4` codex missing/auth → tell the user `codex login`; `5` timeout/stall → read
   the streamed progress tail first, then pick ONE: `-R <session>` to resume where it
   stopped, sharpen `$TASK` with file:line pointers (if it died exploring), or drop to
   `-e high`; `6` codex error → show stderr, stop; `8` quota-exhausted → the preflight
   skipped it (didn't launch); the reset time is already in the engine's error line — relay
   it and resume after reset, do NOT retry in a loop.

2. **Verify — run the checks FIRST (this is codebase-blind):** run the project's tests + lint +
   build and read the exit codes. You do **not** need to understand the code to do this.
   - patch mode: write the `### DIFF` to a temp file, `git -C "$REPO" apply --check` then `git apply`, then run the checks.
   - write mode: just run the checks (Codex already applied + self-tested).
   - **pass** (green AND it satisfies the acceptance criteria) → **report `✅ done`** + a 2-line summary. **STOP.**
   - **fail** → step 3.
   - Only **read code** (the minimal slice — Codex's diff + the failing test output, not the
     codebase) when the checks can't settle it: there's no/weak test coverage for this change, or
     the acceptance criteria are behavioural/UI (run the app or trust Codex's self-check), or you're
     writing feedback.

3. **Feedback** — write *concrete* feedback citing the failing check/output (not "it's wrong").
   `round < 3` → loop with `-f`. `round == 3` → **takeover**.

## Fan-out across many repos/tasks — usage-tapered, ≤3 concurrent

When the work spans several repos/tasks, launch each as its own **background** `delegate`
(one per repo). The engine caps how many actually run `codex exec` at once with a
**cross-process flock semaphore whose cap is usage-tapered from the live Codex quota** — so you
may fire all of them at once and the rest **wait for a slot** (a new one starts only when a
running one finishes). This is automatic; do **not** hand-sequence them.

- **Usage-tapered cap** (from the current 5h/weekly usage, recomputed each poll): **<50% → 3,
  50–75% → 2, >75% → 1**. Ceiling = `-j/--max-concurrent N` → `$CODEX_IN_CLAUDE_MAX_CONCURRENT`
  → **3** (`0` = unlimited). Tapering means fewer in-flight runs as the wall nears → fewer
  sessions to reload after a reset (and no CPU/API thrash, which used to cause exit-5 timeouts).
- **Quota preflight / exit 8:** a `delegate` whose live usage is **≥100%** prints the reset time
  and exits `EX_QUOTA` (8) **without launching codex** (bypass `-Q`). On an `8`, **stop launching
  the rest**, relay the reset time from the engine's error line, and resume after reset.
- A queued run prints `… waiting for a Codex slot (usage-tapered cap C/N) …` to stderr, still
  emits its `model:` line first, and proceeds when a slot frees. Slots auto-release if a run dies.
- The harness re-invokes you per completion — verify + commit each repo as its task returns.

## Takeover (after 3 failed rounds)

Default: announce **`⚠️ Codex failed after 3 rounds — taking over.`**, summarize Codex's 3
attempts (what each tried, why each failed) so the spend is visible, then implement/verify/report
yourself. If invoked with **`--no-takeover`**: do not implement — report the 3 failures + last
diff and ask how to proceed.

## Guardrails

- **Codex does the discovery AND the implementation; you only state intent and verify.** Don't
  pre-read the repo, and don't re-implement before round 3 — both defeat the token-saving purpose.
- In `--write` mode, review **only Codex's** changes (the helper prints a `### CODEX-WROTE` file list).
- Never `git commit`/push from inside the loop (the helper sets `AI_NO_AUTOCOMMIT=1`); the normal
  end-of-turn workflow commits.
- Model/effort: `codex-in-claude pick` (interactive), or
  `codex-in-claude set-model <slug> --for delegate-review`,
  `codex-in-claude set-effort <low|medium|high|xhigh>`, `codex-in-claude models`. The installer
  stamps the resolved model into this file's `description:` as a `[codex …]` marker
  (`codex-in-claude sync-skills` re-stamps it) — don't hand-edit that marker.
  (`delegate-review` is this command's short config key.)
- Which ChatGPT login pays is automatic (`codex-in-claude order` shows/sets the seat order and
  falls through when one is held/exhausted/refusing) — the round's SECOND output line names the
  seat it actually used, e.g. `seat: private (<the login that paid>)`.
- Kill switch: honor a user request to skip; `CCC_NO_CODEX=1` disables the codex automation.
