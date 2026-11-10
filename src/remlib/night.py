"""The night shift: find projects with new sessions and dream for each. Never applies."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import digest as dg
from . import pipeline
from .paths import Lock, Project, claude_dir, rem_home

LABEL = "dev.rem.night"


def _session_cwd(path: Path) -> str:
    """The working directory a transcript was recorded in."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle):
                if i > 200:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                cwd = entry.get("cwd") if isinstance(entry, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


def candidates(min_sessions=3, days=14):
    """Projects with enough new sessions since their last dream, busiest first."""
    root = claude_dir() / "projects"
    found = []
    if not root.is_dir():
        return found
    for folder in root.iterdir():
        try:
            item = _candidate(folder, min_sessions, days)
        except Exception:  # one odd project folder must not stop the night
            continue
        if item:
            found.append(item)
    found.sort(key=lambda item: -item[0])
    return found


def _candidate(folder: Path, min_sessions: int, days: int):
    if True:
        if not folder.is_dir():
            return None
        newest = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not newest:
            return None
        cwd = ""
        for path in newest[:3]:
            cwd = _session_cwd(path)
            if cwd:
                break
        if not cwd or not Path(cwd).is_dir():
            return None
        project = Project(cwd, transcripts_dir=folder)
        since = project.seen_mtime() or (time.time() - days * 86400)
        new = dg.list_sessions(folder, since_ts=since)
        if len(new) >= min_sessions and not project.pending_run():
            return (len(new), project)
        return None


def run(max_projects=5, min_sessions=3, days=14, max_sessions=20, dry_run=False):
    log_path = rem_home() / "night.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    todo = candidates(min_sessions=min_sessions, days=days)[:max_projects]
    if not todo:
        log("night: nothing to dream about")
        return 0
    for count, project in todo:
        log(f"night: {project.cwd} ({count} new sessions)")
        if dry_run:
            continue
        try:
            with Lock(project):
                _, proposal = pipeline.dream(project, days=days, max_sessions=max_sessions,
                                             log=lambda m: log(f"  {m}"))
            live = [op for op in proposal["ops"] if op["status"] == "proposed"]
            log(f"  {len(live)} change(s) proposed, ${proposal['meta']['usage']['cost_usd']}")
        except SystemExit as err:
            log(f"  skipped: {err}")
        except Exception as err:  # one bad project must not stop the night
            log(f"  failed: {type(err).__name__}: {err}")
    return 0


# --- scheduling ------------------------------------------------------------

def _rem_bin() -> str:
    return str(Path(__file__).resolve().parents[2] / "bin" / "rem")


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def schedule(action: str, hour: int = 3) -> int:
    claude = shutil.which("claude") or ""
    if action == "install":
        if not 0 <= hour <= 23:
            print("rem: --hour must be between 0 and 23", file=sys.stderr)
            return 1
        if not claude:
            print("rem: the `claude` CLI is not on PATH, so a nightly dream could not run. Nothing installed.", file=sys.stderr)
            return 1
    path_env = os.pathsep.join(dict.fromkeys(
        ([str(Path(claude).parent)] if claude else []) + ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]))
    env = {"PATH": path_env}
    for key in ("CLAUDE_CONFIG_DIR", "REM_HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if sys.platform == "darwin":
        import plistlib
        plist = _plist_path()
        if action == "status":
            print(f"installed: {plist}" if plist.exists() else "not installed")
            return 0
        if action == "uninstall":
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            if plist.exists():
                plist.unlink()
            print("Nightly dream removed.")
            return 0
        log = rem_home() / "night.out"
        log.parent.mkdir(parents=True, exist_ok=True)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": [sys.executable, _rem_bin(), "night"],
            "EnvironmentVariables": env,
            "StartCalendarInterval": {"Hour": hour, "Minute": 0},
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }))
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        out = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True)
        if out.returncode != 0:
            print(f"rem: launchctl refused the job: {out.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"Nightly dream installed: every day at {hour:02d}:00. It only writes proposals; nothing is applied until you review.")
        return 0

    import shlex
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    command = f"{shlex.quote(sys.executable)} {shlex.quote(_rem_bin())} night"
    marker = "# rem night"
    line = f'0 {hour} * * * {exports} {command} >> {shlex.quote(str(rem_home() / "night.out"))} 2>&1 {marker}'
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    lines = [ln for ln in (current.stdout if current.returncode == 0 else "").splitlines() if marker not in ln]
    if action == "status":
        print("installed" if current.returncode == 0 and marker in current.stdout else "not installed")
        return 0
    if action == "install":
        lines.append(line)
    out = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
    if out.returncode != 0:
        print(f"rem: could not update crontab: {out.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"Nightly dream installed at {hour:02d}:00." if action == "install" else "Nightly dream removed.")
    return 0
