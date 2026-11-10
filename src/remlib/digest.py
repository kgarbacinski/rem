"""Turn a Claude Code session transcript (JSONL) into a compact, numbered text digest.

The transcript format is internal to Claude Code and changes between versions, so
this module is the only place that knows about it. Everything is read defensively:
unknown entry types are skipped, missing fields are tolerated.

A digest keeps what consolidation needs and drops what it does not. The user's own
words and the agent's closing report of each turn are kept nearly whole; routine
tool output is cut short; browser plumbing and polling loops are collapsed.
"""
import json
import re
import time
import unicodedata
from pathlib import Path

from .redact import redact

RAW_CAP = 20_000         # nothing longer than this is kept from a single event
USER_CAP = 8000          # the user's own words are the most valuable signal
REPORT_CAP = 6000        # the agent's last message before the user speaks again
ASSISTANT_CAP = 500      # the agent's running commentary
TOOL_INPUT_CAP = 220
MEMORY_WRITE_CAP = 1500  # what a session saved to memory itself
RESULT_CAP = 260
ERROR_CAP = 600
DIGEST_CAP = 160_000     # characters per session
REPEAT_KEEP = 3          # occurrences kept of an event that repeats in a loop

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_NOISE_TAGS = re.compile(
    r"<(local-command-stdout|local-command-caveat|command-message|command-name|command-args|task-notification)>.*?</\1>",
    re.S)
_BOILERPLATE = re.compile(
    r"^(?:Shell cwd was reset to .*|\[This command modified .*|Session cwd remains .*|"
    r"REMINDER: You MUST include the sources.*)$", re.M)
_WS = re.compile(r"[ \t]+")
# Tools whose output is plumbing: the call is worth a line, the result is not.
_QUIET_TOOLS = re.compile(r"claude-in-chrome|Claude_Browser|computer|screenshot|TodoWrite|ToolSearch|TaskCreate|TaskUpdate", re.I)
_SAME = "[the same instructions as in an earlier message, left out]"
_VARIABLE = re.compile(r"\d+|[0-9a-f]{8,}")
_MEMORY_PATH = re.compile(r"/memory/[^/]+\.md$|(?:^|/)CLAUDE\.md$")


def _s(value) -> str:
    """Transcript fields are not always the type we expect."""
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _clip(text: str, cap: int, head_share: float = 0.7) -> str:
    text = text.strip()
    if len(text) <= cap + 40:
        return text  # a cut that saves a few characters only hides the end of a sentence
    head = int(cap * head_share)
    tail = cap - head
    return f"{text[:head]} […{len(text) - cap} chars cut…] {text[-tail:]}"


def _clean(text) -> str:
    text = _REMINDER.sub("", _s(text))
    text = _NOISE_TAGS.sub("", text)
    text = _BOILERPLATE.sub("", text)
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > RAW_CAP:
        # Cut at a word boundary, so a cut can never leave half a token behind.
        text = text[:RAW_CAP].rsplit(None, 1)[0]
    return redact(text)


