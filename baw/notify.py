"""Steam-style "Achievement Unlocked" popups drawn with Tk.

Windows toast notifications (what the original Achievement Watcher used) are silently
swallowed by Focus Assist / "Do not disturb", which turns on automatically while a game
is running. This popup is a normal always-on-top window instead, made non-activating and
click-through so it never steals focus from the game. It shows over windowed and
borderless-windowed games; nothing but an in-game overlay shows over exclusive fullscreen.
"""

import array
import math
import struct
import subprocess
import sys
import time
import tkinter as tk
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import Config, data_dir
from .locations import steam_install_dir

try:
    from PIL import Image, ImageTk
except ImportError:  # icons are optional
    Image = ImageTk = None

BG = "#1b2838"
BG_ACCENT = "#2a475e"
FG = "#ffffff"
FG_DIM = "#8f98a0"
GOLD = "#e5b143"


@dataclass
class PopupSpec:
    game: str
    title: str
    description: str = ""
    icon: Optional[Path] = None
    percent: Optional[float] = None
    header: str = "Achievement unlocked"


def load_icon(path: Optional[Path], size: int):
    if not path or ImageTk is None:
        return None
    try:
        with Image.open(path) as img:
            return ImageTk.PhotoImage(img.convert("RGBA").resize((size, size), Image.LANCZOS))
    except Exception:
        return None


# ---------------------------------------------------------------- sound

def _chime_path() -> Path:
    """Generate a short two-note chime once and reuse it."""
    path = data_dir() / "chime.wav"
    if path.is_file():
        return path
    rate = 44100
    frames = bytearray()
    total = int(rate * 0.7)
    samples = [0.0] * total
    for freq, start, length in ((880.0, 0.0, 0.45), (1318.5, 0.09, 0.6)):
        offset = int(rate * start)
        for i in range(min(int(rate * length), total - offset)):
            t = i / rate
            env = math.exp(-t * 7.0) * min(1.0, t * 200)
            samples[offset + i] += 0.35 * env * (math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t))
    for value in samples:
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32000))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return path


# Steam's UI plays desktop_toast_default for achievements on the desktop and
# deck_ui_achievement_toast in Big Picture / on the Steam Deck (steamui/sp.js, ToastAchievement).
STEAM_SOUNDS = {"steam": "desktop_toast_default.wav", "steam_deck": "deck_ui_achievement_toast.wav"}


def steam_sound(style: str = "steam") -> Optional[Path]:
    """Steam's own achievement sound, if Steam is installed. It isn't redistributed with
    this app; it's played straight from your Steam folder."""
    steam = steam_install_dir()
    if steam is None:
        return None
    folder = steam / "steamui" / "sounds"
    preferred = STEAM_SOUNDS.get(style, STEAM_SOUNDS["steam"])
    for name in (preferred, *STEAM_SOUNDS.values()):  # the other one if Steam renamed one
        if (folder / name).is_file():
            return folder / name
    return None


def sound_path(config: Config) -> Path:
    if config.sound_file and Path(config.sound_file).is_file():
        return Path(config.sound_file)
    if config.sound_style in STEAM_SOUNDS:
        steam = steam_sound(config.sound_style)
        if steam:
            return steam
    return _chime_path()


NORMALIZED_PEAK = 0.89  # about -1 dBFS


def _adjusted(path: Path, volume: int, normalize: bool) -> Path:
    """A copy of a 16-bit WAV with its peak evened out and/or its volume scaled, cached in the
    data folder (made from the user's own file, never shipped). The original on failure."""
    volume = max(1, min(100, int(volume)))
    if not normalize and volume >= 100:
        return path
    try:
        stat = path.stat()
        out = data_dir() / "sounds" / f"{path.stem}-{int(stat.st_mtime)}-{stat.st_size}-{volume}-{int(normalize)}.wav"
        if out.is_file():
            return out
        with wave.open(str(path), "rb") as src:
            params = src.getparams()
            frames = src.readframes(src.getnframes())
        if params.sampwidth != 2 or params.comptype != "NONE":
            return path
        samples = array.array("h", frames)
        if sys.byteorder == "big":
            samples.byteswap()
        peak = max((abs(s) for s in samples), default=0) or 1
        gain = (NORMALIZED_PEAK * 32767 / peak if normalize else 1.0) * volume / 100
        scaled = array.array("h", (max(-32768, min(32767, int(s * gain))) for s in samples))
        if sys.byteorder == "big":
            scaled.byteswap()
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_suffix(".part")
        with wave.open(str(temp), "wb") as dst:
            dst.setparams(params)
            dst.writeframes(scaled.tobytes())
        temp.replace(out)
        return out
    except Exception:
        return path


def prepared_sound(config: Config) -> Path:
    """The file to play: Steam's sounds evened out (the desktop one is ~4 dB quieter than the
    others), everything scaled by the volume setting."""
    path = sound_path(config)
    steam = not (config.sound_file and Path(config.sound_file).is_file()) and config.sound_style in STEAM_SOUNDS
    return _adjusted(path, getattr(config, "sound_volume", 100), normalize=steam and path.name != "chime.wav")


