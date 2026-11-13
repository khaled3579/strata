"""Windows folder selection and request-scoped tools for the local web chat."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import threading

from serve.mcp import McpHub, McpServer
from serve.project_checks import ProjectChecks
from serve.project_changes import ChangeHistory


PICK_FOLDER = """
import json
import tkinter as tk
from tkinter import filedialog

window = tk.Tk()
window.withdraw()
window.attributes("-topmost", True)
try:
    folder = filedialog.askdirectory(parent=window, title="Choose a Strata project folder", mustexist=True)
    print(json.dumps(folder))
finally:
    window.destroy()
"""


def is_link(info):
    return bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class ProjectFolders:
    def __init__(self, hub):
        self.hub = hub
        self.files = hub.servers["project_files"]
        self.key = secrets.token_bytes(32)
        self.picker_lock = threading.Lock()
        self.changes = ChangeHistory()

    @classmethod
    def from_hub(cls, hub):
        files = hub and hub.servers.get("project_files")
        if (os.name == "nt" and files and files.kind == "stdio"
                and files.cfg.get("project_picker") is True
                and files.cfg.get("cwd") and len(files.cfg.get("args") or []) == 2
                and files.cfg["args"][-1] == files.cfg["cwd"]):
            return cls(hub)
        return None

    def pick(self):
        if not self.picker_lock.acquire(blocking=False):
            raise ValueError("A folder picker is already open.")
        try:
            result = subprocess.run(
                [sys.executable, "-c", PICK_FOLDER], capture_output=True, timeout=300,
                creationflags=subprocess.CREATE_NO_WINDOW, check=True,
            )
        finally:
            self.picker_lock.release()
        folder = json.loads(result.stdout)
        if not folder:
            return None
        root = Path(folder).resolve(strict=True)
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("The selected folder is unavailable.")
        data = base64.urlsafe_b64encode(json.dumps([str(root), info.st_dev, info.st_ino]).encode())
        signature = hmac.digest(self.key, data, hashlib.sha256).hex()
        return {"name": root.name or str(root), "path": str(root), "token": data.decode() + "." + signature}

    def resolve(self, token):
        if not isinstance(token, str) or len(token) > 32768 or not token.isascii():
            raise ValueError("Choose the project folder again to reconnect its tools.")
        data, _, signature = token.partition(".")
        if not hmac.compare_digest(hmac.digest(self.key, data.encode(), hashlib.sha256).hex(), signature):
            raise ValueError("Choose the project folder again to reconnect its tools.")
        path, device, inode = json.loads(base64.urlsafe_b64decode(data))
        root = Path(path)
        try:
            info = root.stat()
            if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode)
                    or root.resolve(strict=True) != root):
                raise OSError
        except OSError:
            raise ValueError("The project folder moved or is unavailable. Choose it again.") from None
        return root

    @staticmethod
    def entry(root, parts):
        if not isinstance(parts, list):
            raise ValueError("Expected a project-relative path.")
        target = root
        for part in parts:
            if (not isinstance(part, str) or not part or part.endswith((".", " "))
                    or any(c in part for c in '\\/:\0')):
                raise ValueError("Invalid project path.")
            target /= part
            if is_link(target.lstat()):
                raise ValueError("This project entry is a link or reparse point.")
        target = target.resolve(strict=True)
        if not target.is_relative_to(root):
            raise ValueError("The entry is outside the selected project.")
        return target

    def tools(self, root):
        """Keep this request's tools fixed even if another tab chooses a different folder."""
        hub = McpHub({}, self.hub.settings)
        hub.media_dir = self.hub.media_dir
        hub.servers = {name: server for name, server in self.hub.servers.items()
                       if name not in ("project_files", "project_checks")}
        if root is None:
            return hub, None
        cfg = {**self.files.cfg, "cwd": str(root), "args": [*self.files.cfg["args"][:-1], str(root)]}
        files = McpServer("project_files", cfg, hub.settings)
        if not files.start():
            raise ValueError(f"Could not start the selected project's file tools: {files.error}")
        hub.servers[files.name] = files
        hub.project_change_record = self.changes.begin(root)
        files.project_change_record = hub.project_change_record
        checks = self.files.cfg.get("project_checks")
        if isinstance(checks, dict):
            hub.servers["project_checks"] = ProjectChecks(root, checks)
        return hub, files


def open_file(action, target):
    if action == "explorer":
        program = Path(os.environ["SystemRoot"]) / "explorer.exe"
        args = ["/select,", str(target)]
    elif action == "vscode":
        locations = {"LOCALAPPDATA": "Programs/Microsoft VS Code/Code.exe",
                     "ProgramFiles": "Microsoft VS Code/Code.exe"}
        candidates = [Path(os.environ[key]) / path for key, path in locations.items() if key in os.environ]
        program = next((path for path in candidates if path.is_file()), None)
        if program is None:
            raise ValueError("VS Code is not installed in a standard Windows location.")
        args = ["--reuse-window", str(target)]
    else:
        raise ValueError("Unknown file action.")
    subprocess.Popen([str(program), *args], cwd=target.parent, close_fds=True)
