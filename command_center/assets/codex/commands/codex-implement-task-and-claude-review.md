---
description: Delegate a task to OpenAI Codex (Codex implements; Claude only reviews/verifies) in a bounded 3-round loop — saves Anthropic tokens. Prints the Codex model first.
argument-hint: "[--write] [--no-takeover] [model] <task>"
allowed-tools: Bash, Read, Edit, Grep, Glob
---

Delegate the implementation of a task to **OpenAI Codex** and oversee it yourself. Codex
does the work; you skim, verify by running tests, give concrete feedback on failure, and
take over only if Codex still fails after 3 rounds. This **saves Anthropic tokens** — the
heavy generation runs on the Codex (ChatGPT) subscription, not on this session.

Arguments: $ARGUMENTS

Do this:

1. **Parse `$ARGUMENTS`** for optional leading flags, in any order:
   - `--write` → Codex edits files directly (`workspace-write`, this call only; the global
     read-only lockout stays). Without it, Codex stays read-only and returns a patch you apply.
   - `--no-takeover` → if Codex fails after 3 rounds, report the failures instead of
     implementing it yourself.
   - an optional **model slug or short name** (e.g. `gpt-5.5`, `sol`, `astra`) — if present, it
     overrides the configured model for this run (`codex-in-claude delegate -m <slug>`). See
     choices with `codex-in-claude models`. Everything left after the flags/model is the `<task>`.

2. **Run the `codex-implement-task-and-claude-review` skill** with that task, in the current repo
   (or the repo the task names). The skill drives the loop: (optional read-only scout) → Codex
   implements + self-checks → you verify by running the project's checks → feedback on failure →
   revise; takeover after round 3 (unless `--no-takeover`).
   **Do NOT read the codebase to "build the task" first** — Codex does the discovery itself (it
   runs `-C <repo>` and reads the repo). Reading it yourself just duplicates Codex's work and
   burns the Anthropic tokens this command exists to save. Your only input is intent + acceptance
   criteria (from this conversation) + any pointers the user already gave. Each Codex round runs in
   the **background**; the harness re-invokes you when it finishes (no waiting/polling).

3. **No Claude-side preamble — the engine announces and gates itself.** Do NOT run
   `get-model`/`get-effort`/`usage`/`command -v` before delegating: `delegate`'s guaranteed
   first stdout line is the model, e.g.:

   ```
   model: gpt-5.5 (effort xhigh)
   ```

   and its built-in quota preflight exits `8` (with the reset time in the error line) without
   launching codex when a window is ≥100% used. **Relay the `model:` line verbatim as the first
   line of your report** when the engine hands back. (The engine is `codex-in-claude`, on
   your `PATH` — call it by its bare name, never an absolute path, so it survives the repo
   moving; if it's missing the first call fails loudly.)

Notes:

- Model + reasoning effort are shared with `/codex-debate` via `codex-in-claude`. Change them
  with `codex-in-claude pick` (interactive picker), or
  `codex-in-claude set-model <slug> --for delegate-review` and
  `codex-in-claude set-effort <low|medium|high|xhigh>` (`delegate-review` is this command's
  short config key). Either way the `[codex <model> effort=<e>]` marker at the front of this
  file's `description:` is re-stamped, so the slash-command help names the model Codex will
  run (Claude Code reads descriptions at session start — it refreshes next session).
- This is a *consult-and-build* with oversight, not a blind hand-off — you always verify by
  running the tests before reporting `✅ done`.
- Billed to the Codex (ChatGPT) subscription, not Anthropic usage.
