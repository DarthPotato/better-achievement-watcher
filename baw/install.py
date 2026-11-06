"""Put the exe in a fixed per-user place with a Start menu shortcut (also used for updates).

There's no installer: the exe copies itself to %LOCALAPPDATA%\\Programs\\Better Achievement Watcher,
the usual place for per-user apps (no admin rights needed), then restarts from there.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import APP_NAME, startup

INSTANCE_PORT = 47863
EXE_NAME = "BetterAchievementWatcher.exe"
CREATE_NO_WINDOW = 0x08000000


def install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Programs" / APP_NAME


def installed_exe() -> Path:
    return install_dir() / EXE_NAME


def running_exe() -> Optional[Path]:
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


def is_installed(exe: Path) -> bool:
    try:
        return exe.resolve() == installed_exe().resolve()
    except OSError:
        return False


def shortcut_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"


def create_shortcut(target: Path, link: Path) -> bool:
    """A .lnk needs the WScript.Shell COM object; PowerShell is the stdlib-free way to reach it."""
    link.parent.mkdir(parents=True, exist_ok=True)
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:BAW_LINK); "
              "$s.TargetPath = $env:BAW_TARGET; $s.WorkingDirectory = $env:BAW_DIR; $s.Save()")
    env = dict(os.environ, BAW_LINK=str(link), BAW_TARGET=str(target), BAW_DIR=str(target.parent))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                       timeout=30, creationflags=CREATE_NO_WINDOW, check=True, capture_output=True)
        return link.is_file()
    except (OSError, subprocess.SubprocessError):
        return False


def instance_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as conn:
            conn.sendall(b"ping\n")
        return True
    except OSError:
        return False


def stop_running_instance(timeout: float = 15.0) -> None:
    """Ask a running copy to quit so its exe can be replaced."""
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1) as conn:
            conn.sendall(b"quit\n")
    except OSError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline and instance_running():
        time.sleep(0.3)
    time.sleep(1.0)  # let the process exit and release its exe


def _copy_with_retry(source: Path, target: Path, attempts: int = 20) -> None:
    temp = target.with_name(target.name + ".new")
    shutil.copy2(source, temp)
    for attempt in range(attempts):
        try:
            os.replace(temp, target)
            return
        except PermissionError:  # the old exe is still shutting down
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def install(source: Path) -> Path:
    """Copy source over the installed exe (quitting a running copy first) and set up shortcuts."""
    target = installed_exe()
    stop_running_instance()
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        _copy_with_retry(source, target)
    create_shortcut(target, shortcut_path())
    if startup.supported() and startup.is_enabled():
        startup.set_enabled(True, exe=target)
    return target


def launch(exe: Path, *args: str) -> None:
    subprocess.Popen([str(exe), *args], close_fds=True, cwd=str(exe.parent))


def delete_later(path: Path, attempts: int = 30) -> None:
    """Delete a file once the process that ran it has exited (called from the new copy)."""
    for _ in range(attempts):
        try:
            path.unlink()
            break
        except FileNotFoundError:
            break
        except OSError:
            time.sleep(1.0)
    if path.parent.name == "BetterAchievementWatcher-update":  # the updater's download folder
        try:
            path.parent.rmdir()  # only if empty
        except OSError:
            pass
