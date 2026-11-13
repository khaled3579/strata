"""Summarize check output independently of the model."""

import re


def check_kind(args):
    tool, *flags = args
    if (tool == "ctest" or tool == "node" and "--test" in flags
            or tool in ("npm", "cargo") and ("test" in flags[:2] or "t" in flags[:1])
            or tool == "python" and flags[:2] in (["-m", "unittest"], ["-m", "pytest"])):
        return "test"
    if tool == "cmake" or any(flag in ("build", "compileall", "typecheck", "check") for flag in flags[:2]):
        return "build"
    return "other"


def test_count(output):
    patterns = (
        r"(?m)^.*?ℹ tests (\d+)\s*$",
        r"(?m)^# tests (\d+)\s*$",
        r"(?m)^Ran (\d+) tests? in ",
        r"(?m)^.*?tests failed out of (\d+)\s*$",
        r"(?m)^Test Files\s+.*\n\s*Tests\s+([^\n]+)",
        r"(?m)^Tests:\s+([^\n]+)",
        r"(?m)^\s*=*\s*((?:\d+ (?:passed|failed|skipped|deselected|errors?|xfailed|xpassed)[, ]*)+) in ",
    )
    for i, pattern in enumerate(patterns):
        match = re.search(pattern, output)
        if match and i < 4:
            return int(match[1])
        if match:
            counts = re.findall(r"(\d+) (passed|failed|skipped|deselected|errors?|xfailed|xpassed)", match[1])
            return sum(int(n) for n, status in counts if status not in ("skipped", "deselected", "error", "errors"))
    rust = re.findall(r"test result: .*?\. (\d+) passed; (\d+) failed;", output)
    if rust:
        return sum(int(passed) + int(failed) for passed, failed in rust)
    if re.search(r"(?im)^\s*(?:no tests (?:ran|were found)|.*\b0 tests collected)", output):
        return 0
    return None


LOCATIONS = (
    r'(?P<file>.+?)\((?P<line>\d+)(?:,\d+)?\)\s*:\s*(?:fatal )?error\b.*',
    r'(?P<file>.+?):(?P<line>\d+)(?::\d+)?\s*[:-]\s*(?:fatal )?error\b.*',
    r'(?P<file>.+?\.\w+):(?P<line>\d+):\s*(?:AssertionError|Error|FAIL|E\b).*',
    r'CMake Error at (?P<file>.+?):(?P<line>\d+).*',
)


def first_error(output):
    lines = [line.strip() for line in output.splitlines()]
    for i, line in enumerate(lines):
        for pattern in LOCATIONS:
            if match := re.fullmatch(pattern, line, re.IGNORECASE):
                return {"file": match["file"][:350], "line": int(match["line"]), "message": line[:600]}
        if re.match(r"^error(?:\[\w+\])?:", line):
            for next_line in lines[i + 1:i + 4]:
                if match := re.fullmatch(r"--> (.+?):(\d+):\d+", next_line):
                    return {"file": match[1][:350], "line": int(match[2]), "message": line[:600]}
    if "Traceback (most recent call last):" in lines or any(line.startswith("FAIL:") for line in lines):
        frame = None
        for line in lines:
            if match := re.match(r'File "(.+)", line (\d+)', line):
                if not match[1].startswith(("<check>", "<frozen")):
                    frame = match
            if frame and re.match(r"^[\w.]+(?:Error|Exception):", line):
                return {"file": frame[1][:350], "line": int(frame[2]), "message": line[:600]}
        if frame:
            return {"file": frame[1][:350], "line": int(frame[2]), "message": "Test failed"}
    if any(re.search(r"\b(?:AssertionError|TypeError|SyntaxError|ReferenceError)\b", line) for line in lines):
        for line in lines:
            if match := re.search(r"\bat (?:[^()]+ \()?((?:[A-Za-z]:[\\/])?[^()]+?\.(?:[cm]?js|tsx?)):(\d+):\d+", line):
                if not match[1].startswith("node:"):
                    return {"file": match[1][:350], "line": int(match[2]), "message": next(
                        (value[:600] for value in lines if "Error" in value), "Test failed")}
    message = next((line for line in lines if re.search(r"(?i)\berror\b|\bfail(?:ed)?\b", line)), "")
    return {"message": message[:600]} if message else None
