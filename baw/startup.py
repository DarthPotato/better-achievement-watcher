"""'Start with Windows' via the per-user Run registry key (no admin rights needed)."""

import sys
from pathlib import Path
from typing import Optional

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "BetterAchievementWatcher"


def supported() -> bool:
    return sys.platform == "win32"


def command(exe: Optional[Path] = None) -> str:
    if exe is not None:
        return f'"{exe}" --background'
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    launcher = Path(__file__).resolve().parent.parent / "BetterAchievementWatcher.pyw"
    return f'"{pythonw if pythonw.exists() else python}" "{launcher}" --background'


def is_enabled() -> bool:
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool, exe: Optional[Path] = None) -> None:
    if not supported():
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command(exe))
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except OSError:
                pass


def _registered_command() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, VALUE_NAME)[0])
    except OSError:
        return ""


def repair() -> None:
    """Keep 'Start with Windows' working after the exe was moved or re-downloaded elsewhere."""
    if not supported() or not getattr(sys, "frozen", False):
        return
    registered = _registered_command()
    if not registered:
        return
    target = registered[1:].split('"', 1)[0] if registered.startswith('"') else registered.split(" ", 1)[0]
    if not Path(target).exists():
        try:
            set_enabled(True)
        except OSError:
            pass
