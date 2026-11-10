"""Show a proposal: as a markdown report, and as a coloured diff in the terminal."""
import difflib
import os
import re
import sys
from pathlib import Path

from . import memory as mem
from .proposal import counts, index_line_diff, resolve

LABEL = {
    "fix": "FIX", "merge": "MERGE", "prune": "PRUNE", "new": "NEW",
    "reindex": "REINDEX", "promote_rule": "PROMOTE → CLAUDE.md", "promote_skill": "PROMOTE → skill",
}


def _color(enabled):
    if not enabled:
        return lambda text, code: text
    return lambda text, code: f"\033[{code}m{text}\033[0m"


def _current(change, project) -> str:
    path = resolve(change, project.memory_dir, project.cwd)
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def change_diff(change, project):
    """Unified diff lines for one file change."""
    before = _current(change, project)
    action = change["action"]
    name = change["path"] if change["target"] != "skill" else f".claude/skills/{change['path']}/SKILL.md"
    index = project.memory_dir / mem.INDEX
    index_text = index.read_text(encoding="utf-8", errors="replace") if index.exists() else ""
    index_lines = index_line_diff(index_text, change) if change["target"] == "memory" else []
    if index_lines:
        index_lines = ["--- a/MEMORY.md", "+++ b/MEMORY.md"] + index_lines
    if action in ("index", "unindex"):
        return index_lines or ["(index already correct)"]
    if action == "delete":
        after = ""
    elif action == "append":
        after = before + ("\n" if before and not before.endswith("\n") else "") + change["content"].strip() + "\n"
    else:
        after = change["content"].rstrip("\n") + "\n"
    lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"a/{name}" if before else "/dev/null",
        tofile=f"b/{name}" if after else "/dev/null",
        lineterm="", n=2,
    ))
    return lines + index_lines


_TICKS = re.compile(r"`+")
WHO = {"USER": "user said", "ASSISTANT": "agent said", "RESULT": "tool output", "ERROR": "tool error", "TOOL": "tool call"}


def evidence_line(ev, dates) -> str:
    if ev.get("file", "").startswith("memory:"):
        return f"memory `{ev['file'][7:]}`: does not exist (checked)"
    if ev.get("file"):
        if not ev.get("quote"):
            return f"repo `{ev['file']}`: does not exist (checked)"
        return f"repo `{ev['file']}`: “{ev['quote']}”"
    who = WHO.get(ev.get("role"), "session")
    return f"{who}, {ev.get('day') or dates.get(ev['session'], '')} `{ev['session'][:8]}`: “{ev['quote']}”"


def _session_dates(proposal):
    return {s["session"]: s.get("date", "") for s in proposal.get("meta", {}).get("sessions", [])}


def report_markdown(proposal, project) -> str:
    meta = proposal.get("meta", {})
    dates = _session_dates(proposal)
    live = [op for op in proposal["ops"] if op["status"] == "proposed"]
    dead = [op for op in proposal["ops"] if op["status"] == "rejected"]
    out = [f"# Dream {meta.get('run', '')}", ""]
    out.append(proposal.get("summary", "").strip())
    out.append("")
    usage = meta.get("usage", {})
    out.append(f"- Project: `{meta.get('project', '')}`")
    out.append(f"- Sessions reviewed: {len(meta.get('sessions', []))}; memory health before: {meta.get('scan_score')}/100")
    out.append(f"- Proposed: {len(live)} change(s); filtered out before you: {len(dead)}")
    if meta.get("note"):
        out.append(f"- Note: {meta['note']}")
    fc = proposal.get("index_forecast") or {}
    if fc:
        out.append(f"- Index after applying all: {fc.get('lines_after')} lines / {fc.get('bytes_after')} bytes"
                   f" (was {fc.get('lines_before')} / {fc.get('bytes_before')})")
    if usage:
        out.append(f"- Cost: {usage.get('calls')} model calls, ${usage.get('cost_usd')} at API prices, {meta.get('wall_seconds')}s")
    out.append("")
    for op in live:
        out.append(f"## {op['id']} · {LABEL.get(op['kind'], op['kind'])} · {op.get('title', '')}")
        out.append("")
        out.append(f"Confidence: **{op.get('confidence')}**. {op.get('rationale', '')}")
        for note in op.get("notes", []):
            out.append(f"> Note: {note}")
        if op.get("evidence"):
            out.append("")
            out.append("Evidence:")
            for ev in op["evidence"]:
                out.append("- " + evidence_line(ev, dates))
        out.append("")
        diff = [line for change in op["changes"] for line in change_diff(change, project)]
        fence = "`" * max(3, 1 + max((len(m) for line in diff for m in _TICKS.findall(line)), default=0))
        out.append(fence + "diff")
        out.extend(diff)
        out.append(fence)
        out.append("")
    if proposal.get("left_out"):
        out.append("## Considered and left out")
        out.append("")
        for item in proposal["left_out"]:
            out.append(f"- {item['what']}: {item['reason']}")
        out.append("")
    if dead:
        out.append("## Filtered out")
        out.append("")
        for op in dead:
            out.append(f"- {op['id']} {LABEL.get(op['kind'], op.get('kind'))} “{op.get('title', '')}”: "
                       f"{op.get('rejected_by')}: {op.get('reason', '')}")
        out.append("")
    return "\n".join(out)


