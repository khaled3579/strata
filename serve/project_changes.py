"""Session-local change records for the selected project's filesystem tools."""

from collections import OrderedDict
from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading

from serve.mcp import McpCancelled
from serve.project_checks import FILE_CHANGES


MAX_FILE = 4 * 1024 * 1024
MAX_ENTRIES = 512
MAX_RECORD = 16 * 1024 * 1024


@dataclass(frozen=True)
class Entry:
    kind: str
    data: bytes = b""
    mode: int = 0
    modified: int = 0
    inode: int = 0


def capture(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError(f"Linked entry: {path.name}")
    if stat.S_ISDIR(info.st_mode):
        return Entry("directory", inode=info.st_ino)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE or info.st_nlink > 1:
        raise ValueError(f"File cannot be recorded for undo: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
    if len(data) != info.st_size or (after.st_ino, after.st_mtime_ns) != (info.st_ino, info.st_mtime_ns):
        raise ValueError(f"File changed during capture: {path.name}")
    return Entry("file", data, stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_ino)


def checked_path(root, raw):
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError("Invalid project path.")
    path = Path(raw).expanduser()
    path = Path(os.path.abspath(path if path.is_absolute() else root / path))
    if path == root or not path.is_relative_to(root):
        raise ValueError("Path is outside the selected project.")
    current = root
    if root.resolve(strict=True) != root:
        raise ValueError("The project folder has moved.")
    for part in path.relative_to(root).parts:
        if part.endswith((".", " ")) or ":" in part or Path(part).is_reserved():
            raise ValueError("Unsupported project path.")
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Linked entry: {current.name}")
    return path


def capture_tree(path, entries, deep, budget):
    if len(entries) >= MAX_ENTRIES:
        raise ValueError("Too many entries to record.")
    value = entries[path] = capture(path)
    if value:
        budget[0] -= len(value.data)
        if budget[0] < 0:
            raise ValueError("The operation exceeds the change record size limit.")
    if deep and value and value.kind == "directory":
        for child in path.iterdir():
            capture_tree(child, entries, True, budget)


def weight(entries):
    return sum(len(value.data) for value in entries.values() if value)


def capture_operation(root, tool, args):
    keys = ("source", "destination") if tool == "move_file" else ("destination",) if tool == "copy_file" else ("path",)
    targets = [checked_path(root, args.get(key)) for key in keys]
    entries = {}
    budget = [MAX_RECORD]
    for target in targets:
        capture_tree(target, entries, tool in ("move_file", "delete_file"), budget)
        parent = target.parent
        while parent != root and not parent.exists():
            entries[parent] = None
            parent = parent.parent
    if tool == "move_file" and entries[targets[0]] and entries[targets[0]].kind == "directory":
        for path in tuple(entries):
            if path != targets[0] and path.is_relative_to(targets[0]):
                destination = checked_path(root, str(targets[1] / path.relative_to(targets[0])))
                entries[destination] = capture(destination)
    return targets, entries


def apply_states(current, desired):
    """Apply only recorded entries. Directory removal requires an empty directory."""
    paths = sorted(desired, key=lambda path: len(path.parts))
    for path in reversed(paths):
        old, new = current[path], desired[path]
        if not old or (new and old.kind == new.kind):
            continue
        if old.kind == "directory":
            path.rmdir()
        else:
            path.unlink()
    for path in paths:
        old, new = current[path], desired[path]
        if not new or old == new:
            continue
        if new.kind == "directory":
            if not old or old.kind != "directory":
                path.mkdir()
            continue
        if old and old.kind == "file":
            with path.open("r+b") as stream:
                stream.write(new.data)
                stream.truncate()
            os.chmod(path, new.mode)
            os.utime(path, ns=(new.modified, new.modified))
            continue
        fd, name = tempfile.mkstemp(prefix=".strata-undo-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(new.data)
            os.chmod(temporary, new.mode)
            os.utime(temporary, ns=(new.modified, new.modified))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def preview(old, new, name):
    if any(entry and entry.kind == "directory" for entry in (old, new)):
        return None
    try:
        left, right = [(entry.data if entry else b"").decode("utf-8-sig") for entry in (old, new)]
    except UnicodeError:
        return None
    if "\0" in left or "\0" in right:
        return None
    if len(left) + len(right) > 160_000:
        return None
    def lines(text):
        return [line.rstrip("\r\n") + (" [no newline]" if not line.endswith(("\n", "\r")) else "")
                for line in text.splitlines(keepends=True)]
    return "\n".join(difflib.unified_diff(lines(left), lines(right),
                                         fromfile=name if old else "/dev/null",
                                         tofile=name if new else "/dev/null", lineterm=""))[:32_000]


class ChangeHistory:
    def __init__(self):
        self.lock = threading.RLock()
        self.records = OrderedDict()
        self.latest = {}

    def begin(self, root):
        return ChangeRecord(self, root)

    def remember(self, record):
        if record.id in self.records and self.latest.get(record.root) != record.id:
            record.problem = "Project changes overlapped another reply."
        self.records[record.id] = record
        self.latest[record.root] = record.id
        self.records.move_to_end(record.id)
        while len(self.records) > 16 or sum(r.size for r in self.records.values()) > 64 * 1024 * 1024:
            key, old = self.records.popitem(last=False)
            if self.latest.get(old.root) == key:
                del self.latest[old.root]

    def access(self, root, key, undo=False):
        with self.lock:
            record = self.records.get(key) if isinstance(key, str) else None
            if record is None or record.root != root or root.stat().st_ino != record.root_inode:
                raise ValueError("This change record is unavailable. Records are kept for the current server session.")
            latest = self.latest.get(root) == key
            if undo:
                if not latest or record.undone or record.problem or not record.finished:
                    raise ValueError("Undo is available only for the latest fully recorded change.")
                record.undo()
            return record.view(latest)


class ChangeRecord:
    def __init__(self, history, root):
        self.history, self.root = history, root
        self.root_inode = root.stat().st_ino
        self.id = secrets.token_urlsafe(24)
        self.before, self.after = {}, {}
        self.problem = ""
        self.undone = False
        self.finished = False

    @property
    def size(self):
        return weight(self.before) + weight(self.after)

    def run(self, tool, args, call):
        if tool not in FILE_CHANGES:
            return call(), None
        with self.history.lock:
            try:
                targets, before = capture_operation(self.root, tool, args if isinstance(args, dict) else {})
            except (ValueError, OSError) as error:
                targets, before = [], {}
                capture_error = str(error)
            try:
                result = call()
            except McpCancelled:
                self.problem = "A file operation was interrupted. Its final changes could not be recorded."
                self.history.remember(self)
                raise
            if not targets:
                if not result["ok"]:
                    return result, None
                self.problem = capture_error
            else:
                try:
                    after = {}
                    budget = [MAX_RECORD]
                    for path in before:
                        capture_tree(checked_path(self.root, str(path)), after, False, budget)
                    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
                    if not changed:
                        return result, None
                    if len(changed | self.before.keys()) > MAX_ENTRIES or self.size + weight(before) + weight(after) > MAX_RECORD:
                        raise ValueError("The reply exceeds the change record limit.")
                    for path in changed:
                        if path in self.after and self.after[path] != before.get(path):
                            self.problem = "A file was edited between tool calls. Undo is unavailable for this reply."
                        self.before.setdefault(path, before.get(path))
                        self.after[path] = after.get(path)
                except (ValueError, OSError) as error:
                    self.problem = str(error)
            self.history.remember(self)
            return result, self.id

    def view(self, latest):
        changes = []
        remaining = 150_000
        for path, old in sorted(self.before.items()):
            new = self.after[path]
            if old == new:
                continue
            name = path.relative_to(self.root).as_posix()
            diff = preview(old, new, name) if remaining > 0 else None
            if diff:
                remaining -= len(diff)
            changes.append({"path": name, "status": "added" if old is None else "removed" if new is None else "modified",
                            "directory": any(e and e.kind == "directory" for e in (old, new)), "diff": diff})
        return {"files": changes, "problem": self.problem, "undone": self.undone,
                "can_undo": latest and self.finished and not self.undone and not self.problem and bool(changes)}

    def undo(self):
        before = {p: v for p, v in self.before.items() if v != self.after[p]}
        after = {p: self.after[p] for p in before}
        for path, value in after.items():
            if capture(checked_path(self.root, str(path))) != value:
                raise ValueError(f"File changed after the reply: {path.relative_to(self.root)}. Nothing was undone.")
            if value and value.kind == "directory" and (not before[path] or before[path].kind != "directory"):
                if any(child not in after for child in path.iterdir()):
                    raise ValueError(f"Folder contains later additions: {path.relative_to(self.root)}. Nothing was undone.")
        try:
            apply_states(after, before)
        except OSError as error:
            try:
                apply_states({p: capture(checked_path(self.root, str(p))) for p in after}, after)
            except (OSError, ValueError) as rollback:
                self.problem = "Undo was interrupted and could not be rolled back completely."
                raise ValueError(self.problem) from rollback
            raise ValueError("Undo could not be applied. The changes were restored.") from error
        self.undone = True