def play_sound(config: Config) -> None:
    if not config.sound:
        return
    try:
        sound = prepared_sound(config)
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(str(sound), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        else:
            for player in ("paplay", "aplay", "afplay"):
                try:
                    subprocess.Popen([player, str(sound)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
                except OSError:
                    continue
    except Exception:
        pass


# ---------------------------------------------------------------- Windows window styles

def _make_unobtrusive(win: tk.Toplevel, foreground_before: int) -> None:
    """No taskbar button, no focus stealing, clicks pass through to the game."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
        WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = -1, 0x1, 0x2, 0x10, 0x40
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        if foreground_before and user32.GetForegroundWindow() != foreground_before:
            user32.SetForegroundWindow(foreground_before)
    except Exception:
        pass


def enable_dpi_awareness() -> None:
    """Crisp text on scaled (125%/150%) Windows displays. Call before creating Tk."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _foreground_window() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        return 0


# ---------------------------------------------------------------- popups

class Notifier:
    MAX_VISIBLE = 3

    def __init__(self, root: tk.Tk, config: Config):
        self.root = root
        self.config = config
        self._visible: List[tk.Toplevel] = []
        self._pending: List[PopupSpec] = []

    def show(self, spec: PopupSpec) -> None:
        if len(self._visible) >= self.MAX_VISIBLE:
            self._pending.append(spec)
            return
        self._create(spec)

    def _create(self, spec: PopupSpec) -> None:
        dpi = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        scale = max(0.5, float(self.config.popup_scale or 1.0)) * dpi
        width, height, icon_size = int(350 * scale), int(106 * scale), int(64 * scale)
        foreground = _foreground_window()

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=BG, highlightthickness=1, highlightbackground=BG_ACCENT)
        try:
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        icon = load_icon(spec.icon, icon_size)
        win._icon = icon  # keep a reference so Tk doesn't drop the image
        pad = int(12 * scale)
        if icon is not None:
            tk.Label(win, image=icon, bg=BG, bd=0).place(x=pad, y=(height - icon_size) // 2)
        else:
            tk.Label(win, text="\U0001F3C6", font=("Segoe UI Emoji", -int(38 * scale)), fg=GOLD, bg=BG).place(
                x=pad, y=(height - icon_size) // 2, width=icon_size, height=icon_size)
        text_x = pad * 2 + icon_size
        wrap = width - text_x - pad
        tk.Label(win, text=spec.header.upper(), font=("Segoe UI", -int(11 * scale), "bold"), fg=FG_DIM, bg=BG,
                 anchor="w").place(x=text_x, y=int(9 * scale), width=wrap)
        tk.Label(win, text=spec.title, font=("Segoe UI", -int(15 * scale), "bold"), fg=FG, bg=BG, anchor="w").place(
            x=text_x, y=int(25 * scale), width=wrap)
        details = spec.description
        tk.Label(win, text=details, font=("Segoe UI", -int(12 * scale)), fg=FG_DIM, bg=BG, anchor="nw",
                 justify="left", wraplength=wrap).place(x=text_x, y=int(47 * scale), width=wrap, height=int(36 * scale))
        footer = spec.game
        if spec.percent is not None:
            footer += f"  •  {spec.percent:.1f}% of players"
        tk.Label(win, text=footer, font=("Segoe UI", -int(10 * scale)), fg=GOLD if spec.percent is not None and
                 spec.percent < 10 else FG_DIM, bg=BG, anchor="w").place(x=text_x, y=height - int(20 * scale), width=wrap)

        win.geometry(f"{width}x{height}+{-10000}+{-10000}")
        self._visible.append(win)
        self._layout(width, height)
        win.update_idletasks()
        _make_unobtrusive(win, foreground)
        play_sound(self.config)
        self._fade(win, 0.0, 0.08)
        win.after(int(float(self.config.popup_seconds) * 1000), lambda: self._fade(win, 0.96, -0.08, close=True))

    def _layout(self, width: int, height: int) -> None:
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        dpi = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        margin, gap, taskbar = int(16 * dpi), int(8 * dpi), int(48 * dpi)
        position = self.config.popup_position
        bottom = position.startswith("bottom")
        x = margin if position.endswith("left") else screen_w - width - margin
        for index, win in enumerate(self._visible):
            offset = margin + taskbar * bottom + index * (height + gap)  # leave room for the taskbar
            y = screen_h - height - offset if bottom else offset
            try:
                win.geometry(f"{width}x{height}+{x}+{y}")
            except tk.TclError:
                pass

    def _fade(self, win: tk.Toplevel, alpha: float, step: float, close: bool = False) -> None:
        alpha = max(0.0, min(0.96, alpha + step))
        try:
            win.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if (step > 0 and alpha < 0.96) or (step < 0 and alpha > 0.0):
            win.after(16, lambda: self._fade(win, alpha, step, close))
        elif close:
            self._close(win)

    def _close(self, win: tk.Toplevel) -> None:
        geometry = win.winfo_width(), win.winfo_height()
        if win in self._visible:
            self._visible.remove(win)
        win.destroy()
        if self._visible:
            self._layout(*geometry)
        if self._pending:
            self._create(self._pending.pop(0))


def demo_spec() -> PopupSpec:
    return PopupSpec("Better Achievement Watcher", "It works!",
                     "If you can see this over your game, popups are working.", percent=4.2)


def wait_until_idle(root: tk.Tk, notifier: Notifier, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while (notifier._visible or notifier._pending) and time.time() < deadline:
        root.update()
        time.sleep(0.01)