def print_proposal(proposal, project, stream=None, color=None, brief=False):
    stream = stream or sys.stdout
    if color is None:
        color = (stream.isatty() or bool(os.environ.get("FORCE_COLOR"))) and not os.environ.get("NO_COLOR")
    c = _color(color)
    meta = proposal.get("meta", {})
    dates = _session_dates(proposal)
    live = [op for op in proposal["ops"] if op["status"] == "proposed"]
    dead = [op for op in proposal["ops"] if op["status"] == "rejected"]
    done = [op for op in proposal["ops"] if op["status"] in ("applied", "skipped")]
    w = stream.write
    w(c(f"dream {meta.get('run', '')}", "1") + f"  {meta.get('project', '')}\n")
    if proposal.get("summary"):
        w(proposal["summary"].strip() + "\n")
    w("\n")
    for op in live:
        tag = LABEL.get(op["kind"], op["kind"])
        conf = op.get("confidence", "")
        conf_code = {"high": "32", "medium": "33", "low": "31"}.get(conf, "0")
        w(c(f"{op['id']}", "1;36") + "  " + c(tag, "1") + f"  {op.get('title', '')}  " + c(f"[{conf}]", conf_code) + "\n")
        w(f"  {op.get('rationale', '')}\n")
        for note in op.get("notes", []):
            w(c(f"  note: {note}\n", "33"))
        for ev in op.get("evidence", [])[: 1 if brief else None]:
            w(c("  evidence ", "2") + evidence_line(ev, dates) + "\n")
        for change in ([] if brief else op["changes"]):
            for line in change_diff(change, project):
                if line.startswith("+") and not line.startswith("+++"):
                    w("  " + c(line, "32") + "\n")
                elif line.startswith("-") and not line.startswith("---"):
                    w("  " + c(line, "31") + "\n")
                elif line.startswith("@@"):
                    w("  " + c(line, "36") + "\n")
                else:
                    w("  " + c(line, "2") + "\n")
        w("\n")
    if dead:
        w(c(f"filtered out before you ({len(dead)}):\n", "2"))
        for op in dead:
            w(c(f"  {op['id']} {op.get('kind')} “{op.get('title', '')}”: {op.get('rejected_by')}: {op.get('reason', '')}\n", "2"))
        w("\n")
    if proposal.get("left_out") and brief:
        w(c(f"considered and left out: {len(proposal['left_out'])} (rem review shows why)\n\n", "2"))
    elif proposal.get("left_out"):
        w(c(f"considered and left out ({len(proposal['left_out'])}):\n", "2"))
        for item in proposal["left_out"]:
            w(c(f"  {item['what']}: {item['reason']}\n", "2"))
        w("\n")
    for op in done:
        w(c(f"  {op['id']} {op['status']}: {op.get('title', '')} {op.get('reason', '')}\n", "2"))
    kinds = counts(proposal)
    if live:
        summary = ", ".join(f"{n} {k}" for k, n in kinds.items())
        w(c(f"{len(live)} change(s) waiting: {summary}\n", "1"))
        w("apply all: rem apply    some: rem apply op1 op3    none: rem discard\n")
    elif not done:
        w("Nothing to apply.\n")


def print_scan(scan, stream=None, color=None):
    stream = stream or sys.stdout
    if color is None:
        color = (stream.isatty() or bool(os.environ.get("FORCE_COLOR"))) and not os.environ.get("NO_COLOR")
    c = _color(color)
    score = scan["score"]
    code = "32" if score >= 85 else "33" if score >= 60 else "31"
    stream.write(c(f"memory health {score}/100", f"1;{code}") +
                 f"  {scan['files']} files, {scan['bytes'] / 1000:.1f} KB, index {scan['index_lines']}/{mem.INDEX_MAX_LINES} lines\n")
    stream.write(c(f"{scan['memory_dir']}\n", "2"))
    if not scan["issues"]:
        stream.write("no issues found\n")
        return
    order = {"high": 0, "medium": 1, "low": 2}
    for item in sorted(scan["issues"], key=lambda i: order[i["severity"]]):
        sev = {"high": "31", "medium": "33", "low": "2"}[item["severity"]]
        stream.write("  " + c(f"{item['severity']:<6}", sev) + f" {item['kind']:<20} {item['file']}: {item['detail']}\n")
