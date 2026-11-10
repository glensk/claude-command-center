# Input-box fixtures (tp#1844)

Masked screen captures of a throwaway Claude Code session (`tools/composer_probe.py`, 80 columns,
iTerm2), one per input-box state. Letters are `a`, digits `9`, other characters `x`; rule and
prompt glyphs, blanks and NUL cells (cells Ink skipped over) are kept. `chars` counts the
non-dim, non-blank cells after the prompt glyph on the lines between the last two full-width
`─` rules.

| fixture | Claude Code | expected state | chars | mode | note |
|---|---|---|---|---|---|
| `empty_idle.json` | 2.1.319 | empty | 0 | prompt | box cleared after drafts |
| `fresh_placeholder.json` | 2.1.319 | empty | 0 | prompt | fresh session; this build draws no placeholder and no inverse cursor cell |
| `suggestion_ghost.json` | 2.1.319 | empty | 0 | prompt | after one short turn; no ghost suggestion was drawn in this build/config |
| `draft_one_line.json` | 2.1.319 | text | 28 | prompt | one-line draft |
| `stray_one_char.json` | 2.1.319 | text | 1 | prompt | a lone `y`; captured after a resize, so the cursor reads `y == -1` |
| `draft_multi_line.json` | 2.1.319 | text | 23 | prompt | three lines (backslash + Enter) |
| `draft_taller_than_screen.json` | 2.1.319 | text | 20 | prompt | 15-line draft in a 12-row window: the box shows only its last 3 lines, the top rule stays on screen, the status line is pushed off; cursor `y == -1` |
| `pasted_marker.json` | 2.1.319 | text | 22 | prompt | `[Pasted text #1 +19 lines]`, drawn in the default colour |
| `bash_mode.json` | 2.1.319 | text | 0 | bash | `!` replaces `❯`, rules turn pink (`rgb:fd5db1`) |
| `busy_queued.json` | 2.1.319 | empty | 0 | prompt | busy turn with one queued message: the queued text renders ABOVE the top rule (`❯` + grey `rgb:999999`), the box holds only a faint placeholder with the cursor on its first cell |
| `slash_menu.json` | 2.1.319 | text | 1 | prompt | `/` typed; the command menu renders above the box |
| `titled_rule_short.json` | 2.1.322 | empty | 0 | prompt | after `/rename` (10-char title): the top rule reads `──…── <title> ─`, 204 columns |
| `titled_rule_long.json` | 2.1.322 | empty | 0 | prompt | 102-char title with `:` and `#` in the top rule |
| `titled_rule_draft.json` | 2.1.322 | text | 16 | prompt | one-line draft under the long titled rule |

Findings: the dim rule is the `faint` flag (placeholder text inside the box, fg default); grey
`rgb:999999` marks hint/queued text outside the box and the rules are `rgb:888888`. The prompt
glyph is `❯` followed by U+00A0. There is no `#` memory mode in this build (`#` is plain text).
`cursor_coord` can point past the screen after a resize (stored as `y == -1`).

2.1.322 (tp#1989 B1b): once a session has a title (hook `sessionTitle` or `/rename`) the TOP
rule carries it right-aligned, `──…── <title> ─` (one `─` after the title, all cells in the rule
colour); the bottom rule stays plain. A titled rule counts only as the top rule right above the
box (`screenread.rule_rows`).
