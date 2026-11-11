"""Lets a GOG game's SDK serve online-only calls (leaderboards) while we stand in for Galaxy offline.

GOG's SDK refuses calls like FindLeaderboard unless the user is really logged on, which needs a GOG login. IRON NEST
asks for a leaderboard at startup and waits forever. Two things have to change in the game's memory (nothing on
disk, nothing is sent to GOG):

1. the SDK's shared ``"%s: not logged on"`` guard becomes a no-op. It is found by that text (the code that loads it,
   then the function containing that code, from the PE exception table), not by address, so other SDK builds
   with the same strings work too;
2. once the game gets that far, the SDK calls a result callback that has no target (address 0) when the game stores
   stats. A jump to address 0 made from inside the SDK is turned into an immediate return.

Both are done by acting as a small debugger (kernel32 through ctypes, nothing to install). Run it for games listed
in ``Config.gog_hooked``. Research notes: ``research/gog/PEER_FINDINGS.md``.

    python -m baw.galaxy_hook analyze <Galaxy64.dll>   what would be patched
    python -m baw.galaxy_hook launch <game.exe>        start the game under the hook
    python -m baw.galaxy_hook attach <pid>             hook a game that is already starting
"""

import bisect
import ctypes
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

GUARD_TEXT = b"%s: not logged on\x00"
MAX_HELPER_SIZE = 0x200  # the shared guard is tiny; guards inlined in big functions are left alone
HELPER_EXES = ("crash", "unins", "setup", "redist")  # not the game itself: leave them alone
SDK_MODULES = ("galaxy64.dll",)  # 1.152+ carries the peer code itself; 32-bit builds are not handled
RET = b"\xc3"

_LEA_RIP = re.compile(rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]", re.DOTALL)  # lea reg, [rip+disp32]
_SCN_EXEC = 0x20000000


@dataclass
class Analysis:
    guards: List[int] = field(default_factory=list)  # function RVAs to turn into `ret`
    image_size: int = 0
    notes: List[str] = field(default_factory=list)


def analyze(data: bytes) -> Analysis:
    """Find the "not logged on" guard helper in a 64-bit SDK DLL (file bytes)."""
    out = Analysis()
    try:
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("not a PE file")
        machine, count = struct.unpack_from("<HH", data, pe + 4)
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        if machine != 0x8664:
            out.notes.append("not a 64-bit DLL, left alone")
            return out
        opt = pe + 24
        out.image_size = struct.unpack_from("<I", data, opt + 56)[0]
        exc_rva, exc_size = struct.unpack_from("<II", data, opt + 112 + 3 * 8)
        sections = []
        for i in range(count):
            at = pe + 24 + opt_size + i * 40
            vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, at + 8)
            flags = struct.unpack_from("<I", data, at + 36)[0]
            sections.append((rva, min(max(vsize, 1), raw_size or vsize), raw_ptr, flags))
    except (struct.error, ValueError) as exc:
        out.notes.append(f"unreadable DLL ({exc})")
        return out

    def raw_range(section):
        rva, size, ptr, _ = section
        return data[ptr:ptr + size]

    texts = set()
    for section in sections:
        if section[3] & _SCN_EXEC:
            continue
        blob, start = raw_range(section), 0
        while True:
            at = blob.find(GUARD_TEXT, start)
            if at < 0:
                break
            texts.add(section[0] + at)
            start = at + 1
    if not texts:
        out.notes.append("no \"not logged on\" text: nothing to patch")
        return out

    begins, ends = [], []
    exc_raw = _rva_to_raw(sections, exc_rva)
    if exc_raw is not None:
        for at in range(exc_raw, exc_raw + exc_size - 11, 12):
            begin, end = struct.unpack_from("<II", data, at)
            begins.append(begin)
            ends.append(end)

    functions: Dict[int, int] = {}
    for section in sections:
        if not section[3] & _SCN_EXEC:
            continue
        blob = raw_range(section)
        for match in _LEA_RIP.finditer(blob):
            i = match.start()
            if i + 7 > len(blob):
                break
            target = section[0] + i + 7 + struct.unpack_from("<i", blob, i + 3)[0]
            if target not in texts:
                continue
            index = bisect.bisect_right(begins, section[0] + i) - 1
            if index >= 0 and section[0] + i < ends[index]:
                functions[begins[index]] = ends[index] - begins[index]
    for begin, size in sorted(functions.items()):
        if size <= MAX_HELPER_SIZE:
            out.guards.append(begin)
        else:
            out.notes.append(f"guard at +0x{begin:x} is inlined in a big function ({size} bytes), left alone")
    if not out.guards:
        out.notes.append("found the text but no small shared guard helper: nothing to patch")
    return out


