"""Discover and run project build/test commands in an offline Windows AppContainer."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

from serve.mcp import McpCancelled
from serve.project_verification import check_kind, first_error, missing_dependency, test_count
from serve.project_smoke import plan as smoke_plan, run_smoke


TOOL = "project_checks__run"
FILE_CHANGES = {"write_file", "append_file", "edit_file", "create_directory", "move_file", "copy_file", "delete_file"}
TIMEOUT = 180
MAX_FILES = 100_000
MAX_BYTES = 2 * 1024**3
SKIP = {".git", ".svn", ".hg", ".venv", "venv", ".pytest_cache", "__pycache__", "target", ".next", ".turbo", ".strata-build"}
_run_lock = threading.Lock()
_settings_lock = threading.Lock()
SETTINGS_FILE = ".strata-checks.json"


def read_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        return json.loads(stream.read(256 * 1024))


def python_test_command(root):
    test_dir = "tests" if (root / "tests").is_dir() else "."
    config = root / "pyproject.toml"
    pytest = (root / "pytest.ini").is_file()
    if config.is_file():
        import tomllib
        with config.open("rb") as stream:
            data = tomllib.loads(stream.read(256 * 1024).decode("utf-8"))
        pytest = pytest or "pytest" in data.get("tool", {})
    return ["-m", "pytest", "-q", "-p", "no:cacheprovider"] if pytest else ["-m", "unittest", "discover", "-v", "-s", test_dir]


def command_rows(root, commands):
    if not isinstance(commands, list) or not 1 <= len(commands) <= 16:
        raise ValueError("Configure between 1 and 16 check commands.")
    result = []
    for item in commands:
        argv = item.get("argv") if isinstance(item, dict) else item
        cwd = item.get("cwd", ".") if isinstance(item, dict) else "."
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or not all(isinstance(arg, str) and len(arg) <= 2048 and "\0" not in arg for arg in argv)
                or not argv[0].strip()):
            raise ValueError("Each command needs a tool name and an array of arguments.")
        if not isinstance(cwd, str) or not cwd or len(cwd) > 1024 or any(c in cwd for c in ":\\\0"):
            raise ValueError("Use a project-relative working folder with forward slashes.")
        relative = Path(cwd)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("The check working folder must be inside the project.")
        directory = root / relative
        if not directory.is_dir() or directory.resolve() != directory:
            raise ValueError(f"Check working folder is unavailable: {cwd}")
        result.append((" ".join(argv), relative.as_posix(), argv))
    return result


def settings_bytes(root):
    path = root / SETTINGS_FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_nlink != 1 or info.st_size > 256 * 1024):
        raise ValueError("The project check settings file must be a regular local file under 256 KiB.")
    return path.read_bytes()


def configured_commands(root, data):
    config = json.loads(data.decode("utf-8-sig"))
    if isinstance(config, dict) and "commands" not in config and "smoke" in config:
        return discover(root, automatic=True)
    return command_rows(root, config.get("commands") if isinstance(config, dict) else None)


def check_settings(root, update=None):
    with _settings_lock:
        data = settings_bytes(root)
        version = hashlib.sha256(data).hexdigest() if data is not None else "automatic"
        detection_error = ""
        try:
            detected = discover(root, automatic=True)
        except (ValueError, OSError) as exc:
            detected, detection_error = [], str(exc)
        if update is not None:
            if update.get("version") != version:
                raise ValueError("Check settings changed since they were opened. Reopen the settings and try again.")
            try:
                previous = json.loads(data.decode("utf-8-sig")) if data is not None else {}
            except ValueError:
                previous = {}
            saved = {**previous} if isinstance(previous, dict) else {}
            if update.get("automatic") is True:
                saved.pop("commands", None)
            else:
                rows = command_rows(root, update.get("commands"))
                saved["commands"] = [{"argv": argv, "cwd": cwd} for _, cwd, argv in rows]
            if not saved:
                (root / SETTINGS_FILE).unlink(missing_ok=True)
                data = None
            else:
                data = json.dumps(saved, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
                if len(data) > 256 * 1024:
                    raise ValueError("Check settings are limited to 256 KiB.")
                fd, name = tempfile.mkstemp(prefix=".strata-checks-", suffix=".tmp", dir=root)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                    os.replace(name, root / SETTINGS_FILE)
                finally:
                    Path(name).unlink(missing_ok=True)
        error = ""
        automatic = data is None
        try:
            config = json.loads(data.decode("utf-8-sig")) if data is not None else {}
            automatic = automatic or isinstance(config, dict) and "commands" not in config and "smoke" in config
            effective = detected if automatic else configured_commands(root, data)
        except ValueError as exc:
            effective, error = detected, str(exc)

        def describe(rows):
            return [{"argv": argv, "cwd": cwd, "kind": check_kind(argv)} for _, cwd, argv in rows]

        return {"version": hashlib.sha256(data).hexdigest() if data is not None else "automatic", "automatic": automatic,
                "detected": describe(detected), "commands": describe(effective),
                "error": error or detection_error}


def discover(root, automatic=False):
    """Each command has a display name, project-relative cwd and a logical tool name."""
    data = None if automatic else settings_bytes(root)
    if data is not None:
        return configured_commands(root, data)

    result = []
    package = root / "package.json"
    if package.is_file():
        manifest = read_json(package)
        scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
        if isinstance(scripts, dict):
            for script in ("typecheck", "build", "test"):
                if script in scripts:
                    result.append((f"npm {script}", ".", ["npm", "run", script]))
        if not result and any(root.glob("test*")):
            result.append(("Node.js tests", ".", ["node", "--test", "--test-isolation=none"]))
    for relative in (".", "src-tauri"):
        if (root / relative / "Cargo.toml").is_file():
            flags = ["--offline", "--color", "never"]
            if (root / relative / "Cargo.lock").is_file():
                flags.append("--locked")
            for task in ("build", "test"):
                result.append((f"cargo {task} ({relative})", relative, ["cargo", task, *flags]))
    python_project = any((root / name).is_file() for name in ("pyproject.toml", "setup.py", "pytest.ini", "requirements.txt"))
    if python_project or any(root.glob("test*.py")) or any((root / "tests").rglob("test*.py")):
        result.append(("Python compile", ".", ["python", "-m", "compileall", "-q", "-x", r"[/\\]node_modules[/\\]", "."]))
        result.append(("Python tests", ".", ["python", *python_test_command(root)]))
    if (root / "CMakeLists.txt").is_file():
        result.extend([
            ("CMake configure", ".", ["cmake", "-S", ".", "-B", ".strata-build", "-G", "NMake Makefiles",
                                       "-DCMAKE_BUILD_TYPE=Release",
                                       "-DCMAKE_POLICY_DEFAULT_CMP0141=NEW",
                                       "-DCMAKE_MSVC_DEBUG_INFORMATION_FORMAT=Embedded"]),
            ("CMake build", ".", ["cmake", "--build", ".strata-build"]),
            ("CMake tests", ".", ["ctest", "--test-dir", ".strata-build", "--output-on-failure", "--no-tests=error"]),
        ])
    return result


def snapshot(source, destination=None, cancel=None):
    """Fingerprint the source entries and optionally copy regular files."""
    if destination is not None and destination.is_relative_to(source):
        raise ValueError("Select the project directory inside this drive. The selected root includes Strata's temporary workspace.")
    pending = [(source, destination)]
    count = total = skipped = 0
    entries_seen = []
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    while pending:
        if cancel is not None and cancel.is_set():
            raise McpCancelled("Project checks stopped.")
        folder, target = pending.pop()
        if folder.resolve(strict=True) != folder or getattr(folder.lstat(), "st_file_attributes", 0) & reparse:
            raise ValueError("A project directory changed while preparing the snapshot. Run the check again.")
        if target is not None:
            target.mkdir()
        with os.scandir(folder) as entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    raise McpCancelled("Project checks stopped.")
                if entry.name.lower() in SKIP:
                    continue
                info = entry.stat(follow_symlinks=False)
                if getattr(info, "st_file_attributes", 0) & reparse or stat.S_ISLNK(info.st_mode):
                    skipped += 1
                    continue
                path = Path(entry.path)
                output = target / entry.name if target is not None else None
                identity = str(path.relative_to(source))
                if stat.S_ISDIR(info.st_mode):
                    entries_seen.append((identity, "directory"))
                    pending.append((path, output))
                elif stat.S_ISREG(info.st_mode):
                    count += 1
                    total += info.st_size
                    if count > MAX_FILES or total > MAX_BYTES:
                        raise ValueError("The project exceeds the snapshot limit (100,000 files or 2 GiB). Choose its project folder.")
                    entries_seen.append((identity, info.st_size, info.st_mtime_ns, info.st_ino, info.st_ctime_ns))
                    if output is not None:
                        if path.resolve(strict=True) != path:
                            raise ValueError("A project file changed while preparing the snapshot. Run the check again.")
                        shutil.copyfile(path, output)
    return hashlib.sha256(repr(sorted(entries_seen)).encode("utf-8", "surrogatepass")).hexdigest(), skipped


def stage_python(python):
    source = Path(sys.base_prefix)
    python.mkdir()
    for item in [source / "python.exe", *source.glob("*.dll")]:
        if item.is_file():
            shutil.copyfile(item, python / item.name)
    for name in ("DLLs", "Lib"):
        if (source / name).is_dir():
            shutil.copytree(source / name, python / name, ignore=shutil.ignore_patterns("site-packages", "__pycache__"))


def toolchain(commands, configured, scratch, container, env):
    requested = {argv[0] for _, _, argv in commands}
    windows = Path(env["SystemRoot"])
    tools = {}
    paths = [windows / "System32"]
    if requested & {"node", "npm"}:
        node = Path(configured["node"]) if configured.get("node") else None
        if node and node.is_file():
            local_node = scratch / "Node"
            local_node.mkdir()
            shutil.copyfile(node, local_node / "node.exe")
            paths.append(local_node)
            node_flags = "--preserve-symlinks --preserve-symlinks-main"
            tools["node"] = [local_node / "node.exe", *node_flags.split()]
            env["NODE_OPTIONS"] = node_flags + " --test-isolation=none"
            npm = node.parent / "node_modules/npm/bin/npm-cli.js"
            if npm.is_file():
                local_npm = local_node / "node_modules/npm"
                shutil.copytree(npm.parent.parent, local_npm)
                tools["npm"] = [*tools["node"], local_npm / "bin/npm-cli.js"]
                env.update(npm_config_offline="true", npm_config_audit="false", npm_config_fund="false",
                           npm_config_cache=str(scratch / "npm-cache"), npm_config_script_shell=str(windows / "System32/cmd.exe"))
    if "python" in requested:
        python = scratch / "Python"
        stage_python(python)
        if (python / "python.exe").is_file():
            tools["python"] = [python / "python.exe", "-E", "-s"]
            paths.append(python)
            packages = configured.get("python_packages")
            if packages and any(argv[:3] == ["python", "-m", "pytest"] for _, _, argv in commands):
                site = python / "Lib/site-packages"
                site.mkdir(exist_ok=True)
                for dependency in ("pytest", "_pytest", "pluggy", "packaging", "iniconfig", "colorama", "pygments"):
                    source = Path(packages) / dependency
                    if source.is_dir():
                        shutil.copytree(source, site / dependency, ignore=shutil.ignore_patterns("__pycache__"))
                shim = Path(packages) / "py.py"
                if shim.is_file():
                    shutil.copyfile(shim, site / shim.name)
    if requested & {"cargo", "cmake", "ctest"}:
        msvc = Path(configured["native"]) if configured.get("native") else None
        if msvc and (msvc / "msvc/bin/cl.exe").is_file():
            container.allow(msvc, "RX")
            compiler, sdk = msvc / "msvc", msvc / "sdk"
            paths.extend((compiler / "bin", sdk / "bin"))
            env.update(INCLUDE=";".join(map(str, [compiler / "include", *(sdk / "include" / part for part in
                       ("ucrt", "shared", "um", "winrt"))])),
                       LIB=";".join(map(str, [compiler / "lib", sdk / "lib/ucrt/x64", sdk / "lib/um/x64"])))
            cargo, rustc = (msvc / "rust/bin" / file for file in ("cargo.exe", "rustc.exe"))
            if "cargo" in requested and cargo.is_file() and rustc.is_file():
                cargo_home = scratch / "Cargo"
                registry = msvc / "registry"
                if registry.is_dir():
                    shutil.copytree(registry, cargo_home / "registry")
                paths.append(cargo.parent)
                env.update(CARGO_HOME=str(cargo_home), RUSTC=str(rustc), CARGO_NET_OFFLINE="true",
                           CARGO_INCREMENTAL="0", CARGO_TARGET_DIR=str(scratch / "Target"))
                tools["cargo"] = [cargo]
        cmake = Path(configured["cmake"]) if configured.get("cmake") else None
        if cmake and cmake.is_file() and requested & {"cmake", "ctest"}:
            local_cmake = scratch / "CMake"
            shutil.copytree(cmake.parent, local_cmake / "bin")
            shutil.copytree(cmake.parent.parent / "share", local_cmake / "share")
            paths.append(local_cmake / "bin")
            tools.update(cmake=[local_cmake / "bin/cmake.exe"], ctest=[local_cmake / "bin/ctest.exe"])
    env["PATH"] = os.pathsep.join(map(str, paths))
    return tools


def check_output(stream):
    size = stream.seek(0, os.SEEK_END)
    stream.seek(0)
    start = stream.read(128 * 1024)
    if size > len(start):
        stream.seek(max(len(start), size - 128 * 1024))
        start += b"\n" + stream.read(128 * 1024)
    stream.seek(max(0, size - 2500))
    tail = ("[Earlier output omitted]\n" if size > 2500 else "") + stream.read(2500).decode("utf-8", "replace").strip()
    return start.decode("utf-8", "replace"), tail


def run_checks(root, config, cancel, timeout):
    from serve.project_appcontainer import AppContainer

    deadline = time.monotonic() + timeout
    while not _run_lock.acquire(timeout=0.1):
        if cancel.is_set():
            raise McpCancelled("Project checks stopped.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for another project check.")
    try:
        with tempfile.TemporaryDirectory(prefix="strata-check-", dir=config.get("scratch"),
                                         ignore_cleanup_errors=True) as tmp:
            scratch = Path(tmp)
            work = scratch / "Project"
            revision, skipped = snapshot(root, work, cancel)
            commands = discover(work)
            smoke_enabled = config.get("browser_smoke") is True
            if not commands and not smoke_enabled:
                return {"status": "unsupported", "revision": revision,
                        "message": "No build or tests detected. Set commands in .strata-checks.json."}
            env = {key: os.environ[key] for key in ("SystemRoot", "SystemDrive", "WINDIR", "LOCALAPPDATA")}
            env.update(TEMP=tmp, TMP=tmp, USERPROFILE=tmp, HOME=tmp, APPDATA=tmp, CI="true", NO_COLOR="1")
            results = []
            cmake_ready = True
            with AppContainer() as container:
                tools = toolchain(commands, config, scratch, container, env)
                container.allow(scratch, "M", temporary=True)
                for name, directory, argv in commands:
                    result = {"check": name[:350], "command": subprocess.list2cmdline(argv)[:1200],
                              "cwd": directory, "kind": check_kind(argv), "status": "unavailable"}
                    results.append(result)
                    if cancel.is_set():
                        raise McpCancelled("Project checks stopped.")
                    if argv[0] not in tools:
                        result["missing_dependency"] = argv[0][:250]
                        result["output"] = f"Tool unavailable for isolated execution: {argv[0]}."
                        continue
                    if (argv[:3] == ["cmake", "--build", ".strata-build"]
                            or argv[:3] == ["ctest", "--test-dir", ".strata-build"]) and not cmake_ready:
                        result.update(status="skipped", output="CMake configure failed.")
                        continue
                    started = time.monotonic()
                    remaining = max(0, min(TIMEOUT, deadline - started))
                    if remaining <= 0:
                        result.update(status="timed_out", output="The project check time limit was reached.")
                        continue
                    with tempfile.TemporaryFile(dir=scratch) as log:
                        try:
                            code = container.run([*tools[argv[0]], *argv[1:]], work / directory, env, log, remaining, cancel)
                            result.update(status="passed" if code == 0 else "failed", exit_code=code)
                        except TimeoutError:
                            result.update(status="timed_out", output="The command exceeded its time limit.")
                        except OSError as error:
                            result.update(status="setup_failed", output=str(error))
                            if isinstance(error, FileNotFoundError) or getattr(error, "winerror", None) in (2, 3, 126):
                                result["missing_dependency"] = argv[0][:250]
                        evidence, output = check_output(log)
                        for prefix, replacement in ((str(work) + "\\", ""), (work.as_posix() + "/", ""),
                                                    (tmp, "<check>"), (scratch.as_posix(), "<check>")):
                            evidence = evidence.replace(prefix, replacement)
                            output = output.replace(prefix, replacement)
                        if result["kind"] == "test" and result["status"] in ("passed", "failed"):
                            result["test_count"] = test_count(evidence)
                            if result["test_count"] == 0 and (result["status"] == "passed" or
                                    "no tests ran" in evidence or "No tests were found" in evidence):
                                result["status"] = "no_tests"
                        if result["status"] in ("failed", "setup_failed", "timed_out"):
                            if dependency := missing_dependency(evidence):
                                result["missing_dependency"] = dependency
                            result["first_error"] = first_error(evidence) or {
                                "message": result.get("output") or f"Command exited with code {result.get('exit_code')}."
                            }
                        if output:
                            result["output"] = "\n".join(filter(None, (result.get("output"), output)))
                    if argv[:2] == ["cmake", "-S"] and ".strata-build" in argv:
                        cmake_ready = result["status"] == "passed"
                    result["seconds"] = round(time.monotonic() - started, 1)
            if smoke_enabled:
                row = {"check": "Browser smoke test", "kind": "smoke"}
                try:
                    settings = read_json(work / SETTINGS_FILE) if (work / SETTINGS_FILE).is_file() else {}
                    package = read_json(work / "package.json") if (work / "package.json").is_file() else {}
                    if not isinstance(settings, dict):
                        raise ValueError("Project check settings must be a JSON object.")
                    if settings.get("smoke") is False:
                        results.append({**row, "status": "skipped", "output": "Browser smoke test disabled for this project."})
                    elif any(r["kind"] == "build" and r["status"] != "passed" for r in results):
                        results.append({**row, "status": "skipped", "output": "The build did not pass."})
                    else:
                        results.append(run_smoke(work, scratch, config,
                                                 smoke_plan(work, settings, package), cancel, deadline))
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    results.append({**row, "status": "setup_failed", "output": str(error)[:1500]})
            for row in results:
                if row["kind"] == "smoke" and row.get("output"):
                    row["detail"] = row["output"]
            if not results:
                return {"status": "unsupported", "revision": revision, "message": "No checks configured."}
            return {"status": "passed" if all(r["status"] == "passed" for r in results) else "incomplete",
                    "revision": revision,
                    "isolation": "Windows AppContainer (offline)", "skipped_links": skipped, "checks": results}
    finally:
        _run_lock.release()


class ProjectChecks:
    name = "project_checks"
    status = "ready"
    verification_report = True
    tools = [{"name": "run", "description": "Detect and run this project's build and tests on a fresh offline Windows snapshot. Returns actual exit codes and diagnostic output.",
              "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]

    def __init__(self, root, config):
        self.root, self.config = root, config
        self.cfg = {"timeout_s": 600}

    def call(self, tool, arguments, timeout, cancel):
        try:
            if tool != "run" or arguments:
                raise ValueError("The project check takes no arguments.")
            report = run_checks(self.root, self.config, cancel or threading.Event(), timeout)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            report = {"status": "setup_failed", "message": str(error)}
        return {"content": [{"type": "text", "text": json.dumps(report, ensure_ascii=False, indent=2)}],
                "verification": {**report, "checks": [{k: v for k, v in row.items() if k != "output"}
                                                     for row in report.get("checks", [])]},
                "isError": report["status"] != "passed"}