def _tool_input(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return _clip(_clean(inp), TOOL_INPUT_CAP)
    path = _s(inp.get("file_path"))
    if name in ("Write", "Edit") and path:
        body = _s(inp.get("new_string") or inp.get("content"))
        if _MEMORY_PATH.search(path):
            return f"{path} <<< {_clip(_clean(body), MEMORY_WRITE_CAP)}"
        return f"{path} ({len(body)} chars)"
    for key in ("command", "file_path", "pattern", "query", "url", "prompt", "skill", "path", "action"):
        if isinstance(inp.get(key), str):
            return _clip(_clean(inp[key]), TOOL_INPUT_CAP)
    return _clip(_clean(json.dumps(inp, ensure_ascii=False)), TOOL_INPUT_CAP)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(_s(block.get("text")))
        return "\n".join(parts)
    return ""


def parse_session(path: Path) -> dict:
    """Read one transcript. Never raises: a transcript we cannot read yields no events."""
    try:
        return _parse_session(path)
    except Exception:
        return {"session": path.stem, "title": "", "cwd": "", "start": "", "end": "", "user_turns": 0, "events": []}


def _parse_session(path: Path) -> dict:
    """Returns metadata plus events: dicts with role, text and the day they happened."""
    events = []
    meta = {"session": path.stem, "title": "", "cwd": "", "start": "", "end": "", "user_turns": 0}
    tool_names = {}

    def add(role, text, day):
        if text:
            events.append({"role": role, "text": text, "day": day})

    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return {**meta, "events": events}
    with handle:
        for line in handle:
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            if kind == "ai-title":
                meta["title"] = _s(entry.get("aiTitle")) or meta["title"]
                continue
            if kind not in ("user", "assistant"):
                continue
            if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary"):
                continue
            stamp = _s(entry.get("timestamp"))
            if stamp:
                meta["start"] = meta["start"] or stamp
                meta["end"] = stamp
            day = stamp[:10]
            meta["cwd"] = meta["cwd"] or _s(entry.get("cwd"))
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if kind == "user":
                blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = _clean(block.get("text"))
                        if text:
                            add("USER", text, day)
                            meta["user_turns"] += 1
                    elif block.get("type") == "tool_result":
                        name = tool_names.get(_s(block.get("tool_use_id")), "tool")
                        text = _clean(_result_text(block.get("content")))
                        if block.get("is_error"):
                            add(f"ERROR {name}", text, day)
                        elif not _QUIET_TOOLS.search(name):
                            add(f"RESULT {name}", text, day)
            else:
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        add("ASSISTANT", _clean(block.get("text")), day)
                    elif block.get("type") == "tool_use":
                        # The name becomes part of a digest label: keep it short and plain.
                        name = re.sub(r"[^\w.\-]", "_", _s(block.get("name")))[:60] or "tool"
                        tool_names[_s(block.get("id"))] = name
                        add(f"TOOL {name}", _tool_input(name, block.get("input")), day)
    return {**meta, "events": events}


def _mark_reports(events):
    """The agent's last message before the user speaks again is its report: keep more of it."""
    last_text = None
    for event in events:
        if event["role"] == "ASSISTANT":
            last_text = event
        elif event["role"] == "USER":
            if last_text is not None:
                last_text["report"] = True
            last_text = None
    if last_text is not None:
        last_text["report"] = True


def _collapse_repeats(events):
    """A polling loop says the same thing dozens of times. Keep the first two and the last."""
    def signature(event):
        return event["role"] + "|" + _VARIABLE.sub("#", event["text"][:160].lower())

    seen = {}
    for i, event in enumerate(events):
        if event["role"] == "USER":
            continue  # the user's words are never collapsed
        seen.setdefault(signature(event), []).append(i)
    drop = set()
    for indexes in seen.values():
        if len(indexes) > REPEAT_KEEP + 1:
            drop.update(indexes[REPEAT_KEEP - 1:-1])
    if not drop:
        return events
    out, run = [], 0
    for i, event in enumerate(events):
        if i in drop:
            run += 1
            continue
        if run:
            out.append({"role": "NOTE", "text": f"{run} repetitive events left out here", "day": event["day"]})
            run = 0
        out.append(event)
    return out


def _dedupe_boilerplate(events):
    """A dispatcher repeats the same long instructions in every brief. Keep the first copy."""
    seen = set()
    for event in events:
        if event["role"] != "USER":
            continue
        kept = []
        for part in re.split(r"\n\s*\n", event["text"]):
            key = _VARIABLE.sub("#", part.strip().lower())
            if len(part) >= 200 and key in seen:
                if not kept or kept[-1] != _SAME:
                    kept.append(_SAME)
                continue
            if len(part) >= 200:
                seen.add(key)
            kept.append(part)
        event["text"] = "\n\n".join(kept)


def _cap(event, shrink: float = 1.0) -> str:
    role, text = event["role"], event["text"]
    if role == "USER":
        return _clip(text, USER_CAP, 0.6)  # never shrunk further
    if role == "ASSISTANT":
        if event.get("report"):
            return _clip(text, int(REPORT_CAP * max(shrink, 0.5)), 0.5)
        return _clip(text, int(ASSISTANT_CAP * shrink), 0.5)
    if role.startswith("ERROR"):
        return _clip(text, int(ERROR_CAP * shrink))
    if role.startswith("RESULT"):
        return _clip(text, int(RESULT_CAP * shrink))
    if role.startswith("TOOL"):
        cap = MEMORY_WRITE_CAP + 200 if "<<<" in text else TOOL_INPUT_CAP
        return _clip(text, int(cap * shrink))
    return text


def _fit(events, cap):
    """Shrink a long session to the cap. Tool output goes first, the user's own
    words last, and when events must be dropped they come out of the middle."""
    def size(texts):
        return sum(len(t) + 20 for t in texts)

    for shrink in (1.0, 0.6, 0.35, 0.2):
        texts = [_cap(e, shrink) for e in events]
        if size(texts) <= cap:
            return [dict(e, text=t) for e, t in zip(events, texts)]
    total, middle, dropped = size(texts), len(events) / 2, set()
    # What the user typed is never dropped; the agent's closing reports go last.
    order = [lambda e: e["role"].startswith("RESULT"), lambda e: e["role"].startswith("TOOL"),
             lambda e: e["role"] == "NOTE", lambda e: e["role"] == "ASSISTANT" and not e.get("report"),
             lambda e: e["role"].startswith("ERROR"), lambda e: e["role"] == "ASSISTANT"]
    for wanted in order:
        for i in sorted((i for i, e in enumerate(events) if i not in dropped and wanted(e)),
                        key=lambda i: abs(i - middle)):
            if total <= cap:
                break
            total -= len(texts[i]) + 20
            dropped.add(i)
        if total <= cap:
            break
    kept, run = [], 0
    for i, event in enumerate(events):
        if i in dropped:
            run += 1
            continue
        if run:
            kept.append({"role": "NOTE", "text": f"{run} events left out of this long session", "day": event["day"]})
            run = 0
        kept.append(dict(event, text=texts[i]))
    return kept


def render_digest(parsed: dict, cap: int = DIGEST_CAP) -> str:
    events = [dict(e) for e in parsed["events"]]
    _dedupe_boilerplate(events)
    _mark_reports(events)
    events = _fit(_collapse_repeats(events), cap)
    date = (parsed.get("start") or "")[:10]
    last = (parsed.get("end") or "")[:10]
    span = date if last in ("", date) else f"{date} to {last}"
    title = " ".join(_s(parsed.get("title")).split()) or "untitled"
    lines = [f"# session {parsed['session']} | {span} | {title}"]
    number, day = 0, date
    for event in events:
        if event.get("day") and event["day"] != day:
            day = event["day"]
            number += 1
            lines.append(f"[L{number}] DATE: {day}")
        number += 1
        # Continuation lines are indented, so nothing inside an event can pass for a new
        # "[L7] USER:" line: tool output cannot forge the user's words.
        body = event["text"].replace("\n", "\n    ")
        lines.append(f"[L{number}] {event['role']}: {body}")
    return redact("\n".join(lines)) + "\n"


def list_sessions(transcripts_dir: Path, since_ts: float = 0, settle_seconds: int = 600):
    """Transcripts newer than since_ts, oldest first, skipping ones still being written."""
    now = time.time()
    found = []
    if not transcripts_dir.is_dir():
        return found
    for path in transcripts_dir.glob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime <= since_ts:
            continue
        if now - stat.st_mtime < settle_seconds:
            continue  # probably the live session
        found.append((stat.st_mtime, path))
    found.sort()
    return [p for _, p in found]


def build_digests(paths, out_dir: Path, min_user_turns: int = 2):
    """Write one digest per usable session. Returns a list of metadata dicts."""
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for path in paths:
        parsed = parse_session(path)
        if parsed["user_turns"] < min_user_turns:
            continue
        text = render_digest(parsed)
        target = out_dir / f"{parsed['session']}.txt"
        target.write_text(text, encoding="utf-8")
        made.append({
            "session": parsed["session"],
            "date": (parsed.get("start") or "")[:10],
            "end": (parsed.get("end") or "")[:10],
            "title": parsed.get("title") or "",
            "cwd": parsed.get("cwd") or "",
            "user_turns": parsed["user_turns"],
            "chars": len(text),
            "source_bytes": path.stat().st_size,
            "digest": str(target),
        })
    return made


_LINE = re.compile(r"\A\[L(\d+)\] ([A-Z]+)(?: [\w.\-]{1,60})?: (.*)\Z", re.S)
_HEADER_DAY = re.compile(r"\| (\d{4}-\d{2}-\d{2})")
_DASH = re.compile(r"[‐-―−]")


def normalize(text: str) -> str:
    """Form used to compare a quote with its source: case, spacing, quote marks,
    dashes and Unicode composition do not count."""
    text = unicodedata.normalize("NFC", text)
    for a, b in (("“", '"'), ("”", '"'), ("„", '"'), ("’", "'"), ("‘", "'"), ("…", "...")):
        text = text.replace(a, b)
    text = _DASH.sub("-", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def digest_lines(digest_text: str):
    """Yield (line_number, role, normalized text) for every event in a digest."""
    current = None
    for raw in digest_text.splitlines():
        m = _LINE.match(raw)
        if m:
            if current:
                yield current[0], current[1], normalize(current[2])
            current = [int(m.group(1)), m.group(2), m.group(3)]
        elif current:
            current[2] += "\n" + raw
    if current:
        yield current[0], current[1], normalize(current[2])


def find_quote(quote: str, digest_text: str):
    """Where a quote really occurs: {"line", "role"} or None.

    The quote must sit inside a single event (one USER, ASSISTANT, RESULT... line),
    so text cannot be stitched across speakers. An ellipsis may join parts of the
    same event, in order, when every part is long enough to mean something.
    """
    q = normalize(quote).strip("\"'` ")
    q = q.rstrip(".!?,;:") if len(q) > 14 else q
    if len(q) < 12:
        return None
    parts = [p.strip() for p in re.split(r"\.\.\.|\[…[^\]]*\]", q)]
    parts = [p for p in parts if p]
    if not parts or any(len(p) < 12 for p in parts):
        return None
    day = ""
    head = _HEADER_DAY.search(digest_text[:200])
    if head:
        day = head.group(1)
    for number, role, text in digest_lines(digest_text):
        if role == "DATE":
            day = text.strip()[:10]
            continue
        if role == "NOTE":
            continue
        pos = 0
        for part in parts:
            pos = text.find(part, pos)
            if pos < 0:
                break
            pos += len(part)
        else:
            return {"line": number, "role": role, "day": day}
    return None


def quote_in_digest(quote: str, digest_text: str) -> bool:
    return find_quote(quote, digest_text) is not None
