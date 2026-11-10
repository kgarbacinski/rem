"""Read Claude Code's auto-memory directory and check its health without any model."""
import os
import re
import time
from pathlib import Path

INDEX = "MEMORY.md"
INDEX_MAX_LINES = 200       # Claude Code loads only this much of MEMORY.md
INDEX_MAX_BYTES = 25_000
FILE_SOFT_MAX = 6_000
STALE_DAYS = 60

_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+\.md)\)")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
_BACKTICK = re.compile(r"`([^`\n]{3,200})`")
_HOME_PATH = re.compile(r"(?<![\w`/])(~/[\w.\-/]+)")
_WIKILINK = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9._\-]{1,100})\]\]")
_WORD = re.compile(r"[\w]{3,}", re.U)


def parse_frontmatter(text: str):
    """Top-level keys, plus keys nested one level under `metadata:` (both layouts exist)."""
    m = _FRONT.match(text)
    if not m:
        return {}, text
    fields, nested = {}, {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"')
        if line.startswith((" ", "\t")):
            nested[key.strip()] = value
        else:
            fields[key.strip()] = value
    for key, value in nested.items():
        fields.setdefault(key, value)
        if not fields.get(key):
            fields[key] = value
    return fields, text[m.end():]


def load(memory_dir: Path) -> dict:
    """All memory files as {name: {text, fields, body, mtime}} plus the index text."""
    files = {}
    index_text = ""
    if not memory_dir.is_dir():
        return {"files": files, "index": index_text}
    for path in sorted(memory_dir.glob("*.md")):
        try:
            raw = path.read_bytes()
            mtime = path.stat().st_mtime
        except OSError:
            continue
        binary = False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text, binary = raw.decode("utf-8", errors="replace"), True
        text = text.replace("\r\n", "\n")
        if path.name == INDEX:
            index_text = text
            continue
        fields, body = parse_frontmatter(text)
        files[path.name] = {"text": text, "fields": fields, "body": body, "mtime": mtime, "binary": binary}
    return {"files": files, "index": index_text}


def index_links(index_text: str):
    return [(m.group(1), m.group(2)) for m in _LINK.finditer(index_text)]


def link_name(target: str) -> str:
    """`./a.md` and `a.md` are the same file."""
    while target.startswith("./"):
        target = target[2:]
    return target


def _shingles(text: str):
    words = [w.lower() for w in _WORD.findall(text)]
    return {" ".join(words[i:i + 3]) for i in range(max(0, len(words) - 2))}


def _exists_loosely(target: Path) -> bool:
    """True when the path exists, or when it is the start of something that does
    (a name with spaces gets cut at the first space in running text)."""
    try:
        if target.exists():
            return True
        parent = target.parent
        if parent.is_dir():
            return any(child.name.startswith(target.name) for child in parent.iterdir())
    except OSError:
        return True
    return False


def _dead_paths(body: str, project_dir: Path):
    """Paths a memory points at that are gone. Deliberately conservative: a path is
    reported only when we can tell where it should be. Paths on other machines,
    in other repositories, or with placeholders are left alone."""
    home = str(Path.home())
    candidates = [m.group(1) for m in _BACKTICK.finditer(body) if "/" in m.group(1)]
    candidates += [m.group(1) for m in _HOME_PATH.finditer(body)]
    dead, seen = [], set()
    for raw in candidates:
        raw = raw.strip().rstrip(".,;:)")
        if raw in seen or len(raw) < 5 or any(ch in raw for ch in "*{}<>$|=") or "..." in raw or "://" in raw:
            continue
        if raw.startswith("..") or (" " in raw and not raw.startswith(("~", "/"))):
            continue  # a command line or a path relative to somewhere else
        seen.add(raw)
        if raw.startswith("~"):
            target = Path(os.path.expanduser(raw))
        elif raw.startswith("/"):
            if not raw.startswith(home):
                continue  # system paths are as likely to be on a server as here
            target = Path(raw)
        else:
            parts = Path(raw).parts
            if parts and parts[0] == project_dir.name:
                parts = parts[1:]
            if len(parts) < 2 or not (project_dir / parts[0]).is_dir():
                continue  # top folder is not in this project: another repo or a remote
            target = project_dir.joinpath(*parts)
        if not _exists_loosely(target):
            dead.append(raw)
    return dead


def scan(memory_dir: Path, project_dir: Path) -> dict:
    """Deterministic health report. Costs nothing, runs in milliseconds."""
    mem = load(memory_dir)
    files, index_text = mem["files"], mem["index"]
    now = time.time()
    issues = []

    def issue(kind, severity, target, detail):
        issues.append({"kind": kind, "severity": severity, "file": target, "detail": detail})

    index_lines = index_text.count("\n") + (1 if index_text and not index_text.endswith("\n") else 0)
    index_bytes = len(index_text.encode("utf-8"))
    if index_lines > INDEX_MAX_LINES or index_bytes > INDEX_MAX_BYTES:
        issue("index_truncated", "high", INDEX,
              f"{index_lines} lines / {index_bytes} bytes: Claude Code loads only the first "
              f"{INDEX_MAX_LINES} lines or {INDEX_MAX_BYTES} bytes, the rest is never seen")
    elif index_lines > INDEX_MAX_LINES * 0.8 or index_bytes > INDEX_MAX_BYTES * 0.8:
        issue("index_near_limit", "medium", INDEX, f"{index_lines} lines / {index_bytes} bytes, close to the load limit")

    linked = []
    for line in index_text.splitlines():
        links = index_links(line)
        if links:
            linked.append(link_name(links[0][1]))  # a line belongs to its first link
    linked_set = set(linked)
    for name in files:
        if name not in linked_set:
            issue("orphan", "high", name, "not listed in MEMORY.md, so no session knows it exists")
    for target in linked_set:
        if target not in files and "/" not in target:
            issue("dangling_link", "medium", target, "listed in MEMORY.md but the file is missing")
    for target in {t for t in linked if linked.count(t) > 1}:
        issue("duplicate_index_entry", "low", target, "listed more than once in MEMORY.md")

    for name, info in files.items():
        fields = info["fields"]
        missing = [k for k in ("name", "description", "type") if not fields.get(k)]
        if missing:
            issue("frontmatter", "low", name, "missing " + ", ".join(missing))
        size = len(info["text"].encode("utf-8"))
        if size > FILE_SOFT_MAX:
            issue("oversized", "low", name, f"{size} bytes: long memories are read less carefully")
        age = int((now - info["mtime"]) / 86400)
        if age > STALE_DAYS and fields.get("type") in ("project", "reference", ""):
            issue("stale", "low", name, f"untouched for {age} days, worth re-checking against reality")
        for link in dict.fromkeys(_WIKILINK.findall(info["body"])):
            if f"{link}.md" not in files and link not in files:
                issue("dead_link", "low", name, f"links to [[{link}]], and there is no such memory")
        for dead in _dead_paths(info["body"], project_dir)[:5]:
            issue("dead_reference", "medium", name, f"mentions `{dead}`, which does not exist any more")

    names = list(files)
    shingle_cache = {n: _shingles(files[n]["body"]) for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sa, sb = shingle_cache[a], shingle_cache[b]
            if len(sa) < 8 or len(sb) < 8:
                continue
            overlap = len(sa & sb) / min(len(sa), len(sb))
            if overlap >= 0.45:
                issue("near_duplicate", "medium", a, f"overlaps {int(overlap * 100)}% with {b}")

    # Many small issues of one kind should not drown out a single serious one.
    weights = {"high": (12, 36), "medium": (6, 24), "low": (2, 8)}
    per_kind = {}
    for item in issues:
        points, cap = weights[item["severity"]]
        per_kind[item["kind"]] = min(cap, per_kind.get(item["kind"], 0) + points)
    score = max(0, 100 - sum(per_kind.values()))
    return {
        "memory_dir": str(memory_dir),
        "files": len(files),
        "bytes": sum(len(f["text"].encode("utf-8")) for f in files.values()),
        "index_lines": index_lines,
        "index_bytes": index_bytes,
        "score": score,
        "issues": issues,
    }
