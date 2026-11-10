#!/usr/bin/env python3
"""Run the benchmark: build each case in a temp folder, dream, apply everything, check.

    python3 evals/run.py                 # all cases
    python3 evals/run.py stale-command   # some cases
    python3 evals/run.py --repeat 3      # each case three times (models vary)

Uses the real `claude` CLI, so it costs what the dreams cost. Nothing outside the
temp folder is read or written.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))
from cases import CASES as EASY  # noqa: E402
try:
    from cases_hard import CASES as HARD  # noqa: E402
except ImportError:
    HARD = []
try:
    from cases_holdout import CASES as HOLDOUT  # noqa: E402
except ImportError:
    HOLDOUT = []
from remlib import memory as mem  # noqa: E402

REM = [sys.executable, str(ROOT / "bin" / "rem")]


def write_session(path: Path, turns, cwd: str, when: float):
    """A minimal transcript in Claude Code's JSONL shape."""
    lines = []
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(when))
    sid = path.stem

    def entry(kind, content):
        lines.append(json.dumps({
            "type": kind, "sessionId": sid, "cwd": cwd, "timestamp": stamp, "uuid": str(uuid.uuid4()),
            "message": {"role": kind, "content": content},
        }))

    for turn in turns:
        role = turn[0]
        if role == "user":
            entry("user", turn[1])
        elif role == "assistant":
            entry("assistant", [{"type": "text", "text": turn[1]}])
        else:
            _, name, tool_input, result = turn
            tool_id = "toolu_" + uuid.uuid4().hex[:20]
            entry("assistant", [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}])
            block = {"type": "tool_result", "tool_use_id": tool_id, "content": result}
            if role == "error":
                block["is_error"] = True
            entry("user", [block])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (when, when))


def build(case, base: Path):
    project = base / "project"
    memory_dir = base / "memory"
    transcripts = base / "transcripts"
    for folder in (project, memory_dir, transcripts):
        folder.mkdir(parents=True)
    for rel, content in (case.get("project") or {}).items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    index = []
    for name, content in case["memory"].items():
        (memory_dir / name).write_text(content, encoding="utf-8")
        fields, _ = mem.parse_frontmatter(content)
        index.append(f"- [{fields.get('name', name)}]({name}) — {fields.get('description', '')}")
    index_text = case.get("index")
    if index_text is None:
        index_text = "\n".join(index) + ("\n" if index else "")
    if case["memory"] or index_text:
        (memory_dir / mem.INDEX).write_text(index_text, encoding="utf-8")
    now = time.time()
    for i, turns in enumerate(case["sessions"]):
        when = now - 86400 * (len(case["sessions"]) - i) - 3600
        write_session(transcripts / f"{uuid.uuid4()}.jsonl", turns, str(project), when)
    return project, memory_dir, transcripts


def all_memory_text(memory_dir: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(memory_dir.glob("*.md")) if p.name != mem.INDEX)


def check(case, spec, ctx):
    (kind, arg), = spec.items()
    memory_dir, project = ctx["memory_dir"], ctx["project"]
    text = all_memory_text(memory_dir)
    claude_md = (project / "CLAUDE.md").read_text(encoding="utf-8") if (project / "CLAUDE.md").exists() else ""
    if kind == "memory_contains":
        return bool(re.search(arg, text))
    if kind == "memory_lacks":
        return not re.search(arg, text) and not re.search(arg, (memory_dir / mem.INDEX).read_text(encoding="utf-8")
                                                          if (memory_dir / mem.INDEX).exists() else "")
    if kind == "claude_md_contains":
        return bool(re.search(arg, claude_md))
    if kind == "claude_md_lacks":
        return not re.search(arg, claude_md)
    if kind == "file_unchanged":
        path = memory_dir / arg
        return path.exists() and path.read_text(encoding="utf-8") == case["memory"][arg]
    if kind == "memory_file_count_max":
        return len([p for p in memory_dir.glob("*.md") if p.name != mem.INDEX]) <= arg
    if kind == "max_ops":
        return ctx["proposed"] <= arg
    if kind == "digest_lacks":
        return not any(re.search(arg, p.read_text(encoding="utf-8")) for p in ctx["run_dir"].glob("digests/*.txt"))
    if kind == "index_ok":
        scan = mem.scan(memory_dir, project)
        return not [i for i in scan["issues"] if i["kind"] in ("orphan", "dangling_link", "duplicate_index_entry", "index_truncated")]
    raise ValueError(f"unknown check {kind}")