def _rva_to_raw(sections, rva: int) -> Optional[int]:
    for start, size, ptr, _ in sections:
        if start <= rva < start + size:
            return ptr + rva - start
    return None


# ---------------------------------------------------------------- the debugger half (Windows only)

DBG_CONTINUE, DBG_EXCEPTION_NOT_HANDLED = 0x00010002, 0x80010001
EXCEPTION_DEBUG_EVENT, CREATE_PROCESS_DEBUG_EVENT, EXIT_PROCESS_DEBUG_EVENT, LOAD_DLL_DEBUG_EVENT = 1, 3, 5, 6
ACCESS_VIOLATION = 0xC0000005
CONTEXT_CONTROL_INTEGER = 0x100003
PAGE_EXECUTE_READWRITE = 0x40
_K32 = None


def _kernel32():
    global _K32
    if _K32 is not None:
        return _K32
    from ctypes import wintypes as w

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    ptr, size_t = ctypes.c_void_p, ctypes.c_size_t
    k.WaitForDebugEvent.argtypes = [ptr, w.DWORD]
    k.WaitForDebugEvent.restype = w.BOOL
    k.ContinueDebugEvent.argtypes = [w.DWORD, w.DWORD, w.DWORD]
    k.ContinueDebugEvent.restype = w.BOOL
    k.DebugActiveProcess.argtypes = [w.DWORD]
    k.DebugActiveProcess.restype = w.BOOL
    k.DebugActiveProcessStop.argtypes = [w.DWORD]
    k.DebugActiveProcessStop.restype = w.BOOL
    k.DebugSetProcessKillOnExit.argtypes = [w.BOOL]
    k.DebugSetProcessKillOnExit.restype = w.BOOL
    k.VirtualProtectEx.argtypes = [ptr, ptr, size_t, w.DWORD, ctypes.POINTER(w.DWORD)]
    k.VirtualProtectEx.restype = w.BOOL
    k.WriteProcessMemory.argtypes = [ptr, ptr, ptr, size_t, ctypes.POINTER(size_t)]
    k.WriteProcessMemory.restype = w.BOOL
    k.ReadProcessMemory.argtypes = [ptr, ptr, ptr, size_t, ctypes.POINTER(size_t)]
    k.ReadProcessMemory.restype = w.BOOL
    k.FlushInstructionCache.argtypes = [ptr, ptr, size_t]
    k.FlushInstructionCache.restype = w.BOOL
    k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenThread.restype = ptr
    k.GetThreadContext.argtypes = [ptr, ptr]
    k.GetThreadContext.restype = w.BOOL
    k.SetThreadContext.argtypes = [ptr, ptr]
    k.SetThreadContext.restype = w.BOOL
    k.CloseHandle.argtypes = [ptr]
    k.CloseHandle.restype = w.BOOL
    k.GetFinalPathNameByHandleW.argtypes = [ptr, w.LPWSTR, w.DWORD, w.DWORD]
    k.GetFinalPathNameByHandleW.restype = w.DWORD
    k.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ptr, ptr, w.BOOL, w.DWORD, ptr, w.LPCWSTR, ptr, ptr]
    k.CreateProcessW.restype = w.BOOL
    _K32 = k
    return k


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_void_p), ("lpDesktop", ctypes.c_void_p),
                ("lpTitle", ctypes.c_void_p), ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
                ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32), ("dwXCountChars", ctypes.c_uint32),
                ("dwYCountChars", ctypes.c_uint32), ("dwFillAttribute", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p),
                ("hStdError", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p), ("dwProcessId", ctypes.c_uint32),
                ("dwThreadId", ctypes.c_uint32)]


