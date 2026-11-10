"""Where Claude Code keeps things, and where rem keeps its own state."""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def rem_home() -> Path:
    return Path(os.environ.get("REM_HOME") or claude_dir() / "rem")


def project_key(path) -> str:
    """Claude Code names a project folder after its path, non-alphanumerics -> '-'."""
    key = re.sub(r"[^A-Za-z0-9]", "-", str(path))
    if len(key) > 200:
        key = key[:200] + "-" + hashlib.sha1(str(path).encode()).hexdigest()[:8]
    return key


def git_root(path: Path):
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class Project:
    """One project: its working directory, its memory directory, its transcripts."""

    def __init__(self, cwd, memory_dir=None, transcripts_dir=None):
        self.cwd = Path(cwd).expanduser().resolve()
        self.key = project_key(self.cwd)
        base = claude_dir() / "projects"
        self.transcripts_dir = Path(transcripts_dir) if transcripts_dir else base / self.key
        if memory_dir:
            self.memory_dir = Path(memory_dir).expanduser().resolve()
        else:
            self.memory_dir = self._find_memory(base)
        # State follows the memory directory, so every folder of one repository shares it.
        state_key = self.memory_dir.parent.name if self.memory_dir.name == "memory" else project_key(self.memory_dir)
        self.state_dir = rem_home() / "projects" / (state_key or self.key)

    def _find_memory(self, base: Path) -> Path:
        # Auto memory is keyed by the git repo, so a session in a subfolder shares it.
        candidates = [self.cwd]
        root = git_root(self.cwd)
        if root and root != self.cwd:
            candidates.insert(0, root)
        for cand in candidates:
            mem = base / project_key(cand) / "memory"
            if mem.is_dir():
                return mem
        return base / project_key(candidates[0]) / "memory"

    # --- run bookkeeping -------------------------------------------------
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    def new_run(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.runs_dir().mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(rem_home(), 0o700)  # digests hold transcript text
        except OSError:
            pass
        for counter in range(1000):
            run = self.runs_dir() / (stamp if counter == 0 else f"{stamp}-{counter}")
            try:
                run.mkdir()
                return run
            except FileExistsError:
                continue
        raise RuntimeError("could not create a run folder")

    def prune_runs(self, keep=10, keep_digests=2):
        """Old runs go; digests (transcript text) are kept only for the newest runs."""
        import shutil
        runs = sorted((p for p in self.runs_dir().iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True) \
            if self.runs_dir().is_dir() else []
        pending = self.load_state().get("pending_run")
        for i, run in enumerate(runs):
            if run.name == pending:
                continue
            if i >= keep:
                shutil.rmtree(run, ignore_errors=True)
            elif i >= keep_digests:
                shutil.rmtree(run / "digests", ignore_errors=True)

    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    def load_state(self) -> dict:
        try:
            data = json.loads(self.state_file().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        write_atomic(self.state_file(), json.dumps(state, indent=2).encode("utf-8"))

    def pending_run(self):
        """Latest run with a proposal that still has undecided operations."""
        state = self.load_state()
        run_id = state.get("pending_run")
        if not run_id:
            return None
        run = self.runs_dir() / run_id
        return run if (run / "proposal.json").exists() else None


class Lock:
    """One dream or apply per project at a time. Stale locks expire."""

    def __init__(self, project: Project, ttl=3 * 3600):
        self.path = project.state_dir / "lock"
        self.ttl = ttl

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if not self._stale():
                    fd = None
                    break
                try:
                    self.path.unlink()
                except OSError:
                    pass
        else:
            fd = None
        if fd is None:
            raise SystemExit(
                f"rem: another run holds the lock for this project ({self.path}). "
                "Wait for it, or delete the file if it is left over from a crash."
            )
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def _stale(self) -> bool:
        """A lock is stale when its process is gone, or it is older than the ttl."""
        try:
            if time.time() - self.path.stat().st_mtime > self.ttl:
                return True
            pid = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def sha_bytes(path) -> str:
    """Hash of a file's raw bytes; '' when it does not exist."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def write_atomic(path, data: bytes) -> None:
    """Replace a file in one step, so a crash never leaves it half written."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.rem-tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
