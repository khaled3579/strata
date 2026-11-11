"""Allow the local Strata page to reveal or open an existing project file."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ORIGINS = {"http://127.0.0.1:8080", "http://localhost:8080"}
HOST = "127.0.0.1:8081"
ROUTE = "/open-project-file"
CONFIG = Path(sys.argv[1]) if __name__ == "__main__" else None


def project_file(root, payload):
    parts = payload.get("parts")
    if payload.get("root") != root.name:
        raise ValueError(f"Windows actions are available for the configured project: {root.name}.")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 64:
        raise ValueError("Invalid project file path.")
    target = root
    for index, part in enumerate(parts):
        if (not isinstance(part, str) or not part or part.endswith((".", " "))
                or any(char in part for char in ("\\", "/", ":", "\0"))):
            raise ValueError("Invalid project file path.")
        target = target / part
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Windows actions are unavailable for linked files or folders.")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError("Invalid project folder path.")
    target = target.resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("The selected item is outside the configured project.")
    return target


def verify_file(target, payload):
    size, modified, fingerprint = (payload.get(key) for key in ("size", "modified", "fingerprint"))
    if type(size) is not int or type(modified) is not int or not isinstance(fingerprint, str):
        raise ValueError("Invalid file information.")
    with target.open("rb") as stream:
        info = os.fstat(stream.fileno())
        digest = hashlib.sha256(stream.read(65536)).hexdigest()
    if (info.st_size != size or abs(info.st_mtime_ns // 1_000_000 - modified) > 1
            or len(fingerprint) != 64 or not fingerprint.isascii()
            or not hmac.compare_digest(digest, fingerprint)):
        raise ValueError("The file does not match the configured project or has changed. Refresh and try again.")


def launch_file(action, target):
    if action == "explorer":
        program = Path(os.environ["SystemRoot"]) / "explorer.exe"
        args = ["/select,", str(target)]
    elif action == "vscode":
        candidates = (
            Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Microsoft VS Code" / "Code.exe",
            Path(os.environ["ProgramFiles"]) / "Microsoft VS Code" / "Code.exe",
        )
        program = next((path for path in candidates if path.is_file()), None)
        if program is None:
            raise ValueError("VS Code is not installed in a standard Windows location.")
        args = ["--reuse-window", str(target)]
    else:
        raise ValueError("Unknown file action.")
    subprocess.Popen([str(program), *args], cwd=str(target.parent), close_fds=True)


class Opener(BaseHTTPRequestHandler):
    def accepted_origin(self):
        return self.headers.get("Host") == HOST and self.headers.get("Origin") in ORIGINS

    def reply(self, status, data=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else b""
        self.send_response(status)
        if self.accepted_origin():
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self.reply(204 if self.path == ROUTE and self.accepted_origin() else 403)

    def do_POST(self):
        if self.path != ROUTE or not self.accepted_origin():
            self.reply(403, {"error": "Only the local Strata page can use this helper."})
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected a JSON file action.")
            length = int(self.headers.get("Content-Length", 0))
            if not 1 <= length <= 8192:
                raise ValueError("Invalid file action size.")
            self.connection.settimeout(3)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Invalid file action.")
            config = json.loads(CONFIG.read_text(encoding="utf-8"))
            root = Path(config["mcp_servers"]["project_files"]["cwd"]).resolve(strict=True)
            target = project_file(root, payload)
            verify_file(target, payload)
            launch_file(payload.get("action"), target)
            self.reply(200, {"ok": True})
        except ValueError as error:
            self.reply(400, {"error": str(error)})
        except FileNotFoundError:
            self.reply(404, {"error": "The file is no longer available in the configured project."})
        except (OSError, KeyError, TypeError):
            self.reply(500, {"error": "Windows could not open the selected file."})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 8081), Opener) as server:
        server.serve_forever()