def run_case(case, keep=False):
    base = Path(tempfile.mkdtemp(prefix=f"rem-eval-{case['name']}-"))
    project, memory_dir, transcripts = build(case, base)
    env = {**os.environ, "REM_HOME": str(base / "remhome"), "NO_COLOR": "1"}
    common = ["--project", str(project), "--memory-dir", str(memory_dir), "--transcripts-dir", str(transcripts)]
    started = time.time()
    dream = subprocess.run(REM + ["dream", *common], capture_output=True, text=True, env=env)
    result = {"name": case["name"], "category": case["category"], "seconds": round(time.time() - started, 1),
              "checks": [], "proposed": 0, "rejected": 0, "cost": 0.0, "error": ""}
    runs = sorted((base / "remhome" / "projects").glob("*/runs/*"))
    if dream.returncode != 0 or not runs:
        result["error"] = (dream.stderr or dream.stdout)[-400:]
        result["checks"] = [{"check": json.dumps(c), "ok": False} for c in case["checks"]]
        return result
    run_dir = runs[-1]
    proposal = json.loads((run_dir / "proposal.json").read_text())
    result["proposed"] = len([op for op in proposal["ops"] if op["status"] == "proposed"])
    result["rejected"] = len([op for op in proposal["ops"] if op["status"] == "rejected"])
    result["cost"] = proposal["meta"]["usage"]["cost_usd"]
    result["ops"] = [f"{op['status']}:{op['kind']}:{op.get('title', '')[:60]}" for op in proposal["ops"]]
    if proposal.get("failed"):
        result["error"] = proposal.get("summary", "")[:300]
    before = {p.name: p.read_bytes() for p in memory_dir.glob("*.md")}
    if result["proposed"]:
        applied = subprocess.run(REM + ["apply", *common], capture_output=True, text=True, env=env)
        if applied.returncode != 0:
            result["error"] = applied.stderr[-300:]
    ctx = {"memory_dir": memory_dir, "project": project, "proposed": result["proposed"], "run_dir": run_dir}
    for spec in case["checks"]:
        try:
            ok = check(case, spec, ctx)
        except Exception as err:
            ok = False
            result["error"] = f"{type(err).__name__}: {err}"
        result["checks"].append({"check": json.dumps(spec), "ok": ok})
    # Every apply must be exactly reversible.
    if result["proposed"]:
        subprocess.run(REM + ["rollback", *common], capture_output=True, text=True, env=env)
        after = {p.name: p.read_bytes() for p in memory_dir.glob("*.md")}
        result["checks"].append({"check": "rollback restores every byte", "ok": after == before})
    if keep:
        result["dir"] = str(base)
    else:
        shutil.rmtree(base, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*")
    parser.add_argument("--suite", choices=["easy", "hard", "holdout", "all"], default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--keep", action="store_true", help="keep the temp folders for inspection")
    parser.add_argument("--json", help="write full results to this file")
    args = parser.parse_args()
    pool = {"easy": EASY, "hard": HARD, "holdout": HOLDOUT, "all": EASY + HARD}[args.suite]
    chosen = [c for c in pool if not args.cases or c["name"] in args.cases]
    jobs = [c for c in chosen for _ in range(args.repeat)]
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda c: run_case(c, keep=args.keep), jobs))

    by_cat = {}
    total_ok = total = 0
    cases_ok = 0
    print(f"{'case':<26}{'category':<13}{'checks':<9}{'ops':<5}{'cut':<5}{'cost':<8}time")
    for r in results:
        ok = sum(1 for c in r["checks"] if c["ok"])
        n = len(r["checks"])
        total_ok += ok
        total += n
        cases_ok += ok == n
        cat = by_cat.setdefault(r["category"], [0, 0])
        cat[0] += ok
        cat[1] += n
        mark = "PASS" if ok == n else "FAIL"
        print(f"{r['name']:<26}{r['category']:<13}{ok}/{n:<7}{r['proposed']:<5}{r['rejected']:<5}${r['cost']:<7.3f}{r['seconds']}s  {mark}")
        for c in r["checks"]:
            if not c["ok"]:
                print(f"    failed: {c['check']}")
        if r["error"]:
            print(f"    error: {r['error']}")
        if ok != n or args.keep:
            for op in r.get("ops", []):
                print(f"    {op}")
            if r.get("dir"):
                print(f"    kept: {r['dir']}")
    print()
    for cat, (ok, n) in sorted(by_cat.items()):
        print(f"{cat:<13}{ok}/{n}  {100 * ok / n:.0f}%")
    cost = sum(r["cost"] for r in results)
    print(f"\nchecks {total_ok}/{total} ({100 * total_ok / max(total, 1):.1f}%), cases {cases_ok}/{len(results)}, "
          f"${cost:.2f} total, ${cost / max(len(results), 1):.3f} per dream, {time.time() - started:.0f}s wall")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
