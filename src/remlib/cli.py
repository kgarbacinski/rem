"""Command line: rem status | dream | review | apply | discard | rollback | night | schedule."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, apply as ap, digest as dg, memory as mem, night, pipeline, render
from .paths import Lock, Project
from .proposal import counts


def _project(args) -> Project:
    return Project(args.project or os.getcwd(), memory_dir=args.memory_dir, transcripts_dir=args.transcripts_dir)


def _load_pending(project):
    run = project.pending_run()
    if not run:
        return None, None
    try:
        proposal = json.loads((run / "proposal.json").read_text(encoding="utf-8"))
        if not isinstance(proposal, dict) or not isinstance(proposal.get("ops"), list):
            raise ValueError("not a proposal")
    except (OSError, ValueError):
        state = project.load_state()
        state.pop("pending_run", None)
        project.save_state(state)
        print(f"rem: the waiting proposal in {run} is unreadable and was set aside. Run: rem dream", file=sys.stderr)
        return None, None
    proposal["ops"] = [op for op in proposal["ops"] if isinstance(op, dict) and isinstance(op.get("changes"), list)
                       and all(isinstance(op.get(k), str) for k in ("id", "status", "kind"))]
    meta = proposal.setdefault("meta", {})
    if not isinstance(meta, dict) or (meta.get("memory_dir") and meta["memory_dir"] != str(project.memory_dir)):
        print("rem: the waiting proposal belongs to a different memory directory; ignoring it.", file=sys.stderr)
        return None, None
    # Files are changed where the dream looked, whatever folder this command runs in.
    if meta.get("project") and Path(meta["project"]).is_dir():
        project.cwd = Path(meta["project"])
    return run, proposal


def cmd_status(args):
    project = _project(args)
    scan = mem.scan(project.memory_dir, project.cwd)
    state = project.load_state()
    new = dg.list_sessions(project.transcripts_dir, since_ts=project.seen_mtime())
    if args.json:
        print(json.dumps({**scan, "sessions_since_last_dream": len(new), "last_dream": state.get("last_dream")}, indent=2))
        return 0
    render.print_scan(scan)
    last = state.get("last_dream") or "never"
    print(f"last dream: {last}; sessions since: {len(new)}")
    run, proposal = _load_pending(project)
    if proposal:
        live = [op for op in proposal["ops"] if op["status"] == "proposed"]
        print(f"a dream from {proposal['meta']['created']} has {len(live)} change(s) waiting: rem review")
    return 0


def cmd_dream(args):
    project = _project(args)
    if not project.memory_dir.is_dir() and not project.transcripts_dir.is_dir():
        print(f"rem: no Claude Code memory or sessions found for {project.cwd}", file=sys.stderr)
        return 1
    with Lock(project):
        if args.fresh:
            state = project.load_state()
            for key in ("seen", "audited_memory_sha", "audited_at", "file_audits"):
                state.pop(key, None)
            project.save_state(state)
        run, proposal = pipeline.dream(
            project, days=args.days, max_sessions=args.max_sessions,
            skeptic=not args.no_second_read, instructions=args.focus or "", max_reviews=args.max_reviews,
            log=(lambda *_: None) if args.quiet else print,
        )
    if not args.quiet:
        print()
        render.print_proposal(proposal, project, brief=args.brief)
        usage = proposal["meta"]["usage"]
        print(f"\n{usage['calls']} model calls, ${usage['cost_usd']} at API prices, {proposal['meta']['wall_seconds']}s. "
              f"Report: {run / 'report.md'}")
    if proposal.get("failed"):
        print(f"rem: the dream did not finish: {proposal.get('summary', '')}", file=sys.stderr)
        return 1
    return 0


def cmd_review(args):
    project = _project(args)
    run, proposal = _load_pending(project)
    if not proposal:
        print("No dream is waiting for review. Run: rem dream")
        return 0
    if args.markdown:
        print(render.report_markdown(proposal, project))
    elif args.json:
        print(json.dumps(proposal, indent=2, ensure_ascii=False))
    else:
        render.print_proposal(proposal, project, brief=args.brief)
    return 0


def cmd_apply(args):
    project = _project(args)
    run, proposal = _load_pending(project)
    if not proposal:
        print("No dream is waiting. Run: rem dream")
        return 0
    only = set(args.ops) if args.ops else None
    if only:
        known = {op["id"] for op in proposal["ops"]}
        unknown = only - known
        if unknown:
            print(f"rem: unknown operation id(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
    with Lock(project):
        chosen = ap.select(proposal, only=only, min_confidence=args.min_confidence)
        if not chosen:
            print("Nothing matched. Operations that were filtered out cannot be applied.")
            return 1 if only else 0
        result = ap.apply_ops(project, run, proposal, chosen)
    if result.get("error"):
        print(f"rem: apply stopped early: {result['error']}", file=sys.stderr)
        print("Everything written so far is in the snapshot; `rem rollback` undoes it.", file=sys.stderr)
    for op in chosen:
        if op["status"] == "proposed":
            continue
        mark = "applied" if op["status"] == "applied" else f"skipped ({op.get('reason')})"
        print(f"  {op['id']} {mark}: {op.get('title', '')}")
    left = [op for op in proposal["ops"] if op["status"] == "proposed"]
    state = project.load_state()
    if not left:
        state.pop("pending_run", None)
    project.save_state(state)
    if result["snapshot"]:
        print(f"{len(result['applied'])} applied. Undo with: rem rollback")
    if left:
        print(f"{len(left)} still waiting: rem review")
    after = mem.scan(project.memory_dir, project.cwd)
    print(f"memory health now {after['score']}/100 (was {proposal['meta'].get('scan_score')}/100)")
    return 1 if result.get("error") else 0


def cmd_discard(args):
    project = _project(args)
    run, proposal = _load_pending(project)
    if not proposal:
        print("No dream is waiting.")
        return 0
    for op in proposal["ops"]:
        if op["status"] == "proposed":
            op["status"] = "discarded"
    (run / "proposal.json").write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    state = project.load_state()
    state.pop("pending_run", None)
    project.save_state(state)
    print("Discarded. Nothing was changed.")
    return 0


def cmd_rollback(args):
    project = _project(args)
    with Lock(project):
        result = ap.rollback(project, snap_id=args.snapshot, force=args.force)
    if result.get("blocked_by"):
        print(f"A newer apply ({result['blocked_by']}) is still in place. Roll back newest first: rem rollback")
        return 1
    if not result["snapshot"]:
        print("Nothing to roll back.")
        return 0
    if result["conflicts"]:
        print("These files were edited after rem applied its changes, so they were left alone:")
        for path in result["conflicts"]:
            print(f"  {path}")
        print("Use --force to restore them anyway.")
        return 1
    print(f"Restored {len(result['restored'])} file(s) to their state before snapshot {result['snapshot']}.")
    # The undone operations wait for review again.
    run = project.runs_dir() / str(result.get("run"))
    try:
        proposal = json.loads((run / "proposal.json").read_text(encoding="utf-8"))
        for op in proposal["ops"]:
            if op.get("id") in result.get("ops", []) and op.get("status") == "applied":
                op["status"] = "proposed"
        (run / "proposal.json").write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
        state = project.load_state()
        state["pending_run"] = run.name
        project.save_state(state)
        print("The proposal is waiting again: rem review")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 0


def cmd_pending(args):
    """Used by the SessionStart hook: one short line when a dream is waiting.
    A hook must never disturb a session, so any failure here is silent."""
    if os.environ.get("REM_CHILD"):
        return 0
    try:
        return _pending(args)
    except Exception:
        return 0


def _pending(args):
    cwd = args.project or os.getcwd()
    if args.hook:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            cwd = payload.get("cwd") or cwd
            if payload.get("source") not in (None, "startup", "clear"):
                return 0
        except ValueError:
            pass
    project = Project(cwd)
    message = ""
    run, proposal = _load_pending(project)
    if proposal:
        live = [op for op in proposal["ops"] if op["status"] == "proposed"]
        kinds = ", ".join(f"{n} {k}" for k, n in counts(proposal).items())
        if live:
            message = (f"rem: a memory consolidation from {proposal['meta']['created']} is waiting for the user's review "
                       f"({len(live)} proposed change(s): {kinds}). Mention this to the user in one short sentence "
                       f"and offer to show it with /rem:review. Do not apply anything without their say-so.")
    else:
        state = project.load_state()
        new = dg.list_sessions(project.transcripts_dir, since_ts=project.seen_mtime())
        if not project.seen_mtime():
            new = [p for p in new if time.time() - p.stat().st_mtime < 14 * 86400]
        nudged = state.get("nudged_at", 0)
        if len(new) >= args.nudge_after and project.memory_dir.is_dir() and time.time() - nudged > 3 * 86400:
            state["nudged_at"] = time.time()
            project.save_state(state)
            message = (f"rem: this project's memory has not been consolidated for {len(new)} sessions. "
                       f"If it fits the moment, tell the user in one short sentence that /rem:dream can review it.")
    if not message:
        return 0
    if args.hook:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}}))
    else:
        print(message)
    return 0


def cmd_digest(args):
    project = _project(args)
    paths = [Path(p) for p in args.sessions] if args.sessions else dg.list_sessions(
        project.transcripts_dir, since_ts=time.time() - args.days * 86400)
    out = Path(args.out or project.state_dir / "digests")
    metas = dg.build_digests(paths, out)
    for meta in metas:
        print(f"{meta['date']}  {meta['source_bytes'] / 1e6:6.1f} MB -> {meta['chars'] / 1e3:5.1f} KB  {meta['title'][:60]}")
    print(f"{len(metas)} digest(s) in {out}")
    return 0


def cmd_night(args):
    return night.run(max_projects=args.max_projects, min_sessions=args.min_sessions,
                     days=args.days, max_sessions=args.max_sessions, dry_run=args.dry_run)


def cmd_schedule(args):
    return night.schedule(args.action, hour=args.hour)


def build_parser():
    parser = argparse.ArgumentParser(prog="rem", description="Reviewable, evidence-backed memory consolidation for Claude Code.")
    parser.add_argument("--version", action="version", version=f"rem {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="project directory (default: current directory)")
    common.add_argument("--memory-dir", help="override the memory directory")
    common.add_argument("--transcripts-dir", help="override the transcripts directory")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("status", parents=[common], help="memory health check, no model calls")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("dream", parents=[common], help="review recent sessions and propose memory changes (read-only)")
    p.add_argument("--days", type=int, default=14, help="on the first dream, look back this many days")
    p.add_argument("--max-sessions", type=int, default=20)
    p.add_argument("--max-reviews", type=int, default=8, help="how many flagged memory files get a focused review")
    p.add_argument("--fresh", action="store_true", help="ignore the last-dream marker and review the last --days again")
    p.add_argument("--focus", help="what this consolidation should pay attention to")
    p.add_argument("--no-second-read", action="store_true", help="skip the independent reviewer")
    p.add_argument("--brief", action="store_true", help="show the proposal without diffs")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_dream)

    p = sub.add_parser("review", parents=[common], help="show the waiting proposal")
    p.add_argument("--brief", action="store_true", help="one evidence line per change, no diffs")
    p.add_argument("--markdown", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("apply", parents=[common], help="apply all waiting changes, or only the ids given")
    p.add_argument("ops", nargs="*")
    p.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("discard", parents=[common], help="drop the waiting proposal")
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("rollback", parents=[common], help="undo the last apply")
    p.add_argument("--snapshot")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("pending", parents=[common], help=argparse.SUPPRESS)
    p.add_argument("--hook", action="store_true")
    p.add_argument("--nudge-after", type=int, default=12)
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("digest", parents=[common], help="write session digests (for inspection)")
    p.add_argument("sessions", nargs="*")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--out")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("night", help="dream for every project that has new sessions")
    p.add_argument("--max-projects", type=int, default=5)
    p.add_argument("--min-sessions", type=int, default=3)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--max-sessions", type=int, default=20)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_night)

    p = sub.add_parser("schedule", help="run `rem night` every night")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("--hour", type=int, default=3, choices=range(24), metavar="0-23")
    p.set_defaults(func=cmd_schedule)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
