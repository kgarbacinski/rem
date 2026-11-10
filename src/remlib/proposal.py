"""Check a model-written proposal with plain code before a person ever sees it.

Nothing here trusts the model. Each kind of operation may only contain the file
changes that kind is for; every piece of evidence is looked up in the transcript
digest or in the repository; every path is confined and may not be a symlink;
new content is scanned for credentials and control characters; operations that
collide are dropped. What survives is safe to show and safe to apply.
"""
import os
import re
from pathlib import Path

from . import memory as mem
from .digest import find_quote, normalize
from .paths import sha_bytes
from .redact import MARK, find_secrets, new_secrets, redact

KINDS = ("fix", "merge", "prune", "new", "reindex", "promote_rule", "promote_skill")
MEMORY_TYPES = ("user", "feedback", "project", "reference")
_FILE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._\-]{0,120}\.md\Z")
_SLUG = re.compile(r"\A[a-z0-9][a-z0-9\-]{1,48}\Z")
RULE_MAX = 700
SKILL_MAX = 8000
MEMORY_MAX = 12000
ENTRY_MAX = 220

# What each kind may do: (target, action) pairs, and which must be present.
ALLOWED = {
    "fix": {("memory", "write"), ("memory", "index")},
    "merge": {("memory", "write"), ("memory", "delete")},
    "prune": {("memory", "delete")},
    "new": {("memory", "write")},
    "reindex": {("memory", "index"), ("memory", "unindex")},
    "promote_rule": {("claude_md", "append"), ("memory", "write"), ("memory", "delete")},
    "promote_skill": {("skill", "write"), ("memory", "write"), ("memory", "delete")},
}
REQUIRED = {
    "fix": ("memory", "write"), "merge": ("memory", "delete"), "prune": ("memory", "delete"),
    "new": ("memory", "write"), "promote_rule": ("claude_md", "append"), "promote_skill": ("skill", "write"),
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")


def clean_text(text: str) -> str:
    """Drop control and direction-override characters; they can hide text in a terminal."""
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def claude_md_path(project_dir: Path) -> Path:
    for cand in (project_dir / "CLAUDE.md", project_dir / ".claude" / "CLAUDE.md"):
        if cand.exists():
            return cand
    return project_dir / "CLAUDE.md"


def skill_path(project_dir: Path, slug: str) -> Path:
    return project_dir / ".claude" / "skills" / slug / "SKILL.md"


def resolve(change: dict, memory_dir: Path, project_dir: Path) -> Path:
    target = change.get("target")
    if target == "memory":
        return memory_dir / change["path"]
    if target == "claude_md":
        return claude_md_path(project_dir)
    return skill_path(project_dir, change["path"])


def file_key(path: Path) -> str:
    """One key per real file, so `Note.md` and `note.md` collide where the disk says they do."""
    return os.path.normcase(os.path.realpath(str(path))).casefold()


def _confined(path: Path, root: Path) -> bool:
    """The path, and every existing folder on the way to it, stays inside root; no symlinks."""
    if path.is_symlink():
        return False
    try:
        real_root = root.resolve()
        real = path.resolve()
    except OSError:
        return False
    return real == real_root or real_root in real.parents


def default_index_entry(name: str, content: str) -> str:
    fields, _ = mem.parse_frontmatter(content)
    title = (fields.get("name") or name[:-3]).replace("[", "(").replace("]", ")")
    return make_entry(title, name, fields.get("description") or "")


def make_entry(title: str, name: str, hook: str) -> str:
    """An index line that always keeps its link, whatever gets truncated."""
    title = " ".join(title.split())[:80]
    head = f"- [{title}]({name})"
    hook = " ".join(hook.split())
    room = ENTRY_MAX - len(head) - 3
    if hook and room > 10:
        head += " — " + (hook if len(hook) <= room else hook[: room - 1].rstrip() + "…")
    return head


def _normal_entry(entry: str, name: str, content: str) -> str:
    entry = clean_text(entry or "").strip().splitlines()
    entry = entry[0].strip() if entry else ""
    links = mem.index_links(entry)
    if not links or mem.link_name(links[0][1]) != name:
        return default_index_entry(name, content) if content else ""
    match = mem._LINK.search(entry)
    return make_entry(links[0][0], name, entry[match.end():].lstrip(" —–-:"))


def check_repo_evidence(project_dir: Path, rel: str, quote: str) -> str:
    """Confirm a claim about the repository. Returns how it was confirmed, or ''."""
    try:
        root = project_dir.resolve()
        target = (root / rel).resolve()
        if os.path.isabs(rel) or (target != root and root not in target.parents):
            return ""
        if not quote:
            return "" if target.exists() or (root / rel).is_symlink() else "path is absent"
        if not target.is_file() or target.stat().st_size > 2_000_000:
            return ""
        text = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    needle, hay = normalize(quote), normalize(text)
    return "in file" if len(needle) >= 12 and needle in hay else ""


def _check_change(change, memory_files, memory_dir, project_dir, known_text):
    """Return an error string, or None when the change is acceptable. Normalises in place."""
    target, action = change.get("target"), change.get("action")
    path = change.get("path")
    if not isinstance(path, str) or not isinstance(change.get("content") or "", str):
        return "malformed change"
    path = path.strip()
    change["path"] = path
    content = clean_text(change.get("content") or "")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        return "content is not valid text"
    change["content"] = content
    change["index_entry"] = change.get("index_entry") if isinstance(change.get("index_entry"), str) else ""
    if MARK in content or MARK in change["index_entry"]:
        return "content contains a redaction mark where the real text should be"
    if new_secrets(content + "\n" + change["index_entry"], known_text):
        return "content looks like it contains a credential"

    if target == "memory":
        if not _FILE.match(path) or path.casefold() == mem.INDEX.casefold():
            return f"`{path}` is not a valid memory file name"
        full = memory_dir / path
        if full.is_symlink() or (memory_dir.exists() and not _confined(full, memory_dir)):
            return f"`{path}` is a link or leads outside the memory directory"
        existing = {name.casefold(): name for name in memory_files}
        actual = existing.get(path.casefold())
        if actual and actual != path:
            return f"`{path}` differs only by case from the existing `{actual}`"
        if action == "write":
            fields, body = mem.parse_frontmatter(content)
            if not fields.get("name") or not fields.get("description"):
                return "memory file needs `name` and `description` in its frontmatter"
            if fields.get("type") not in MEMORY_TYPES:
                return f"memory `type` must be one of {', '.join(MEMORY_TYPES)}"
            if len(body.strip()) < 20:
                return "memory body is empty"
            if len(content) > MEMORY_MAX:
                return "memory file is too long; split it or shorten it"
            if path in memory_files:
                if memory_files[path].get("binary"):
                    return f"`{path}` is not UTF-8 text; rem will not rewrite it"
                if memory_files[path]["text"].strip() == content.strip():
                    return "the new content is identical to the current file"
            change["index_entry"] = _normal_entry(change["index_entry"], path, content)
        elif action == "delete":
            if path not in memory_files:
                return f"cannot delete `{path}`: no such memory file"
            change["content"], change["index_entry"] = "", ""
        elif action == "index":
            if path not in memory_files:
                return f"cannot re-index `{path}`: no such memory file"
            entry = _normal_entry(change["index_entry"], path, "")
            if not entry:
                return "index_entry must link to the file"
            change["index_entry"], change["content"] = entry, ""
        elif action == "unindex":
            if path in memory_files or full.exists():
                return f"`{path}` exists; only entries for missing files can be dropped from the index"
            change["content"], change["index_entry"] = "", ""
        else:
            return f"unsupported action `{action}` for a memory file"
    elif target == "claude_md":
        if action != "append":
            return "CLAUDE.md can only be appended to"
        if not content.strip() or len(content) > RULE_MAX:
            return f"a promoted rule must be 1 to {RULE_MAX} characters"
        rule_lines = [ln for ln in content.strip().splitlines() if ln.strip()]
        if len(rule_lines) > 4 or any(ln.lstrip().startswith(("#", "---", "```")) for ln in rule_lines):
            return "a promoted rule is a few plain sentences, not headings, frontmatter or code"
        target_path = claude_md_path(project_dir)
        if not _confined(target_path, project_dir):
            return "CLAUDE.md is a link or leads outside the project"
        existing = ""
        if target_path.exists():
            raw = target_path.read_bytes()
            try:
                existing = raw.decode("utf-8")
            except UnicodeDecodeError:
                return "CLAUDE.md is not UTF-8 text; rem will not edit it"
        if re.sub(r"\s+", " ", content.strip().lower()) in re.sub(r"\s+", " ", existing.lower()):
            return "CLAUDE.md already contains this rule"
        change["path"], change["index_entry"] = "CLAUDE.md", ""
    elif target == "skill":
        if action != "write":
            return "a skill can only be created"
        if not _SLUG.match(path):
            return f"`{path}` is not a valid skill name (lowercase letters, digits, hyphens)"
        full = skill_path(project_dir, path)
        if full.exists() or full.is_symlink() or full.parent.exists():
            return f"skill `{path}` already exists; rem never overwrites a skill"
        if not _confined(full, project_dir):
            return "the skills folder is a link or leads outside the project"
        fields, body = mem.parse_frontmatter(content)
        if not fields.get("name") or not fields.get("description"):
            return "a skill needs `name` and `description` in its frontmatter"
        if len(content) > SKILL_MAX or len(body.strip()) < 40:
            return "skill body is empty or too long"
        change["index_entry"] = ""
    else:
        return f"unknown target `{target}`"
    return None


def revise_change(op: dict, path: str, content: str, memory_dir: Path, project_dir: Path) -> str:
    """Replace the content of one change with a second reader's trimmed version.
    Returns '' on success, or the reason the revision was refused."""
    targets = [c for c in op.get("changes", []) if c.get("path") == path and c.get("action") in ("write", "append")]
    if len(targets) != 1 and path == "":
        targets = [c for c in op.get("changes", []) if c.get("action") in ("write", "append")]
    if len(targets) != 1:
        return "no single change to revise"
    change = targets[0]
    if not isinstance(content, str) or not content.strip():
        return "empty revision"
    # A revision may only delete: same frontmatter, and every sentence already in the original.
    old = change["content"]
    old_body, new_body = mem.parse_frontmatter(old)[1], mem.parse_frontmatter(content)[1]
    if old[:len(old) - len(old_body)].strip() != content[:len(content) - len(new_body)].strip():
        return "a revision may not change the frontmatter"
    if len(content) > len(old):
        return "a revision may trim, not expand"
    original = normalize(old_body)
    for sentence in re.split(r"(?<=[.!?])\s+|\n", new_body):
        if normalize(sentence).strip("*_ ") and normalize(sentence) not in original:
            return "a revision may only remove sentences, not reword them"
    memory_files = mem.load(memory_dir)["files"]
    known = "\n".join(info["text"] for info in memory_files.values())
    trial = dict(change, content=content)
    error = _check_change(trial, memory_files, memory_dir, project_dir, known)
    if error:
        return error
    change["content"], change["index_entry"] = trial["content"], trial["index_entry"]
    return ""


def _check_evidence(op, digests, project_dir, memory_dir):
    """Keep only evidence a program can confirm. Returns (kept, dropped_count)."""
    good, dropped = [], 0
    for ev in op.get("evidence") if isinstance(op.get("evidence"), list) else []:
        if not isinstance(ev, dict):
            dropped += 1
            continue
        quote = " ".join(clean_text(str(ev.get("quote") or "")).split())
        session, file = str(ev.get("session") or "").strip(), str(ev.get("file") or "").strip()
        if file.startswith("memory:"):
            # "this memory, which another one links to, does not exist"
            name = file[len("memory:"):].strip()
            if not quote and _FILE.match(name) and not (memory_dir / name).exists():
                good.append({"session": "", "file": file, "quote": "", "checked": "memory is absent", "role": "REPO"})
            else:
                dropped += 1
            continue
        if file:
            verdict = check_repo_evidence(project_dir, file, quote)
            if verdict and find_secrets(quote):
                verdict = ""  # never show a credential from the repository as evidence
            if verdict:
                good.append({"session": "", "file": file, "quote": quote, "checked": verdict, "role": "REPO"})
            else:
                dropped += 1
            continue
        hit = find_quote(quote, digests.get(session, "")) if session in digests else None
        if hit:
            good.append({"session": session, "file": "", "quote": quote, "checked": "in transcript",
                         "role": hit["role"], "line": hit["line"], "day": hit.get("day", "")})
        else:
            dropped += 1
    return good, dropped


def _evidence_problem(kind, good, changes, memory_files):
    """Each kind has its own bar for what counts as enough evidence."""
    session_ev = [e for e in good if e["session"]]
    repo_quotes = [e for e in good if e["file"] and e["checked"] == "in file"]
    # "It no longer exists" only counts when the memory being changed actually mentions it.
    current = "\n".join(memory_files.get(c.get("path"), {}).get("text", "")
                        for c in changes if c.get("target") == "memory")

    def mentioned(e):
        name = e["file"]
        needle = f"[[{name[7:-3]}]]" if name.startswith("memory:") else name.strip("/")
        return bool(needle) and needle in current

    repo_absent = [e for e in good if e["file"] and e["checked"] in ("path is absent", "memory is absent")
                   and mentioned(e)]
    if kind == "new":
        if not session_ev:
            return "a new memory needs a quote from a session transcript"
    elif kind == "fix":
        if not (session_ev or repo_quotes or repo_absent):
            return "a fix needs evidence from a transcript or from the repository"
    elif kind in ("promote_rule", "promote_skill"):
        user_ev = [e for e in session_ev if e["role"] == "USER"]
        if not user_ev:
            return "a promotion needs a quote of the user's own words"
        sessions = {e["session"] for e in session_ev}
        rewrites_existing = any(
            c.get("target") == "memory" and c.get("path") in memory_files for c in changes)
        if len(sessions) < 2 and not rewrites_existing:
            return "a promotion needs evidence from two sessions, or a memory that already recorded the lesson"
    return None


def validate(proposal: dict, memory_dir: Path, project_dir: Path, digests: dict, base: dict = None) -> dict:
    """Annotate every operation with an id, a status and file hashes. Mutates and returns."""
    memory_files = mem.load(memory_dir)["files"]
    known_text = "\n".join(info["text"] for info in memory_files.values())
    touched = {}
    raw_ops = proposal.get("ops") if isinstance(proposal.get("ops"), list) else []
    ops = [op for op in raw_ops if isinstance(op, dict)]
    proposal["summary"] = clean_text(str(proposal.get("summary") or ""))
    for number, op in enumerate(ops, 1):
        op["id"] = f"op{number}"
        op["status"] = "proposed"
        op["notes"] = []
        for field in ("title", "rationale"):
            op[field] = redact(" ".join(clean_text(str(op.get(field) or "")).split()))[:600]
        if op.get("confidence") not in ("high", "medium", "low"):
            op["confidence"] = "low"
        problems = []
        kind = op.get("kind")
        changes = op.get("changes") if isinstance(op.get("changes"), list) else []
        changes = [c for c in changes if isinstance(c, dict)]
        for c in changes:
            for key in ("target", "action"):
                if not isinstance(c.get(key), str):
                    c[key] = ""
        op["changes"] = changes
        if kind not in KINDS:
            problems.append(f"unknown kind `{kind}`")
        if not changes:
            problems.append("no file changes")

        good, dropped = _check_evidence(op, digests, project_dir, memory_dir)
        op["evidence"] = good
        if dropped:
            op["notes"].append(f"{dropped} piece(s) of evidence could not be confirmed and were removed")

        if kind in KINDS:
            pairs = [(c.get("target"), c.get("action")) for c in changes]
            for pair in pairs:
                if pair not in ALLOWED[kind]:
                    problems.append(f"a {kind} may not {pair[1]} a {pair[0]} file")
            if kind in REQUIRED and REQUIRED[kind] not in pairs:
                problems.append(f"a {kind} must include a {REQUIRED[kind][0]} {REQUIRED[kind][1]}")
            if pairs.count(("claude_md", "append")) > 1 or pairs.count(("skill", "write")) > 1:
                problems.append("only one rule or skill per operation")

        if not problems:
            for change in changes:
                error = _check_change(change, memory_files, memory_dir, project_dir, known_text)
                if error:
                    problems.append(error)
                    continue
                path = resolve(change, memory_dir, project_dir)
                key = file_key(path)
                if key in touched:
                    problems.append(f"touches {path.name}, which {touched[key]} already changes")
                # Compare against what the dream read, not what is on disk minutes later.
                change["base_sha"] = base[str(path)] if base and str(path) in base else sha_bytes(path)
                change["_key"] = key
            keys = [c.get("_key") for c in changes if c.get("_key")]
            if len(keys) != len(set(keys)):
                problems.append("changes the same file twice")

        if not problems and kind in KINDS:
            writes = [c for c in changes if c["target"] == "memory" and c["action"] == "write"]
            if kind == "new" and any(c["path"] in memory_files for c in writes):
                problems.append("`new` would overwrite an existing memory file; use `fix` or `merge`")
            if kind == "fix" and any(c["path"] not in memory_files for c in writes):
                problems.append("`fix` must rewrite an existing memory file; use `new` for a new one")
            if kind == "merge" and len(writes) != 1:
                problems.append("a merge writes exactly one file and deletes the others")
            if kind == "merge":
                existing = {c["path"] for c in changes if c["target"] == "memory" and c["path"] in memory_files}
                if len(existing) < 2:
                    problems.append("a merge must combine at least two existing memory files")
            problem = _evidence_problem(kind, good, changes, memory_files)
            if problem:
                problems.append(problem)

        for change in changes:
            change.pop("_key", None)
        if problems:
            op["status"] = "rejected"
            op["rejected_by"] = "validator"
            op["reason"] = "; ".join(dict.fromkeys(problems))
        else:
            for change in changes:
                touched[file_key(resolve(change, memory_dir, project_dir))] = op["id"]

    proposal["ops"] = ops
    proposal["index_forecast"] = forecast_index(proposal, memory_dir)
    return proposal


def apply_index_changes(index_text: str, changes) -> str:
    """Replace, add or remove index lines. A line belongs to a file when the first
    link on it points at that file; a mention elsewhere on a line is left alone.
    Every other line keeps its exact bytes, including its line ending."""
    newline = "\r\n" if "\r\n" in index_text else "\n"
    raw = re.findall(r"[^\n]*\n|[^\n]+", index_text)

    def body(line):
        return line.rstrip("\r\n")

    def owner(line):
        links = mem.index_links(body(line))
        return mem.link_name(links[0][1]) if links else None

    for change in changes:
        if change.get("target") != "memory":
            continue
        name = change["path"]
        if change["action"] in ("delete", "unindex"):
            raw = [ln for ln in raw if owner(ln) != name]
            continue
        entry = change.get("index_entry")
        if not entry:
            continue
        hits = [i for i, ln in enumerate(raw) if owner(ln) == name]
        if hits:
            raw[hits[0]] = entry + (raw[hits[0]][len(body(raw[hits[0]])):] or newline)
            for extra in reversed(hits[1:]):
                del raw[extra]
        else:
            if raw and raw[-1] == body(raw[-1]):
                raw[-1] += newline
            at = len(raw)
            while at > 0 and not body(raw[at - 1]).strip():
                at -= 1
            raw.insert(at, entry + newline)
    return "".join(raw)


def index_line_diff(index_text: str, change) -> list:
    """The MEMORY.md lines one change removes and adds, for display."""
    after = apply_index_changes(index_text, [change])
    before_lines, after_lines = index_text.splitlines(), after.splitlines()
    removed = [ln for ln in before_lines if ln not in after_lines]
    added = [ln for ln in after_lines if ln not in before_lines]
    return [f"-{ln}" for ln in removed] + [f"+{ln}" for ln in added]


def forecast_index(proposal: dict, memory_dir: Path) -> dict:
    """What MEMORY.md would look like if every proposed operation were applied."""
    index_path = memory_dir / mem.INDEX
    before = index_path.read_text(encoding="utf-8", errors="replace") if index_path.exists() else ""
    changes = [c for op in proposal.get("ops", []) if op.get("status") == "proposed" for c in op.get("changes", [])]
    after = apply_index_changes(before, changes)
    return {
        "lines_before": len(before.splitlines()),
        "lines_after": len(after.splitlines()),
        "bytes_before": len(before.encode("utf-8")),
        "bytes_after": len(after.encode("utf-8")),
        "over_limit": len(after.splitlines()) > mem.INDEX_MAX_LINES or len(after.encode("utf-8")) > mem.INDEX_MAX_BYTES,
    }


def index_repairs(proposal: dict, memory_dir: Path) -> list:
    """Index fixes that need no judgement: entries for missing files, files with no
    entry, entries listed twice. Built by code, so they are always right."""
    data = mem.load(memory_dir)
    files, index_text = data["files"], data["index"]
    live = [op for op in proposal.get("ops", []) if op.get("status") == "proposed"]
    handled = {c["path"] for op in live for c in op.get("changes", []) if c.get("target") == "memory"}
    created = {c["path"] for op in live for c in op.get("changes", [])
               if c.get("target") == "memory" and c.get("action") == "write"}
    owners = []
    for line in index_text.splitlines():
        links = mem.index_links(line)
        if links:
            owners.append((mem.link_name(links[0][1]), line))
    linked = [name for name, _ in owners]
    repairs = []

    def op(title, rationale, change):
        change.setdefault("content", "")
        change.setdefault("index_entry", "")
        change["base_sha"] = sha_bytes(memory_dir / change["path"])
        repairs.append({"kind": "reindex", "title": title, "rationale": rationale, "confidence": "high",
                        "evidence": [], "changes": [change], "status": "proposed", "notes": [],
                        "source": "program"})

    for target in dict.fromkeys(linked):
        if target not in files and target not in created and _FILE.match(target) \
                and not (memory_dir / target).exists():
            op(f"Drop index entry for missing file {target}",
               "MEMORY.md lists this file but it does not exist, so the line only costs attention.",
               {"target": "memory", "path": target, "action": "unindex"})
    for name, info in files.items():
        if name in handled or not _FILE.match(name) or (memory_dir / name).is_symlink():
            continue
        if name not in linked:
            op(f"Add {name} to the index",
               "The file exists but MEMORY.md does not list it, so no session can find it.",
               {"target": "memory", "path": name, "action": "index",
                "index_entry": default_index_entry(name, info["text"])})
        elif linked.count(name) > 1:
            first = next(line for owner, line in owners if owner == name)
            entry = _normal_entry(first, name, info["text"]) or default_index_entry(name, info["text"])
            op(f"Remove duplicate index entries for {name}",
               "MEMORY.md lists this file more than once.",
               {"target": "memory", "path": name, "action": "index", "index_entry": entry})
    return repairs


def counts(proposal: dict) -> dict:
    out = {}
    for op in proposal.get("ops", []):
        if op.get("status") == "proposed":
            out[op["kind"]] = out.get(op["kind"], 0) + 1
    return out
