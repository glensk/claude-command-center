---
name: ccc-mark-done-and-close
description: Mark the current ccc job/session done and close it — the hosting terminal pane/tab closes itself after this turn. Use when the user says "mark this job as done", "mark this session as done", "this job is done", "we're done here — close this session", or otherwise asks to finish AND close the current ccc job. Do NOT use for subtask/todo/file/step-level completion remarks ("this subtask is done", "mark this todo done", "done editing this file") — those never close the session. If it is ambiguous whether the user means the whole job/session, ask first instead of closing.
---

# ccc-mark-done-and-close

Mark the current ccc session done in the command center, then close the session
(and its terminal pane/tab) automatically after this turn ends.

Run exactly:

```
ccc mark-done --close -q
```

Then:

1. Report ONE short confirmation line, e.g.:
   `✓ Marked done — this session will close itself after this message.`
2. END your turn immediately — no further tool calls, no extra prose. The close
   fires only after ALL of this turn's Stop hooks (including any auto-commit
   hook) have completed; then the Claude process exits and the hosting
   pane/tab closes. If a Stop hook outlives the wait (or `ps` cannot be read),
   the close is refused and re-armed instead: it then fires after the NEXT
   turn ends (within 10 minutes of the original request), or the user closes
   the tab by hand — a desktop notification says which.

Rules:

- Only for the WHOLE current job/session. Never fire on subtask-, todo-, file-,
  or step-level "done" remarks; if unsure, ask.
- Mistake? From any shell: `ccc mark-done --undo --session <id>`, then resume
  the session (`ccc resume <id>`).
