"""Apply approved operations with a snapshot first, and undo them on request.

The snapshot manifest is written before the first byte changes and after every
file, so an apply that dies halfway can still be rolled back. Files are replaced
atomically. Hashes are over raw bytes, so nothing is ever "the same" by accident.
"""
import json
import os
import shutil
import time
from pathlib import Path

from . import memory as mem
from .paths import sha_bytes, write_atomic
from .proposal import apply_index_changes, file_key, resolve

CONFIDENCE = {"low": 0, "medium": 1, "high": 2}


def select(proposal: dict, only=None, min_confidence="low"):
    floor = CONFIDENCE.get(min_confidence, 0)
    chosen = []
    for op in proposal.get("ops", []):
        if op.get("status") != "proposed":
            continue
        if only is not None and op["id"] not in only:
            continue
        if only is None and CONFIDENCE.get(op.get("confidence", "low"), 0) < floor:
            continue
        chosen.append(op)
    return chosen


def _new_snapshot_dir(project) -> Path:
    root = project.state_dir / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for counter in range(1000):
        path = root / (stamp if counter == 0 else f"{stamp}-{counter}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError("could not create a snapshot folder")


def _save_json(path: Path, data) -> None:
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))


def _append(path: Path, addition: str) -> bytes:
    """New bytes for a file with a rule appended, keeping its bytes and line endings."""
    existing = path.read_bytes() if path.exists() else b""
    newline = b"\r\n" if b"\r\n" in existing else b"\n"
    body = addition.strip().replace("\n", newline.decode()).encode("utf-8")
    if not existing:
        joiner = b""
    elif existing.endswith(newline * 2):
        joiner = b""
    elif existing.endswith(newline):
        joiner = newline
    else:
        joiner = newline * 2
    return existing + joiner + body + newline


def apply_ops(project, run_dir: Path, proposal: dict, ops) -> dict:
    """Write the chosen operations. Returns a summary; updates proposal statuses in place."""
    memory_dir, project_dir = project.memory_dir, project.cwd
    snap_dir = _new_snapshot_dir(project)
    manifest = {"id": snap_dir.name, "run": run_dir.name, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "complete": False, "files": [], "dirs": [], "ops": []}
    applied, skipped = [], []
    index_path = memory_dir / mem.INDEX
    index_changes = []
    saved = {}

    def backup(path: Path):
        key = file_key(path)
        if key in saved:
            return saved[key]
        entry = {"path": str(path), "existed": path.exists(), "backup": "", "after_sha": None}
        if path.exists():
            name = f"{len(manifest['files']):03d}-{path.name}"
            shutil.copy2(path, snap_dir / name)
            entry["backup"] = name
        else:
            # Remember which folders we are about to create, innermost first, to remove them on undo.
            missing, folder = [], path.parent
            while not folder.exists():
                missing.append(str(folder))
                folder = folder.parent
            manifest["dirs"].extend(d for d in missing if d not in manifest["dirs"])
        manifest["files"].append(entry)
        saved[key] = entry
        _save_json(snap_dir / "manifest.json", manifest)
        return entry

    def written(entry, path: Path):
        entry["after_sha"] = sha_bytes(path)
        _save_json(snap_dir / "manifest.json", manifest)

    error = None
    try:
        for op in ops:
            changes = op.get("changes", [])
            # Refuse an operation whose files changed since the dream looked at them.
            stale = next((resolve(c, memory_dir, project_dir).name for c in changes
                          if sha_bytes(resolve(c, memory_dir, project_dir)) != c.get("base_sha", "")), None)
            if stale:
                op["status"] = "skipped"
                op["reason"] = f"{stale} changed after the dream; run a new dream"
                skipped.append(op["id"])
                continue
            for change in changes:
                path = resolve(change, memory_dir, project_dir)
                action = change["action"]
                if action in ("index", "unindex"):
                    index_changes.append(change)
                    continue
                if path.is_symlink():
                    raise RuntimeError(f"{path} is a symbolic link; refusing to write through it")
                entry = backup(path)
                if action == "delete":
                    if path.exists():
                        path.unlink()
                    index_changes.append(change)
                elif action == "write":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_atomic(path, (change["content"].rstrip("\n") + "\n").encode("utf-8"))
                    if change["target"] == "memory":
                        index_changes.append(change)
                elif action == "append":
                    write_atomic(path, _append(path, change["content"]))
                written(entry, path)
            op["status"] = "applied"
            applied.append(op["id"])
            manifest["ops"].append(op["id"])
    except Exception as err:  # keep what we have reversible, then report
        error = err
    finally:
        try:
            if index_changes:
                entry = backup(index_path)
                memory_dir.mkdir(parents=True, exist_ok=True)
                current = index_path.read_bytes().decode("utf-8", errors="replace") if index_path.exists() else ""
                write_atomic(index_path, apply_index_changes(current, index_changes).encode("utf-8"))
                written(entry, index_path)
        finally:
            if manifest["files"]:
                manifest["complete"] = error is None
                _save_json(snap_dir / "manifest.json", manifest)
            else:
                shutil.rmtree(snap_dir, ignore_errors=True)
            _save_json(run_dir / "proposal.json", proposal)
    result = {"applied": applied, "skipped": skipped,
              "snapshot": manifest["id"] if manifest["files"] else None, "error": None}
    if error is not None:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def list_snapshots(project):
    root = project.state_dir / "snapshots"
    out = []
    if root.is_dir():
        for path in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            manifest = path / "manifest.json"
            if manifest.exists():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                out.append((path, data))
    return out


def rollback(project, snap_id=None, force=False) -> dict:
    """Put every file from the newest snapshot back the way it was before that apply.
    Snapshots are undone newest first; all-or-nothing unless forced."""
    active = [(p, d) for p, d in list_snapshots(project) if not d.get("rolled_back")]
    empty = {"restored": [], "conflicts": [], "snapshot": None, "blocked_by": None}
    if not active:
        return empty
    if snap_id and active[0][1]["id"] != snap_id:
        if snap_id not in [d["id"] for _, d in active]:
            return empty
        return {**empty, "blocked_by": active[0][1]["id"]}
    snap_dir, manifest = active[0]
    conflicts = []
    for entry in manifest["files"]:
        path = Path(entry["path"])
        if entry.get("after_sha") is not None and sha_bytes(path) != entry["after_sha"]:
            conflicts.append(str(path))
    if conflicts and not force:
        return {"restored": [], "conflicts": conflicts, "snapshot": manifest["id"], "blocked_by": None}
    restored = []
    for entry in manifest["files"]:
        path = Path(entry["path"])
        if entry.get("after_sha") is None and not force:
            continue  # the apply died before touching this file
        if entry["existed"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, (snap_dir / entry["backup"]).read_bytes())
        elif path.exists():
            path.unlink()
        restored.append(str(path))
    for folder in manifest.get("dirs", []):
        try:
            os.rmdir(folder)  # only succeeds when empty
        except OSError:
            pass
    manifest["rolled_back"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_json(snap_dir / "manifest.json", manifest)
    return {"restored": restored, "conflicts": conflicts if force else [], "snapshot": manifest["id"],
            "blocked_by": None, "ops": manifest.get("ops", []), "run": manifest.get("run")}
