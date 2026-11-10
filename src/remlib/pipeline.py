"""The dream: digest sessions, analyze, synthesize, validate, second-read. Read-only."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import digest as dg
from . import llm
from . import memory as mem
from . import proposal as pp
from .digest import find_quote, normalize
from .paths import sha, sha_bytes

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
BATCH_CHARS = 60_000
CHUNK_CHARS = 28_000
MEMORY_FILE_CAP = 30_000
ANALYZE_FILE_CAP = 1_800
AUDIT_EVERY_DAYS = 7
LONG_SESSION_CHARS = 40_000
CLAUDE_MD_CAP = 20_000

_EVIDENCE = {
    "type": "object",
    "properties": {"session": {"type": "string"}, "file": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["session", "file", "quote"],
}

ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "memory_checks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "verdict": {"type": "string", "enum": ["confirmed", "contradicted", "outdated", "ignored"]},
                                "note": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                            "required": ["file", "verdict", "note", "quote"],
                        },
                    },
                    "lessons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "statement": {"type": "string"},
                                "kind": {"type": "string", "enum": ["user", "feedback", "project", "reference"]},
                                "why": {"type": "string"},
                                "quote": {"type": "string"},
                                "covered_by": {"type": "string"},
                            },
                            "required": ["statement", "kind", "why", "quote", "covered_by"],
                        },
                    },
                },
                "required": ["session", "memory_checks", "lessons"],
            },
        }
    },
    "required": ["sessions"],
}

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(pp.KINDS)},
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence": {"type": "array", "items": _EVIDENCE},
                    "changes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "enum": ["memory", "claude_md", "skill"]},
                                "path": {"type": "string"},
                                "action": {"type": "string", "enum": ["write", "delete", "append", "index"]},
                                "content": {"type": "string"},
                                "index_entry": {"type": "string"},
                            },
                            "required": ["target", "path", "action", "content", "index_entry"],
                        },
                    },
                },
                "required": ["kind", "title", "rationale", "confidence", "evidence", "changes"],
            },
        },
        "file_reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["keep", "fix", "merge", "prune"]},
                    "note": {"type": "string"},
                },
                "required": ["file", "verdict", "note"],
            },
        },
        "left_out": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"what": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["what", "reason"],
            },
        },
    },
    "required": ["summary", "ops", "file_reviews", "left_out"],
}

SKEPTIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["approve", "revise", "downgrade", "reject"]},
                    "reason": {"type": "string"},
                    "revised_path": {"type": "string"},
                    "revised_content": {"type": "string"},
                },
                "required": ["id", "verdict", "reason", "revised_path", "revised_content"],
            },
        }
    },
    "required": ["verdicts"],
}


def _prompt(name: str) -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    if "{shared}" in text:
        # The file format and the evidence rules are written once, in synthesize.md.
        full = (PROMPTS / "synthesize.md").read_text(encoding="utf-8")
        text = text.replace("{shared}", full[full.index("## File changes"):full.index("## Judgement")].strip())
    return text


AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": SYNTH_SCHEMA["properties"]["ops"],
        "note": {"type": "string"},
    },
    "required": ["ops", "note"],
}
_PRIORITY = {"check": 0, "dead_reference": 1, "near_duplicate": 1, "oversized": 2, "dead_link": 3, "stale": 4}


def audit_targets(memory_files, scan, findings, state, limit):
    """Memory files that deserve a focused look, most pressing first."""
    reasons = {}
    for finding in findings:
        for check in finding.get("memory_checks", []):
            # `ignored` means the memory is right and the agent lapsed: that is a question
            # of promotion, which the consolidation step decides, not a fault in the file.
            if check.get("verdict") in ("contradicted", "outdated") and check.get("file") in memory_files:
                entry = reasons.setdefault(check["file"], {"checks": [], "issues": [], "partner": None, "rank": 9})
                entry["checks"].append({**check, "session": finding["session"],
                                        "session_dates": finding.get("session_dates", "")})
                entry["rank"] = 0
    for issue in scan["issues"]:
        name = issue["file"]
        if name not in memory_files or issue["kind"] not in _PRIORITY:
            continue
        entry = reasons.setdefault(name, {"checks": [], "issues": [], "partner": None, "rank": 9})
        entry["issues"].append(f"{issue['kind']}: {issue['detail']}")
        entry["rank"] = min(entry["rank"], _PRIORITY[issue["kind"]])
        if issue["kind"] == "near_duplicate":
            partner = issue["detail"].rsplit(" with ", 1)[-1]
            if partner in memory_files:
                entry["partner"] = partner
    for finding in findings:
        for lesson in finding.get("lessons", []):
            name = lesson.get("covered_by")
            if isinstance(name, str) and name in memory_files and name not in reasons:
                reasons[name] = {"checks": [], "issues": [], "partner": None, "rank": 5, "by_lesson": True}
    done = state.get("file_audits", {})
    targets = []
    for name, entry in reasons.items():
        signature = sha(memory_files[name]["text"] + "|" + "|".join(sorted({i.split(":")[0] for i in entry["issues"]})))
        if not entry["checks"] and not entry.get("by_lesson") and done.get(name) == signature:
            continue  # already looked at this exact file for these exact reasons
        lessons = [dict(lesson, session=f["session"], session_dates=f.get("session_dates", ""))
                   for f in findings for lesson in f.get("lessons", [])
                   if lesson.get("covered_by") == name or name in str(lesson.get("why", ""))]
        targets.append({"file": name, "signature": signature, "lessons": lessons[:12], **entry})
    targets.sort(key=lambda t: (t["rank"], -len(t["checks"]), t["file"]))
    partners = set()
    chosen = []
    for target in targets:
        if target["file"] in partners:
            continue  # its duplicate is already being reviewed together with it
        if target["partner"]:
            partners.add(target["partner"])
        chosen.append(target)
    return chosen[:limit], max(0, len(chosen) - limit)


def audit(project, memory_files, targets, metas, usage, log, workers=4):
    """One focused call per flagged memory file: verify it against the project, fix or keep."""
    system = _prompt("audit")
    claude_md = pp.claude_md_path(project.cwd)
    claude_text = claude_md.read_text(encoding="utf-8", errors="replace")[:6000] if claude_md.exists() else ""
    index_text = (project.memory_dir / mem.INDEX).read_text(encoding="utf-8", errors="replace") \
        if (project.memory_dir / mem.INDEX).exists() else ""
    others = "\n".join(f"- {name}: {info['fields'].get('description', '')[:160]}" for name, info in memory_files.items())
    tools = ["Read", "Glob", "Grep"] if project.cwd.is_dir() else None

    def run(target):
        name = target["file"]
        info = memory_files[name]
        index_line = next((ln for ln in index_text.splitlines() if f"({name})" in ln), "(not in the index)")
        parts = [
            f"Today is {time.strftime('%Y-%m-%d')}. Project directory: {project.cwd}",
            f'<memory_file name="{name}" last_changed_days_ago="{int((time.time() - info["mtime"]) / 86400)}">\n{info["text"]}\n</memory_file>',
            f"<index_line>{index_line}</index_line>",
            "<flagged_because>\n" + "\n".join(f"- {r}" for r in target["issues"]) + "\n</flagged_because>" if target["issues"] else "",
            "<session_findings>\n" + json.dumps(target["checks"], ensure_ascii=False, indent=1) + "\n</session_findings>" if target["checks"] else "",
            "<lessons_for_this_file>\n" + json.dumps(target["lessons"], ensure_ascii=False, indent=1) + "\n</lessons_for_this_file>" if target.get("lessons") else "",
        ]
        if target["partner"]:
            parts.append(f'<partner_file name="{target["partner"]}">\n{memory_files[target["partner"]]["text"]}\n</partner_file>')
        parts += [f"<other_memory_files>\n{others}\n</other_memory_files>",
                  f"<claude_md>\n{claude_text or '(none)'}\n</claude_md>",
                  "Review this memory file and return at most one operation."]
        try:
            result = llm.call("audit", system, "\n\n".join(p for p in parts if p), AUDIT_SCHEMA, usage=usage,
                              tools=tools, cwd=project.cwd if project.cwd.is_dir() else None, timeout=900)
        except llm.LLMError as err:
            log(f"  review of {name} failed: {err}")
            return target, None
        return target, result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, targets))
    ops, notes, reviewed = [], [], {}
    for target, result in results:
        if result is None:
            continue
        reviewed[target["file"]] = target["signature"]
        found = [op for op in (result.get("ops") or []) if isinstance(op, dict)][:1]
        for op in found:
            op["source"] = "file review"
        ops.extend(found)
        if not found:
            notes.append({"what": f"{target['file']} (flagged: {'; '.join(target['issues'] + [c.get('verdict', '') for c in target['checks']])[:160]})",
                          "reason": "kept as is: " + str(result.get("note") or "")})
    return ops, notes, reviewed


def _memory_block(files: dict, cap: int = MEMORY_FILE_CAP) -> str:
    if not files:
        return "<memory>\n(the memory directory is empty)\n</memory>"
    parts = ["<memory>"]
    for name, info in files.items():
        text = info["text"]
        if len(text) > cap:
            text = text[:cap] + "\n[…cut…]"
        age = int((time.time() - info["mtime"]) / 86400)
        parts.append(f'<file name="{name}" last_changed_days_ago="{age}">\n{text}\n</file>')
    parts.append("</memory>")
    return "\n".join(parts)


def _chunks(session: str, text: str):
    """A long digest is read in parts, so a lesson in the middle gets the same attention as one at the start."""
    if len(text) <= CHUNK_CHARS:
        return [{"session": session, "text": text, "chars": len(text)}]
    lines = text.splitlines(keepends=True)
    header, parts, current, size = lines[0], [], [], 0
    for line in lines[1:]:
        if current and size + len(line) > CHUNK_CHARS:
            parts.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        parts.append(current)
    out = []
    for i, part in enumerate(parts, 1):
        body = f"{header.rstrip()} | part {i} of {len(parts)}\n" + "".join(part)
        out.append({"session": session, "text": body, "chars": len(body)})
    return out


def _batches(units):
    batch, size = [], 0
    for unit in units:
        if batch and size + unit["chars"] > BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(unit)
        size += unit["chars"]
    if batch:
        yield batch


def analyze(metas, digests, memory_files, usage, log, workers=4):
    """Map step: one call per batch of sessions. Returns verified findings and stats."""
    system = _prompt("analyze")
    memory_block = _memory_block(memory_files, ANALYZE_FILE_CAP)
    units = [unit for meta in metas for unit in _chunks(meta["session"], digests[meta["session"]])]
    batches = list(_batches(units))
    errors = []

    def run(batch):
        body = [memory_block, "<sessions>"]
        for unit in batch:
            body.append(f'<digest session="{unit["session"]}">\n{unit["text"]}</digest>')
        body.append("</sessions>")
        body.append("Report on each session above (a long session arrives in parts; report on the part you "
                    "were given). Use the session ids exactly as given.")
        try:
            return llm.call("analyze", system, "\n".join(body), ANALYZE_SCHEMA, usage=usage)
        except llm.LLMError as err:
            log(f"  analyze batch failed: {err}")
            errors.append(str(err))
            return {"sessions": []}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, batches))

    findings, stats = [], {"items": 0, "grounded": 0, "sessions_with_findings": 0, "errors": errors}
    by_session = {}
    for result in results:
        for sess in result.get("sessions") or []:
            if not isinstance(sess, dict):
                continue
            sid = sess.get("session")
            text = digests.get(sid) if isinstance(sid, str) else None
            if not text:
                continue
            kept = by_session.setdefault(sid, {"session": sid, "memory_checks": [], "lessons": []})
            for field in ("memory_checks", "lessons"):
                for item in (sess.get(field) if isinstance(sess.get(field), list) else []):
                    stats["items"] += 1
                    if not isinstance(item, dict):
                        continue
                    if field == "memory_checks" and not (isinstance(item.get("file"), str) and item["file"] in memory_files):
                        continue
                    hit = find_quote(str(item.get("quote") or ""), text) if isinstance(item, dict) else None
                    if hit:
                        stats["grounded"] += 1
                        item["said_by"] = hit["role"]
                        if hit.get("day"):
                            item["said_on"] = hit["day"]
                        kept[field].append(item)
    for meta in metas:
        kept = by_session.get(meta["session"])
        if kept and (kept["memory_checks"] or kept["lessons"]):
            stats["sessions_with_findings"] += 1
            findings.append(kept)
    return findings, stats


def synthesize(project, memory_files, scan, findings, metas, usage, instructions="", extra=""):
    """Reduce step: one call that sees everything and may read the repository."""
    claude_md = pp.claude_md_path(project.cwd)
    claude_text = ""
    if claude_md.exists():
        claude_text = claude_md.read_text(encoding="utf-8", errors="replace")[:CLAUDE_MD_CAP]
    skills_dir = project.cwd / ".claude" / "skills"
    skills = sorted(p.name for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else []
    index_text = (project.memory_dir / mem.INDEX).read_text(encoding="utf-8", errors="replace") \
        if (project.memory_dir / mem.INDEX).exists() else ""
    parts = [
        f"Today is {time.strftime('%Y-%m-%d')}. Project directory: {project.cwd}",
        f"<index file=\"MEMORY.md\">\n{index_text}</index>",
        _memory_block(memory_files),
        "<scan>\n" + json.dumps(scan["issues"], ensure_ascii=False, indent=1) + "\n</scan>",
        "<findings>\n" + json.dumps(findings, ensure_ascii=False, indent=1) + "\n</findings>",
        f"<claude_md>\n{claude_text or '(none)'}\n</claude_md>",
        f"<existing_skills>{', '.join(skills) or '(none)'}</existing_skills>",
    ]
    if extra:
        parts.append(extra)
    if instructions:
        parts.append(f"<focus>\nThe person asked this consolidation to focus on: {instructions}\n</focus>")
    parts.append("Return the proposed operations.")
    tools = ["Read", "Glob", "Grep"] if project.cwd.is_dir() else None
    return llm.call("synthesize", _prompt("synthesize"), "\n\n".join(parts), SYNTH_SCHEMA,
                    usage=usage, tools=tools, cwd=project.cwd if project.cwd.is_dir() else None, timeout=1500)


def _context(quote: str, digest_text: str, radius=5) -> str:
    lines = digest_text.splitlines()
    needle = normalize(quote)[:60]
    for i, line in enumerate(lines):
        if needle and needle in normalize(line):
            lo, hi = max(1, i - radius), min(len(lines), i + radius + 1)
            return "\n".join(ln[:500] for ln in lines[lo:hi])
    return ""


def second_read(proposal, memory_files, digests, usage, log, project=None):
    """An independent model reads each surviving operation and may veto it."""
    ops = [op for op in proposal["ops"] if op["status"] == "proposed"]
    if not ops:
        return
    blocks = []
    for op in ops:
        block = [f'<operation id="{op["id"]}" kind="{op["kind"]}" confidence="{op.get("confidence")}">',
                 f"title: {op.get('title')}", f"rationale: {op.get('rationale')}"]
        for change in op["changes"]:
            name = change["path"]
            block.append(f'<change target="{change["target"]}" path="{name}" action="{change["action"]}">')
            if change["target"] == "memory" and name in memory_files:
                block.append(f"<current>\n{memory_files[name]['text'][:MEMORY_FILE_CAP]}\n</current>")
            if change.get("content"):
                block.append(f"<proposed>\n{change['content']}\n</proposed>")
            if change.get("index_entry"):
                block.append(f"<index_entry>{change['index_entry']}</index_entry>")
            block.append("</change>")
        for ev in op["evidence"]:
            if ev.get("file"):
                claim = f'quote: {ev["quote"]}' if ev["quote"] else "claim: this path does not exist"
                block.append(f'<evidence repository_file="{ev["file"]}" confirmed_by_program="{ev.get("checked")}">\n{claim}\n</evidence>')
                continue
            ctx = _context(ev["quote"], digests.get(ev["session"], ""))
            block.append(f'<evidence session="{ev["session"]}" said_by="{ev.get("role")}" said_on="{ev.get("day", "")}">\nquote: {ev["quote"]}\n<context>\n{ctx}\n</context>\n</evidence>')
        block.append("</operation>")
        blocks.append("\n".join(block))
    try:
        result = llm.call("skeptic", _prompt("skeptic"),
                          "\n\n".join(blocks) + "\n\nGive a verdict for every operation id.",
                          SKEPTIC_SCHEMA, usage=usage)
    except llm.LLMError as err:
        log(f"  second read failed, proposal left unreviewed: {err}")
        proposal["second_read"] = "failed"
        for op in ops:
            op["confidence"] = "low"
            op["notes"].append("the second reader did not run, so this was not independently checked")
        return
    verdicts = result.get("verdicts") if isinstance(result.get("verdicts"), list) else []
    by_id = {}
    for v in verdicts:
        if isinstance(v, dict) and isinstance(v.get("id"), str) and v.get("verdict") in ("approve", "revise", "downgrade", "reject"):
            v["reason"] = " ".join(pp.clean_text(str(v.get("reason") or "")).split())[:500]
            by_id[v["id"]] = v
    for op in ops:
        verdict = by_id.get(op["id"])
        if not verdict:
            op["notes"].append("second reader gave no verdict")
            op["confidence"] = "low"
            continue
        op["second_read"] = verdict["verdict"]
        if verdict["verdict"] == "reject":
            op["status"] = "rejected"
            op["rejected_by"] = "second reader"
            op["reason"] = verdict.get("reason", "")
        elif verdict["verdict"] == "revise" and project is not None:
            refused = pp.revise_change(op, str(verdict.get("revised_path") or ""), verdict.get("revised_content") or "",
                                       project.memory_dir, project.cwd)
            if refused:
                op["confidence"] = "low"
                op["notes"].append("second reader asked for cuts it could not make cleanly: " + verdict.get("reason", ""))
            else:
                if op.get("confidence") == "high":
                    op["confidence"] = "medium"
                op["notes"].append("trimmed by the second reader: " + verdict.get("reason", ""))
        elif verdict["verdict"] in ("downgrade", "revise"):
            op["confidence"] = "low"
            op["notes"].append("second reader: " + verdict.get("reason", ""))
    proposal["second_read"] = "done"


def dream(project, days=14, max_sessions=20, skeptic=True, instructions="", log=print, sessions=None, max_reviews=8):
    """Run one dream for a project. Writes a proposal; changes nothing else."""
    started = time.time()
    state = project.load_state()
    usage = llm.Usage()
    run = project.new_run()
    scan = mem.scan(project.memory_dir, project.cwd)
    memory_files = mem.load(project.memory_dir)["files"]
    # What the files looked like when the dream read them; an apply refuses anything changed since.
    base = {str(p): sha_bytes(p) for p in project.memory_dir.glob("*.md")} if project.memory_dir.is_dir() else {}
    base[str(pp.claude_md_path(project.cwd))] = sha_bytes(pp.claude_md_path(project.cwd))
    replaced = state.get("pending_run")
    log(f"memory: {scan['files']} files, health {scan['score']}/100, {len(scan['issues'])} issue(s)")

    if sessions is None:
        if project.seen_mtime():
            # Work through the backlog oldest first, so nothing is skipped for good.
            sessions = dg.list_sessions(project.transcripts_dir, since_ts=project.seen_mtime())[:max_sessions]
        else:
            # First dream: the most recent sessions are the ones worth the money.
            sessions = dg.list_sessions(project.transcripts_dir, since_ts=time.time() - days * 86400)[-max_sessions:]
    metas = dg.build_digests(sessions, run / "digests")
    digests = {m["session"]: Path(m["digest"]).read_text(encoding="utf-8") for m in metas}
    raw_bytes = sum(m["source_bytes"] for m in metas)
    digest_chars = sum(m["chars"] for m in metas)
    log(f"sessions: {len(metas)} to review ({raw_bytes / 1e6:.1f} MB of transcript -> {digest_chars / 1e3:.0f} KB digest)")

    findings, stats = [], {"items": 0, "grounded": 0, "sessions_with_findings": 0, "errors": []}
    if metas:
        log("analyzing sessions…")
        findings, stats = analyze(metas, digests, memory_files, usage, log)
        log(f"  {stats['grounded']}/{stats['items']} findings verified against transcripts")
    (run / "findings.json").write_text(json.dumps(findings, indent=2, ensure_ascii=False), encoding="utf-8")
    analysis_failed = bool(stats.get("errors"))

    # The memory itself is audited (duplicates, drift from the code) whenever it has
    # changed since the last audit, and at least once a week, even with no new findings.
    memory_sha = sha("".join(f"{n}\0{i['text']}" for n, i in sorted(memory_files.items())))
    audit_due = bool(memory_files) and (
        state.get("audited_memory_sha") != memory_sha
        or time.time() - state.get("audited_at", 0) > AUDIT_EVERY_DAYS * 86400
    )
    dates = {m["session"]: (m["date"], m.get("end") or m["date"]) for m in metas}
    for f in findings:
        start, end = dates.get(f["session"], ("", ""))
        f["session_dates"] = start if start == end else f"{start} to {end}"

    # Flagged files each get a focused review that checks them against the project.
    targets, deferred = audit_targets(memory_files, scan, findings, state, max_reviews)
    audit_ops, audit_notes, reviewed = [], [], {}
    if targets:
        log(f"reviewing {len(targets)} flagged memory file(s)…")
        audit_ops, audit_notes, reviewed = audit(project, memory_files, targets, metas, usage, log)
    under_review = set(reviewed)
    for op in audit_ops:
        for change in op.get("changes") or []:
            if isinstance(change, dict) and change.get("target") == "memory":
                under_review.add(str(change.get("path")))

    proposal = {"summary": "", "ops": []}
    if findings or scan["issues"] or audit_due:
        log("consolidating…")
        try:
            rest = [dict(f, memory_checks=[c for c in f["memory_checks"] if c.get("file") not in under_review])
                    for f in findings]
            rest = [f for f in rest if f["memory_checks"] or f["lessons"]]
            hands_off = ""
            if under_review:
                hands_off = ("<under_review>\nThese memory files have just had a separate focused review; do not "
                             "change or delete them, and give them the verdict `keep`: " + ", ".join(sorted(under_review)) +
                             ". If a lesson belongs in one of them, put it in left_out and name the file.\n</under_review>")
            proposal = synthesize(project, memory_files, scan, rest, metas, usage, instructions, hands_off)
        except llm.LLMError as err:
            log(f"  consolidation failed: {err}")
            proposal = {"summary": f"Consolidation failed: {err}", "ops": [], "failed": True}
    else:
        proposal["summary"] = "Nothing new in recent sessions and the memory is healthy. No changes proposed."

    proposal["ops"] = audit_ops + [op for op in (proposal.get("ops") or []) if isinstance(op, dict)]
    proposal["left_out"] = audit_notes + [i for i in (proposal.get("left_out") or []) if isinstance(i, dict)]
    if deferred:
        proposal["left_out"].append({"what": f"{deferred} more flagged memory file(s)",
                                     "reason": "not reviewed this time (review limit); the next dream picks them up"})
    proposal = pp.validate(proposal, project.memory_dir, project.cwd, digests, base)
    proposal["left_out"] = [
        {"what": pp.clean_text(str(i.get("what", "")))[:300], "reason": pp.clean_text(str(i.get("reason", "")))[:400]}
        for i in proposal.get("left_out") or [] if isinstance(i, dict)]
    proposal["file_reviews"] = [
        {"file": str(i.get("file", ""))[:120], "verdict": str(i.get("verdict", "")), "note": pp.clean_text(str(i.get("note", "")))[:300]}
        for i in proposal.get("file_reviews") or [] if isinstance(i, dict)]
    survived = [op for op in proposal["ops"] if op["status"] == "proposed"]
    if skeptic and survived:
        log("second read…")
        second_read(proposal, memory_files, digests, usage, log, project)
    for number, repair in enumerate(pp.index_repairs(proposal, project.memory_dir), len(proposal["ops"]) + 1):
        repair["id"] = f"op{number}"
        proposal["ops"].append(repair)
    proposal["index_forecast"] = pp.forecast_index(proposal, project.memory_dir)

    # The record of what happened is computed here, not taken from a model's summary.
    live = [op for op in proposal["ops"] if op["status"] == "proposed"]
    touched = {c["path"]: op["kind"] for op in live for c in op["changes"] if c.get("target") == "memory"}
    notes = {r["file"]: r["note"] for r in proposal.get("file_reviews", [])}
    proposal["file_reviews"] = [{"file": name, "verdict": touched.get(name, "keep"), "note": notes.get(name, "")}
                                for name in memory_files]
    proposal["left_out"] = [item for item in proposal["left_out"]
                            if not any(name in item["what"] or name[:-3] in item["what"] for name in touched)]
    if audit_ops:
        kinds = pp.counts(proposal)
        counted = ", ".join(f"{n} {k}" for k, n in kinds.items()) or "no changes"
        proposal["summary"] = (f"Proposed: {counted}. Flagged memory files were each checked against the project. "
                               f"From recent sessions: {proposal['summary']}").strip()
    if replaced and live:
        proposal["summary"] += f" (This replaces the earlier unreviewed proposal {replaced}.)"

    proposal["meta"] = {
        "run": run.name,
        "project": str(project.cwd),
        "memory_dir": str(project.memory_dir),
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "sessions": [{k: m.get(k, "") for k in ("session", "date", "end", "title")} for m in metas],
        "scan_score": scan["score"],
        "scan_issues": len(scan["issues"]),
        "analysis": stats,
        "usage": usage.as_dict(),
        "wall_seconds": round(time.time() - started, 1),
        "transcript_bytes": raw_bytes,
        "digest_chars": digest_chars,
    }
    (run / "proposal.json").write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    from .render import report_markdown
    (run / "report.md").write_text(report_markdown(proposal, project), encoding="utf-8")

    if analysis_failed:
        proposal["meta"]["note"] = "Some sessions could not be analyzed; they will be reviewed again next time."
        (run / "proposal.json").write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    if not proposal.get("failed"):
        # Sessions count as reviewed only when their analysis actually ran.
        if sessions and not analysis_failed:
            state.setdefault("seen", {})[str(project.transcripts_dir)] = max(os.path.getmtime(p) for p in sessions)
        state["last_dream"] = time.strftime("%Y-%m-%d %H:%M")
        if not analysis_failed:
            state["audited_memory_sha"] = memory_sha
            state["audited_at"] = time.time()
        if reviewed:
            state["file_audits"] = {**state.get("file_audits", {}), **reviewed}
        state["last_run"] = run.name
        live = [op for op in proposal["ops"] if op["status"] == "proposed"]
        if live:
            state["pending_run"] = run.name
        project.save_state(state)
    try:
        project.prune_runs()
    except OSError:
        pass
    return run, proposal