class Hook:
    """Debugs one game process until it exits: patches the SDK when it loads, rescues the empty callback."""

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.stop = threading.Event()
        self.sdk: Dict[int, Tuple[int, str]] = {}  # base -> (size, name)
        self.process = None
        self.rescued = 0
        self.patched = 0
        self._cache: Dict[Tuple[str, int, float], Analysis] = {}

    @property
    def ready(self) -> bool:
        """The SDK's guard has been patched in this process."""
        return self.patched > 0

    # --- entry points; each runs the whole debug loop on the calling thread (debug events belong to it) ---
    def attach(self, pid: int) -> None:
        k = _kernel32()
        if not k.DebugActiveProcess(pid):
            raise OSError(f"couldn't attach to {pid} (error {ctypes.get_last_error()})")
        k.DebugSetProcessKillOnExit(False)  # the game keeps running if we go away
        self._loop(pid)

    def launch(self, exe: str) -> int:
        k = _kernel32()
        startup, info = _StartupInfo(), _ProcessInfo()
        startup.cb = ctypes.sizeof(startup)
        command = ctypes.create_unicode_buffer(f'"{exe}"')
        if not k.CreateProcessW(None, command, None, None, False, 0x2, None, str(Path(exe).parent),
                                ctypes.byref(startup), ctypes.byref(info)):  # DEBUG_ONLY_THIS_PROCESS
            raise OSError(f"couldn't start {exe} (error {ctypes.get_last_error()})")
        k.DebugSetProcessKillOnExit(False)
        k.CloseHandle(info.hThread)
        k.CloseHandle(info.hProcess)
        self._loop(info.dwProcessId)
        return info.dwProcessId

    # --- the loop ---
    def _loop(self, pid: int) -> None:
        k = _kernel32()
        event = (ctypes.c_ubyte * 256)()
        self.log(f"hook: watching process {pid}")
        try:
            while not self.stop.is_set():
                if not k.WaitForDebugEvent(event, 250):
                    continue  # timeout (error 121)
                code, event_pid, tid = struct.unpack_from("<III", event, 0)
                status = DBG_CONTINUE
                try:
                    if code == CREATE_PROCESS_DEBUG_EVENT:
                        h_file, self.process = struct.unpack_from("<QQ", event, 16)
                        if h_file:
                            k.CloseHandle(h_file)
                    elif code == LOAD_DLL_DEBUG_EVENT:
                        h_file, base = struct.unpack_from("<QQ", event, 16)
                        self._dll_loaded(h_file, base)
                        if h_file:
                            k.CloseHandle(h_file)
                    elif code == EXCEPTION_DEBUG_EVENT:
                        status = self._exception(event, tid)
                    elif code == EXIT_PROCESS_DEBUG_EVENT:
                        k.ContinueDebugEvent(event_pid, tid, DBG_CONTINUE)
                        self.log(f"hook: game exited ({self.patched} patched, {self.rescued} callbacks rescued)")
                        return
                except Exception as exc:  # never take the game down because the hook tripped
                    self.log(f"hook: event {code} failed: {exc!r}")
                k.ContinueDebugEvent(event_pid, tid, status)
        finally:
            if self.stop.is_set():
                k.DebugActiveProcessStop(pid)  # let the game go on without us

    def _dll_loaded(self, h_file: int, base: int) -> None:
        if not h_file or self.process is None:
            return
        k = _kernel32()
        buf = ctypes.create_unicode_buffer(1024)
        if not k.GetFinalPathNameByHandleW(h_file, buf, len(buf), 0):
            return
        path = buf.value[4:] if buf.value.startswith("\\\\?\\") else buf.value
        if Path(path).name.lower() not in SDK_MODULES:
            return
        stat = Path(path).stat()
        key = (path, stat.st_size, stat.st_mtime)
        if key not in self._cache:
            self._cache[key] = analyze(Path(path).read_bytes())
        plan = self._cache[key]
        for note in plan.notes:
            self.log(f"hook: {Path(path).name}: {note}")
        if plan.image_size:
            self.sdk[base] = (plan.image_size, Path(path).name)
        for rva in plan.guards:
            self._write(base + rva, RET)
            self.patched += 1
            self.log(f"hook: {Path(path).name}+0x{rva:x}: \"not logged on\" guard turned into a no-op")

    def _write(self, address: int, data: bytes) -> None:
        k = _kernel32()
        old, written = ctypes.c_uint32(), ctypes.c_size_t()
        if not k.VirtualProtectEx(self.process, address, len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
            raise OSError(f"VirtualProtectEx failed ({ctypes.get_last_error()})")
        ok = k.WriteProcessMemory(self.process, address, data, len(data), ctypes.byref(written))
        k.VirtualProtectEx(self.process, address, len(data), old.value, ctypes.byref(ctypes.c_uint32()))
        k.FlushInstructionCache(self.process, address, len(data))
        if not ok:
            raise OSError(f"WriteProcessMemory failed ({ctypes.get_last_error()})")

    def _exception(self, event, tid: int) -> int:
        code, _flags, _rec, address, nparams = struct.unpack_from("<IIQQI", event, 16)
        info0 = struct.unpack_from("<Q", event, 16 + 32)[0] if nparams else 0
        if code != ACCESS_VIOLATION or address != 0 or info0 != 8:  # not "executed address 0"
            return DBG_CONTINUE if code == 0x80000003 else DBG_EXCEPTION_NOT_HANDLED  # initial breakpoint
        k = _kernel32()
        thread = k.OpenThread(0x18, False, tid)  # GET_CONTEXT | SET_CONTEXT
        if not thread:
            return DBG_EXCEPTION_NOT_HANDLED
        try:
            raw = ctypes.create_string_buffer(1232 + 16)
            ctx = ctypes.addressof(raw)
            ctx += -ctx % 16  # CONTEXT must be 16-byte aligned
            struct.pack_into("<I", (ctypes.c_char * 1232).from_address(ctx), 0x30, CONTEXT_CONTROL_INTEGER)
            if not k.GetThreadContext(thread, ctx):
                return DBG_EXCEPTION_NOT_HANDLED
            memory = (ctypes.c_char * 1232).from_address(ctx)
            rsp = struct.unpack_from("<Q", memory, 0x98)[0]
            back = ctypes.c_uint64()
            if not k.ReadProcessMemory(self.process, rsp, ctypes.byref(back), 8, None):
                return DBG_EXCEPTION_NOT_HANDLED
            if not any(base <= back.value < base + size for base, (size, _) in self.sdk.items()):
                return DBG_EXCEPTION_NOT_HANDLED  # not called from the SDK: not ours to fix
            struct.pack_into("<Q", memory, 0xF8, back.value)  # Rip: pretend the callee returned
            struct.pack_into("<Q", memory, 0x98, rsp + 8)  # Rsp: pop the return address
            if not k.SetThreadContext(thread, ctx):
                return DBG_EXCEPTION_NOT_HANDLED
            self.rescued += 1
            if self.rescued <= 20:
                self.log("hook: the SDK called an empty callback (address 0); skipped that call")
            return DBG_CONTINUE
        finally:
            k.CloseHandle(thread)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32), ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_int32), ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_wchar * 260)]


