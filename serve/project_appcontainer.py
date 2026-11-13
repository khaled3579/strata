"""Run a check and its children in a Windows AppContainer with no capabilities."""

import ctypes as C
import msvcrt
import os
import subprocess
import time
import uuid
from ctypes import wintypes as W
from serve.mcp import McpCancelled


def api(library, name, result, *args):
    function = getattr(C.WinDLL(library, use_last_error=True), name)
    function.restype, function.argtypes = result, args
    return function


P = C.c_void_p
SIZE = C.c_size_t
DWORD = W.DWORD
HANDLE = W.HANDLE
BOOL = W.BOOL
WORD = W.WORD
TEXT = W.LPCWSTR


class Startup(C.Structure):
    _fields_ = [("cb", DWORD), ("reserved", TEXT), ("desktop", TEXT), ("title", TEXT),
                ("position", DWORD * 8), ("show", WORD), ("reserved_size", WORD),
                ("reserved_data", P), ("stdin", HANDLE), ("stdout", HANDLE), ("stderr", HANDLE),
                ("attributes", P)]


class Process(C.Structure):
    _fields_ = [("handle", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


class SecurityCapabilities(C.Structure):
    _fields_ = [("sid", P), ("capabilities", P), ("count", DWORD), ("reserved", DWORD)]


class JobLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", DWORD),
                ("working_set_min", SIZE), ("working_set_max", SIZE), ("process_limit", DWORD),
                ("affinity", SIZE), ("priority", DWORD), ("scheduling", DWORD)]


class ExtendedJobLimits(C.Structure):
    _fields_ = [("basic", JobLimits), ("io", C.c_uint64 * 6), ("memory", SIZE * 4)]


create_profile = api("userenv", "CreateAppContainerProfile", C.c_long, TEXT, TEXT, TEXT, P, DWORD, C.POINTER(P))
delete_profile = api("userenv", "DeleteAppContainerProfile", C.c_long, TEXT)
sid_string = api("advapi32", "ConvertSidToStringSidW", BOOL, P, C.POINTER(P))
free_sid = api("advapi32", "FreeSid", P, P)
local_free = api("kernel32", "LocalFree", P, P)
init_attributes = api("kernel32", "InitializeProcThreadAttributeList", BOOL, P, DWORD, DWORD, C.POINTER(SIZE))
update_attributes = api("kernel32", "UpdateProcThreadAttribute", BOOL, P, DWORD, SIZE, P, SIZE, P, P)
delete_attributes = api("kernel32", "DeleteProcThreadAttributeList", None, P)
create_process = api("kernel32", "CreateProcessW", BOOL, TEXT, W.LPWSTR, P, P, BOOL, DWORD, P, TEXT,
                     C.POINTER(Startup), C.POINTER(Process))
create_job = api("kernel32", "CreateJobObjectW", HANDLE, P, TEXT)
set_job = api("kernel32", "SetInformationJobObject", BOOL, HANDLE, C.c_int, P, DWORD)
assign_job = api("kernel32", "AssignProcessToJobObject", BOOL, HANDLE, HANDLE)
terminate_job = api("kernel32", "TerminateJobObject", BOOL, HANDLE, C.c_uint)
terminate_process = api("kernel32", "TerminateProcess", BOOL, HANDLE, C.c_uint)
resume_thread = api("kernel32", "ResumeThread", DWORD, HANDLE)
wait = api("kernel32", "WaitForSingleObject", DWORD, HANDLE, DWORD)
exit_code = api("kernel32", "GetExitCodeProcess", BOOL, HANDLE, C.POINTER(DWORD))
close = api("kernel32", "CloseHandle", BOOL, HANDLE)


def require(ok):
    if not ok:
        raise C.WinError(C.get_last_error())


class AppContainer:
    def __enter__(self):
        self.grants = []
        self.name = "StrataProjectChecks." + uuid.uuid4().hex
        self.sid = P()
        result = create_profile(self.name, "Strata project checks", "Offline project checks", None, 0, C.byref(self.sid))
        if result:
            raise OSError(f"CreateAppContainerProfile failed (0x{result & 0xffffffff:08x})")
        try:
            string = P()
            require(sid_string(self.sid, C.byref(string)))
            try:
                self.account = "*" + C.wstring_at(string)
            finally:
                local_free(string)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        try:
            for directory in reversed(self.grants):
                try:
                    self.revoke(directory)
                except (OSError, subprocess.SubprocessError):
                    pass
        finally:
            delete_profile(self.name)
            free_sid(self.sid)

    def allow(self, directory, rights, *, temporary=False):
        # Grant the container SID only, never the All Application Packages group.
        subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe"), str(directory),
                        "/grant", f"{self.account}:(OI)(CI)({rights})"], check=True, capture_output=True,
                       timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if not temporary:
            self.grants.append(directory)

    def revoke(self, directory):
        subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "icacls.exe"), str(directory),
                        "/remove:g", self.account], check=True, capture_output=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)

    def run(self, command, cwd, env, output, timeout, cancel=None):
        size = SIZE()
        init_attributes(None, 2, 0, C.byref(size))
        require(size.value)
        attributes = C.create_string_buffer(size.value)
        require(init_attributes(attributes, 2, 0, C.byref(size)))
        process = Process()
        job = None
        assigned = False
        try:
            caps = SecurityCapabilities(self.sid, None, 0, 0)
            require(update_attributes(attributes, 0, 0x20009, C.byref(caps), C.sizeof(caps), None, None))
            with open(os.devnull, "rb") as source:
                stdin, stdout = (msvcrt.get_osfhandle(f.fileno()) for f in (source, output))
                handles = (HANDLE * 2)(stdin, stdout)
                require(update_attributes(attributes, 0, 0x20002, handles, C.sizeof(handles), None, None))
                startup = Startup(cb=C.sizeof(Startup), position=(DWORD * 8)(0, 0, 0, 0, 0, 0, 0, 0x100),
                                  stdin=stdin, stdout=stdout, stderr=stdout, attributes=C.addressof(attributes))
                arguments = C.create_unicode_buffer(subprocess.list2cmdline([str(arg) for arg in command]))
                block = C.create_unicode_buffer("\0".join(f"{k}={env[k]}" for k in sorted(env, key=str.upper)) + "\0\0")
                job = create_job(None, None)
                require(job)
                limits = ExtendedJobLimits(basic=JobLimits(flags=0x2000))  # kill descendants if the broker is stopped
                require(set_job(job, 9, C.byref(limits), C.sizeof(limits)))
                flags = 0x00080000 | 0x00000400 | 0x08000000 | 0x00000004
                try:
                    for handle in handles:
                        os.set_handle_inheritable(handle, True)
                    require(create_process(str(command[0]), arguments, None, None, True, flags,
                                           block, str(cwd), C.byref(startup), C.byref(process)))
                finally:
                    for handle in handles:
                        os.set_handle_inheritable(handle, False)
                require(assign_job(job, process.handle))
                assigned = True
                require(resume_thread(process.thread) != 0xffffffff)
                deadline = time.monotonic() + timeout
                while (status := wait(process.handle, 100)) == 258:
                    if cancel is not None and cancel.is_set():
                        raise McpCancelled("Project checks stopped.")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Check exceeded {timeout} seconds")
                require(status == 0)
                code = DWORD()
                require(exit_code(process.handle, C.byref(code)))
                return code.value
        finally:
            if assigned:
                terminate_job(job, 1)
            elif process.handle:
                terminate_process(process.handle, 1)
            if process.handle:
                wait(process.handle, 5000)
                close(process.thread)
                close(process.handle)
            if job:
                close(job)
            delete_attributes(attributes)
