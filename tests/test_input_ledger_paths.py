"""Every prompt-injecting call site is ledgered or exempted with a reason (tp#2170 D8, O6).

The input ledger (:mod:`command_center.input_ledger`) is only worth anything while EVERY
path that types or launches text into a Claude session writes its row. This test scans
``command_center/`` (AST, never regex, never live state) for calls of the typing and
launching primitives and checks each call site — ``(module, function, callee)`` with its
call count — against :data:`SITES`. A new typing site, or another call in a known
function, fails here until it is ledgered (a D2 kind) or exempted with its reason, so the
ledger's coverage cannot silently regress.

Verdicts:

- a D2 kind (``send``, ``park_now`` …): the site is ledgered with that kind. Checked: the
  kind literal appears in the module, a ``guarded_paste`` / ``_exec_in`` call carries
  ``ledger=``, and any other site's module calls ``input_ledger.record`` / ``record_exec``.
- ``choke: <reason>``: the choke point that records the kind its CALLER passes in a
  ``LedgerSpec``; checked: the function itself calls ``record`` / ``record_exec``.
- ``exempt: <reason>``: no Claude prompt candidate (no prompt, a shell line, a Codex tab,
  a ``tool_result``) or an inner primitive whose every caller is itself a scanned site.
- ``gap: <reason>``: a prompt path known NOT to be ledgered yet; listed so it stays
  visible until a follow-up ledgers it.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent / "command_center"

#: The prompt-injecting primitives: D8's list, the raw primitives and new-tab typers of
#: debate O6 (``async_send_text``, ``_iterm``, ``_open_tab``, ``_tmux_window``,
#: ``claude_command``), the remaining iTerm2 typing rungs, and the ``**kwargs`` forwarder
#: ``cli._profile_argv`` (a ``prompt=`` passed through it would reach ``claude_argv``
#: unseen). Matched by call name, on any receiver.
PRIMITIVES = frozenset(
    {
        "send_text",
        "send_text_detailed",
        "send_text_via",
        "send_text_to_session",
        "_send_text_python_api",
        "_send_text_applescript",
        "send_keys",
        "send_keys_via",
        "type_into_iterm_session",
        "tmux_send_keys",
        "guarded_paste",
        "_exec_in",
        "relaunch_command",
        "claude_argv",
        "claude_command",
        "_profile_argv",
        "async_send_text",
        "_iterm",
        "_iterm_api_tab",
        "_open_tab",
        "_tmux_window",
    }
)

#: Design D2's kinds (one per ledgered path P1-P10, plus ``resume_prompt``: B3d).
KINDS = frozenset(
    {
        "send",
        "answer",
        "park_now",
        "attached_paste",
        "attached_resume",
        "await_paste",
        "await_resume",
        "start_job",
        "switch_resume",
        "limit_resume",
        "resume_prompt",
    }
)

_LEDGER_WRITERS = frozenset({"record", "record_exec"})
_LEDGER_CARRIERS = frozenset({"guarded_paste", "_exec_in"})

_INNER = "inner primitive: every caller of {} is itself a scanned site"
_NO_PROMPT = "builds a claude argv/command without a prompt"

Key = tuple[str, str, str]

#: ``(module, function, callee) -> (number of calls, verdict)``; module relative to
#: ``command_center`` (dotted), function the enclosing qualname (``<module>`` at top level).
SITES: dict[Key, tuple[int, str]] = {
    # P1 `ccc send`: the row is written right before `deps.send_text`.
    ("bridge_send", "run_send", "send_text"): (1, "send"),
    # P2 `ccc answer` (debate R1, A9).
    ("bridge_answer", "run_answer", "send_keys"): (
        1,
        "exempt: P2 answer keys produce the call's tool_result, never a prompt candidate",
    ),
    # P3/P4/P6: callers of the guarded_paste choke point, each with its LedgerSpec.
    ("park", "_park_now", "guarded_paste"): (1, "park_now"),
    ("daemon", "_default_attached_paster", "guarded_paste"): (1, "attached_paste"),
    ("await_delivery", "_default_paster", "guarded_paste"): (1, "await_paste"),
    ("bridge_target", "guarded_paste", "send_text"): (
        1,
        "choke: P3/P4/P6 record the caller's LedgerSpec kind inside the tab lock",
    ),
    ("bridge_target", "_default_send_text", "send_text_detailed"): (
        1,
        "exempt: default of the deps.send_text seam; every deps.send_text call is scanned",
    ),
    ("bridge_target", "_default_send_keys", "send_keys_via"): (
        1,
        "exempt: default of the deps.send_keys seam (P2 answer keys); its call is scanned",
    ),
    # P5/P7/P8: callers of the _exec_in(stdin_prompt=…) choke point.
    ("cli", "cmd_fire_attached", "_exec_in"): (1, "attached_resume"),
    ("cli", "cmd_fire_await", "_exec_in"): (1, "await_resume"),
    ("cli", "cmd_start_job", "_exec_in"): (1, "start_job"),
    ("cli", "cmd_resume", "_exec_in"): (
        1,
        "exempt: `ccc resume` execs claude --resume <id> with no stdin prompt",
    ),
    ("cli", "cmd_fire_attached", "_profile_argv"): (
        1,
        "exempt: argv without a prompt; the prompt travels on stdin via the ledgered _exec_in",
    ),
    ("cli", "cmd_resume", "_profile_argv"): (1, "exempt: " + _NO_PROMPT),
    ("cli", "_profile_argv", "claude_argv"): (
        1,
        "exempt: **kwargs forwarder; every _profile_argv call is itself a scanned site",
    ),
    ("cli", "cmd_fire_await", "claude_argv"): (
        1,
        "exempt: argv without a prompt; the prompt travels on stdin via the ledgered _exec_in",
    ),
    ("cli", "cmd_start_job", "claude_argv"): (
        1,
        "exempt: argv without a prompt; the prompt travels on stdin via the ledgered _exec_in",
    ),
    # P9 `ccc switch-now`: the relaunch line carries claude's positional first prompt.
    ("cli", "cmd_switch_now", "relaunch_command"): (1, "switch_resume"),
    ("cli", "cmd_switch_now", "tmux_send_keys"): (1, "switch_resume"),
    ("cli", "cmd_switch_now", "type_into_iterm_session"): (1, "switch_resume"),
    ("accounts", "relaunch_command", "claude_argv"): (
        1,
        "exempt: builder of the P9 line; every relaunch_command call is a scanned site",
    ),
    ("accounts", "relaunch_command", "claude_command"): (
        1,
        "exempt: builder of the P9 line; every relaunch_command call is a scanned site",
    ),
    # P10 `claude-session-continue`: `_ledger_record` writes the row before the exec.
    ("session_continue", "build_command", "claude_argv"): (1, "limit_resume"),
    # Not a Claude prompt.
    ("codex_switch", "run_switch_now.deliver", "tmux_send_keys"): (
        1,
        "exempt: /quit and the codex-switch-exec trampoline into a Codex TUI tab, not Claude",
    ),
    ("codex_switch", "run_switch_now.deliver", "type_into_iterm_session"): (
        1,
        "exempt: /quit and the codex-switch-exec trampoline into a Codex TUI tab, not Claude",
    ),
    ("continue_session", "_dry_run", "claude_argv"): (
        1,
        "exempt: --dry-run only prints the argv; the real launch is the ledgered P8 start-job",
    ),
    ("snapshot", "_claude_action", "claude_argv"): (
        1,
        "exempt: snapshot restore resumes claude --resume <id> without a prompt",
    ),
    ("snapshot", "_claude_action", "claude_command"): (
        1,
        "exempt: snapshot restore resumes claude --resume <id> without a prompt",
    ),
    ("snapshot", "_deliver", "async_send_text"): (
        1,
        "exempt: snapshot restore types the pane's shell line (cd / resume, no prompt)",
    ),
    # terminal.py: the new-tab typers type SHELL lines; prompts travel through P5/P7/P8/P10.
    ("terminal", "resume_in_new_tab", "claude_argv"): (1, "exempt: " + _NO_PROMPT),
    ("terminal", "resume_in_new_tab", "claude_command"): (3, "resume_prompt"),
    ("terminal", "resume_in_new_tab", "_tmux_window"): (
        1,
        "exempt: types the shell line built by claude_command (its row covers the prompt)",
    ),
    ("terminal", "resume_in_new_tab", "_open_tab"): (
        1,
        "exempt: types the shell line built by claude_command (its row covers the prompt)",
    ),
    ("terminal", "resume_halted_in_new_tab", "_tmux_window"): (
        1,
        "exempt: shell line `<continue script> <id> now`; the prompt is ledgered at P10",
    ),
    ("terminal", "resume_halted_in_new_tab", "_open_tab"): (
        1,
        "exempt: shell line `<continue script> <id> now`; the prompt is ledgered at P10",
    ),
    ("terminal", "start_job_launch", "_tmux_window"): (
        1,
        "exempt: shell line `ccc start-job <id>`; the prompt is ledgered at P8",
    ),
    ("terminal", "start_job_launch", "_open_tab"): (
        1,
        "exempt: shell line `ccc start-job <id>`; the prompt is ledgered at P8",
    ),
    ("terminal", "fire_attached_in_new_tab", "_tmux_window"): (
        1,
        "exempt: shell line `ccc fire-attached <id>`; the prompt is ledgered at P5",
    ),
    ("terminal", "fire_attached_in_new_tab", "_open_tab"): (
        1,
        "exempt: shell line `ccc fire-attached <id>`; the prompt is ledgered at P5",
    ),
    ("terminal", "fire_await_in_new_tab", "_tmux_window"): (
        1,
        "exempt: shell line `ccc fire-await <group> <token>`; the prompt is ledgered at P7",
    ),
    ("terminal", "fire_await_in_new_tab", "_open_tab"): (
        1,
        "exempt: shell line `ccc fire-await <group> <token>`; the prompt is ledgered at P7",
    ),
    ("terminal", "probe_launch", "_tmux_window"): (
        1,
        "exempt: `ccc terminal-probe` prints a marker; no Claude session",
    ),
    ("terminal", "probe_launch", "_open_tab"): (
        1,
        "exempt: `ccc terminal-probe` prints a marker; no Claude session",
    ),
    ("terminal", "focus_tmux_window", "_open_tab"): (
        1,
        "exempt: opens a `tmux attach` tab; no Claude prompt",
    ),
    ("terminal", "launch_ccc_tab", "_open_tab"): (1, "exempt: opens the ccc TUI; no Claude prompt"),
    ("terminal", "_open_tab", "_iterm"): (1, "exempt: " + _INNER.format("_open_tab")),
    ("terminal", "_open_tab", "_iterm_api_tab"): (1, "exempt: " + _INNER.format("_open_tab")),
    ("terminal", "_open_tab", "_tmux_window"): (1, "exempt: " + _INNER.format("_open_tab")),
    ("terminal", "_iterm_api_tab._go", "async_send_text"): (
        1,
        "exempt: " + _INNER.format("_iterm_api_tab"),
    ),
    ("terminal", "_send_text_python_api._go", "async_send_text"): (
        2,
        "exempt: " + _INNER.format("_send_text_python_api"),
    ),
    ("terminal", "send_text_detailed", "_send_text_python_api"): (
        1,
        "exempt: " + _INNER.format("send_text_detailed"),
    ),
    ("terminal", "send_text_detailed", "_send_text_applescript"): (
        1,
        "exempt: " + _INNER.format("send_text_detailed"),
    ),
    ("terminal", "send_text_via", "send_text_detailed"): (
        1,
        "exempt: " + _INNER.format("send_text_via"),
    ),
    ("terminal", "send_text_to_session", "send_text_via"): (
        1,
        "exempt: " + _INNER.format("send_text_to_session"),
    ),
    ("terminal", "type_into_iterm_session", "send_text_to_session"): (
        1,
        "exempt: " + _INNER.format("type_into_iterm_session"),
    ),
    ("terminal", "send_keys_via._go", "async_send_text"): (
        1,
        "exempt: " + _INNER.format("send_keys_via"),
    ),
}


def _callee(call: ast.Call) -> str | None:
    """The called name (``f(...)`` or ``x.f(...)``), or None for other call shapes."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _Site:  # pylint: disable=too-few-public-methods
    """One scanned call of a primitive."""

    def __init__(self, module: str, function: str, call: ast.Call, callee: str) -> None:
        self.module = module
        self.function = function
        self.call = call
        self.callee = callee

    @property
    def key(self) -> Key:
        """``(module, function, callee)``."""
        return (self.module, self.function, self.callee)