def running_processes() -> List[Tuple[int, str]]:
    """(pid, exe name) of every running process."""
    k = _kernel32()
    k.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    snapshot = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    found = []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((entry.th32ProcessID, entry.szExeFile))
            ok = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return found


def exe_path(pid: int) -> Optional[Path]:
    k = _kernel32()
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.OpenProcess.restype = ctypes.c_void_p
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                                             ctypes.POINTER(ctypes.c_uint32)]
    handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buf))
        return Path(buf.value) if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else None
    finally:
        k.CloseHandle(handle)


class HookManager:
    """Notices when one of the given games starts and hooks it until it exits (Windows only)."""

    def __init__(self, folders: Callable[[], List[Path]], log: Callable[[str], None] = print, interval: float = 0.5):
        self.folders = folders
        self.log = log
        self.interval = interval
        self.hooks: Dict[int, Hook] = {}
        self._tried = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if sys.platform == "win32" and self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="galaxy-hook-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for hook in list(self.hooks.values()):
            hook.stop.set()

    def ready(self, pid: int) -> bool:
        hook = self.hooks.get(pid)
        return bool(hook and hook.ready)

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                folders = [Path(f) for f in self.folders()]
                if not folders:
                    continue
                names = {}
                for folder in folders:
                    try:
                        for exe in folder.glob("*.exe"):
                            if not any(word in exe.name.lower() for word in HELPER_EXES):
                                names.setdefault(exe.name.lower(), []).append(folder)
                    except OSError:
                        continue
                alive = set()
                for pid, name in running_processes():
                    alive.add(pid)
                    if pid in self._tried or name.lower() not in names:
                        continue
                    path = exe_path(pid)
                    if path is None or not any(_inside(path, f) for f in names[name.lower()]):
                        continue
                    self._tried.add(pid)
                    self._attach(pid, path)
                self._tried &= alive | set(self.hooks)
                for pid in [p for p in self.hooks if p not in alive]:
                    del self.hooks[pid]
            except Exception as exc:  # the watcher must outlive whatever one pass hits
                self.log(f"hook: watcher error {exc!r}")

    def _attach(self, pid: int, path: Path) -> None:
        hook = Hook(self.log)
        self.hooks[pid] = hook

        def run():
            try:
                hook.attach(pid)
            except Exception as exc:
                self.log(f"hook: couldn't attach to {path.name}: {exc}")

        self.log(f"hook: {path.name} started (pid {pid}), attaching")
        threading.Thread(target=run, name=f"galaxy-hook-{pid}", daemon=True).start()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] not in ("analyze", "launch", "attach"):
        print(__doc__)
        return 2
    command, target = args
    if command == "analyze":
        plan = analyze(Path(target).read_bytes())
        for rva in plan.guards:
            print(f"patch +0x{rva:x}: \"not logged on\" guard helper")
        for note in plan.notes:
            print(f"note: {note}")
        return 0 if plan.guards else 1
    if sys.platform != "win32":
        print("Windows only")
        return 2
    hook = Hook()
    try:
        if command == "launch":
            hook.launch(target)
        else:
            hook.attach(int(target))
    except KeyboardInterrupt:
        hook.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
