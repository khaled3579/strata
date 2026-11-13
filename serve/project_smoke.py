"""Run a browser and the built project together in an offline Windows Sandbox."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from serve.mcp import McpCancelled
from serve.project_verification import missing_dependency


GUEST = r"C:\StrataSmoke"
PORT = 4173
AGENT = Path(__file__).with_name("project_smoke_guest.cjs")


def plan(root, settings, manifest):
    override = settings.get("smoke")
    if override is False:
        return None
    if override is not None:
        if not isinstance(override, dict):
            raise ValueError("smoke must be an object with argv and an optional page, or false.")
        from serve.project_checks import command_rows
        _, cwd, argv = command_rows(root, [override])[0]
        page = override.get("page", "/")
        if not isinstance(page, str) or not page.startswith("/") or len(page) > 512 or "\\" in page:
            raise ValueError("The smoke test page must be a relative URL beginning with /.")
        url = urlsplit(page)
        if url.netloc or url.scheme or page.startswith("//") or any(ord(c) < 32 for c in page):
            raise ValueError("The smoke test page must belong to the local application.")
        if argv[0] not in ("node", "npm", "python"):
            raise ValueError("The smoke test supports node, npm, and python launch commands.")
        return {"argv": argv, "cwd": cwd, "page": page}
    # Prefer a built static site so the checked page actually uses the build.
    for directory in ("dist", "build", "out"):
        if (root / directory / "index.html").is_file():
            return {"static": directory, "page": "/"}
    scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
    if not isinstance(scripts, dict):
        scripts = {}
    if isinstance(scripts, dict) and "start" in scripts:
        return {"argv": ["npm", "start"], "cwd": ".", "page": "/"}
    if (root / "manage.py").is_file():
        return {"argv": ["python", "manage.py", "runserver", f"127.0.0.1:{PORT}", "--noreload"], "cwd": ".", "page": "/"}
    if (root / "index.html").is_file() and "build" not in scripts:
        return {"static": ".", "page": "/"}
    return None


def configuration(inputs, result):
    root = ET.Element("Configuration")
    for name in ("Networking", "ClipboardRedirection", "PrinterRedirection", "AudioInput", "VideoInput", "VGpu"):
        ET.SubElement(root, name).text = "Disable"
    ET.SubElement(root, "ProtectedClient").text = "Enable"
    mappings = ET.SubElement(root, "MappedFolders")
    for source, target, readonly in ((inputs, "Input", "true"), (result, "Result", "false")):
        folder = ET.SubElement(mappings, "MappedFolder")
        for key, value in (("HostFolder", str(source)), ("SandboxFolder", GUEST + "\\" + target), ("ReadOnly", readonly)):
            ET.SubElement(folder, key).text = value
    command = ET.SubElement(ET.SubElement(root, "LogonCommand"), "Command")
    command.text = GUEST + r"\Input\Node\node.exe " + GUEST + r"\Input\smoke.cjs"
    return ET.tostring(root, encoding="unicode")


def run_smoke(work, scratch, config, launch, cancel, deadline):
    from serve.project_checks import snapshot

    row = {"check": "Browser smoke test", "kind": "smoke", "status": "unavailable"}
    if launch is None:
        return {**row, "output": "No runnable web application detected. Set smoke.argv and smoke.page in .strata-checks.json."}
    row["command"] = subprocess.list2cmdline(launch["argv"]) if "argv" in launch else "Serve " + launch["static"]
    windows = Path(os.environ["SystemRoot"])
    cli = shutil.which("wsb.exe") or str(windows / "System32/wsb.exe")
    if not Path(cli).is_file():
        return {**row, "missing_dependency": "Windows Sandbox (wsb.exe)",
                "output": "Windows Sandbox CLI is unavailable. The app and browser must run in the same offline sandbox."}
    node = Path(config.get("node") or "missing")
    playwright = Path(config.get("playwright") or "missing")
    if not node.is_file() or not (playwright / "package.json").is_file():
        return {**row, "missing_dependency": "Node.js" if not node.is_file() else "playwright-core",
                "output": "Configure node and playwright in the Strata project check toolchain."}

    with tempfile.TemporaryDirectory(prefix="strata-smoke-", dir=scratch.parent, ignore_cleanup_errors=True) as directory:
        staging = Path(directory).resolve()
        inputs, result = staging / "inputs", staging / "result"
        inputs.mkdir()
        result.mkdir()
        (inputs / "Node").mkdir()
        shutil.copyfile(node, inputs / "Node/node.exe")
        snapshot(work, inputs / "Project", cancel)
        shutil.copytree(playwright, inputs / "Playwright")
        shutil.copyfile(AGENT, inputs / "smoke.cjs")
        browser = config.get("smoke_browser")
        if browser:
            executable = Path(browser).resolve(strict=True)
            snapshot(executable.parent, inputs / "Browser", cancel)
            launch = {**launch, "browser": executable.name}
        if launch.get("argv", [None])[0] == "npm":
            npm = node.parent / "node_modules/npm"
            if not (npm / "bin/npm-cli.js").is_file():
                return {**row, "missing_dependency": "npm"}
            shutil.copytree(npm, inputs / "npm")
        if launch.get("argv", [None])[0] == "python":
            from serve.project_checks import stage_python
            stage_python(inputs / "Python")
            packages = config.get("smoke_python_packages")
            if packages:
                snapshot(Path(packages).resolve(strict=True), inputs / "Python/Lib/site-packages", cancel)
        (inputs / "launch.json").write_text(json.dumps(launch), encoding="utf-8")
        if cancel.is_set():
            raise McpCancelled("Project checks stopped.")
        timeout = min(100, deadline - time.monotonic())
        if timeout <= 0:
            return {**row, "status": "timed_out"}
        identity = str(uuid.uuid4())
        started = time.monotonic()
        options = {"creationflags": subprocess.CREATE_NO_WINDOW, "stdin": subprocess.DEVNULL,
                   "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        with (result / "report.json").open("x+b", buffering=0) as report:
            try:
                child = subprocess.Popen([cli, "start", "--id", identity, "--config", configuration(inputs, result)], **options)
            except OSError as exc:
                return {**row, "status": "setup_failed", "output": str(exc)[:800]}
            try:
                while time.monotonic() - started < timeout:
                    if cancel.wait(.25):
                        raise McpCancelled("Project checks stopped.")
                    report.seek(0)
                    try:
                        data = json.loads(report.read(16384))
                    except (UnicodeError, ValueError):
                        data = None
                    if isinstance(data, dict) and data.get("status") in ("passed", "failed", "setup_failed"):
                        row["status"] = data["status"]
                        for key, limit in (("page", 512), ("title", 260), ("output", 2000)):
                            if isinstance(data.get(key), str):
                                row[key] = data[key][:limit]
                        if type(data.get("http")) is int:
                            row["http"] = data["http"]
                        if isinstance(data.get("browser_errors"), list):
                            row["browser_errors"] = [s[:600] for s in data["browser_errors"][:5] if isinstance(s, str)]
                        if row["status"] == "failed":
                            if dependency := missing_dependency(row.get("output", "")):
                                row["missing_dependency"] = dependency
                            row["first_error"] = {"message": next(iter(row.get("browser_errors", [])),
                                                                   row.get("output") or "Browser smoke test failed.")}
                        return {**row, "seconds": round(time.monotonic() - started, 1)}
                    if child.poll() not in (None, 0):
                        return {**row, "status": "setup_failed", "output": "Windows Sandbox could not start."}
                return {**row, "status": "timed_out", "output": "Windows Sandbox did not return a browser result in time."}
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                try:
                    subprocess.run([cli, "stop", "--id", identity], timeout=15, **options)
                except (OSError, subprocess.TimeoutExpired):
                    pass