class _Module:  # pylint: disable=too-few-public-methods
    """A parsed module: its primitive call sites and the facts the verdict checks need."""

    def __init__(self, name: str, tree: ast.Module) -> None:
        self.name = name
        self.sites: list[_Site] = []
        self.literals: set[str] = set()
        self.writers: set[str] = set()  # functions that call record / record_exec
        self._walk(tree, "")

    def _walk(self, node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = qual
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{qual}.{child.name}" if qual else child.name
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                self.literals.add(child.value)
            if isinstance(child, ast.Call):
                name = _callee(child)
                if name in PRIMITIVES:
                    self.sites.append(_Site(self.name, qual or "<module>", child, name))
                if name in _LEDGER_WRITERS:
                    self.writers.add(qual or "<module>")
            self._walk(child, inner)


def scan(package: Path) -> list[_Module]:
    """Parse every ``*.py`` under *package* (static: nothing is imported)."""
    modules = []
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(package).with_suffix("")
        name = ".".join(rel.parts)
        modules.append(_Module(name, ast.parse(path.read_text(encoding="utf-8"), str(path))))
    return modules


def _verdict_problem(site: _Site, module: _Module, verdict: str) -> str | None:
    """Why *verdict* does not hold for *site*, or None when it does."""
    prefix, _, reason = verdict.partition(": ")
    if prefix in {"exempt", "gap"}:
        return None if len(reason.strip()) >= 15 else "exemption without a real reason"
    if prefix == "choke":
        if site.function not in module.writers:
            return "choke point does not call input_ledger.record/record_exec itself"
        return None
    return _kind_problem(site, module, verdict)


def _kind_problem(site: _Site, module: _Module, verdict: str) -> str | None:
    """Why the ledgered-kind *verdict* does not hold for *site*, or None when it does."""
    if verdict not in KINDS:
        return f"verdict {verdict!r} is neither a D2 kind nor exempt:/gap:/choke:"
    if verdict not in module.literals:
        return f"kind {verdict!r} never appears in module {module.name}"
    if site.callee in _LEDGER_CARRIERS:
        if not any(kw.arg == "ledger" for kw in site.call.keywords):
            return f"{site.callee} call without ledger="
    elif not module.writers:
        return f"module {module.name} never calls input_ledger.record/record_exec"
    return None


def violations(modules: list[_Module], table: dict[Key, tuple[int, str]]) -> list[str]:
    """Every site that is neither ledgered nor exempted, plus every stale table row."""
    found: Counter[Key] = Counter()
    problems: list[str] = []
    for module in modules:
        for site in module.sites:
            found[site.key] += 1
            row = table.get(site.key)
            where = f"{module.name}.{site.function}:{site.call.lineno} -> {site.callee}"
            if row is None:
                problems.append(f"unledgered site {where}: ledger it or add an exemption")
                continue
            problem = _verdict_problem(site, module, row[1])
            if problem:
                problems.append(f"{where}: {problem}")
    for key, (count, _verdict) in table.items():
        if found[key] != count:
            problems.append(f"{key}: table says {count} call(s), the tree has {found[key]}")
    return problems


def test_every_prompt_injecting_site_is_ledgered_or_exempt() -> None:
    """The real tree: each primitive call matches its row, and no row is stale."""
    problems = violations(scan(_PKG), SITES)
    assert not problems, "\n".join(problems)


def test_fixture_unledgered_site_fails(tmp_path: Path) -> None:
    """A synthetic module with a bare ``send_text`` call fails the invariant."""
    pkg = tmp_path / "command_center"
    pkg.mkdir()
    (pkg / "rogue.py").write_text(
        "def nudge(deps, target):\n    deps.send_text(target, 'continue')\n", encoding="utf-8"
    )
    problems = violations(scan(pkg), {})
    assert problems == ["unledgered site rogue.nudge:2 -> send_text: ledger it or add an exemption"]


def test_fixture_extra_call_in_known_function_fails(tmp_path: Path) -> None:
    """A second typing call in an already-listed function fails on the count."""
    pkg = tmp_path / "command_center"
    pkg.mkdir()
    (pkg / "rogue.py").write_text(
        "def nudge(deps, target):\n"
        "    deps.send_text(target, 'a')\n"
        "    deps.send_text(target, 'b')\n",
        encoding="utf-8",
    )
    table = {("rogue", "nudge", "send_text"): (1, "exempt: a synthetic fixture, never run")}
    problems = violations(scan(pkg), table)
    assert any("table says 1 call(s), the tree has 2" in p for p in problems), problems


def test_fixture_claimed_kind_without_ledger_fails(tmp_path: Path) -> None:
    """A row claiming a kind fails unless the module really ledgers it."""
    pkg = tmp_path / "command_center"
    pkg.mkdir()
    (pkg / "rogue.py").write_text(
        "def paste(deps, live, text):\n    return guarded_paste(deps, live, text)\n",
        encoding="utf-8",
    )
    problems = violations(scan(pkg), {("rogue", "paste", "guarded_paste"): (1, "park_now")})
    assert problems == [
        "rogue.paste:2 -> guarded_paste: kind 'park_now' never appears in module rogue"
    ]
    (pkg / "rogue.py").write_text(
        "def paste(deps, live, text):\n"
        "    kind = 'park_now'\n"
        "    return guarded_paste(deps, live, text)\n",
        encoding="utf-8",
    )
    problems = violations(scan(pkg), {("rogue", "paste", "guarded_paste"): (1, "park_now")})
    assert problems == ["rogue.paste:3 -> guarded_paste: guarded_paste call without ledger="]


def test_fixture_stale_row_fails(tmp_path: Path) -> None:
    """A table row whose site is gone fails, so the table tracks the tree."""
    pkg = tmp_path / "command_center"
    pkg.mkdir()
    (pkg / "quiet.py").write_text("X = 1\n", encoding="utf-8")
    table = {("quiet", "gone", "send_text"): (1, "exempt: a synthetic fixture, never run")}
    assert violations(scan(pkg), table) == [
        "('quiet', 'gone', 'send_text'): table says 1 call(s), the tree has 0"
    ]


def test_table_verdicts_are_well_formed() -> None:
    """Every verdict is a D2 kind or carries an exempt:/gap:/choke: prefix with a reason."""
    for key, (count, verdict) in SITES.items():
        assert count >= 1, key
        prefix, sep, reason = verdict.partition(": ")
        if verdict in KINDS:
            continue
        assert sep and prefix in {"exempt", "gap", "choke"} and len(reason) >= 15, (key, verdict)
