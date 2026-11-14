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

# "Steam classic": Steam's older achievement toast.
BG = "#243441"
BG_ACCENT = "#34495a"
FG = "#ffffff"
FG_DIM = "#8f98a0"
GOLD = "#e5b143"

# "Purple": a purple card with a rarity badge and a glow for rare achievements.
STEAM_BG = "#2e1d2f"
STEAM_BORDER = "#5c3a5d"
STEAM_HEADER = "#b9a9ba"
STEAM_DESC = "#e3d8e4"
STEAM_BADGE = "#6b3fa6"
STEAM_GLOW = (255, 186, 64)
RARE_PERCENT = 10.0  # Steam makes achievements rarer than this glow
POPUP_STYLES = {"classic": "Steam (blue)", "purple": "Purple with rarity badge"}


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


def _round_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, r: int, **kw) -> int:
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
              x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kw)


def _elide(text: str, font, width: int) -> str:
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "\u2026") > width:
        text = text[:-1]
    return text.rstrip() + "\u2026"


def _fit_lines(text: str, font, width: int, lines: int) -> str:
    """Word-wrap to at most `lines` lines, ending in an ellipsis if it had to cut."""
    out, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if font.measure(candidate) <= width:
            current = candidate
            continue
        if current:
            out.append(current)
        current = word
        if len(out) == lines:
            break
    if current and len(out) < lines:
        out.append(current)
    if out and len(" ".join(out)) < len(" ".join(text.split())):  # cut short: end on an ellipsis
        last = out[-1]
        while last and font.measure(last + "\u2026") > width:
            last = last[:-1]
        out[-1] = last.rstrip() + "\u2026"
    return "\n".join(out)


def _glow_image(size: int, spread: int):
    """A soft golden halo around the icon (Steam's rare-achievement glow). Needs Pillow."""
    if ImageTk is None:
        return None
    try:
        from PIL import ImageDraw, ImageFilter

        total = size + 2 * spread
        img = Image.new("RGBA", (total, total), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle([spread // 2, spread // 2, total - spread // 2, total - spread // 2],
                                      fill=STEAM_GLOW + (200,))
        return ImageTk.PhotoImage(img.filter(ImageFilter.GaussianBlur(spread / 2.2)))
    except Exception:
        return None


def _round_corners(win: tk.Toplevel, width: int, height: int, radius: int) -> None:
    """Clip the window to a rounded rectangle (Windows only; square elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetParent.restype = ctypes.c_void_p
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        user32.SetWindowRgn(hwnd, region, True)  # Windows owns the region from here
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
        if getattr(self.config, "popup_style", "classic") == "purple":
            try:
                self._create_purple(spec)
                return
            except tk.TclError:
                pass  # fall back to the classic look rather than lose the popup
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
        from tkinter import font as tkfont

        desc_font = tkfont.Font(root=self.root, family="Segoe UI", size=-int(12 * scale))
        details = _fit_lines(spec.description, desc_font, wrap, 2)
        tk.Label(win, text=details, font=desc_font, fg=FG_DIM, bg=BG, anchor="nw",
                 justify="left", wraplength=wrap).place(x=text_x, y=int(47 * scale), width=wrap, height=int(36 * scale))
        footer = spec.game
        if spec.percent is not None:
            footer += f"  •  {spec.percent:.1f}% of players"
        tk.Label(win, text=footer, font=("Segoe UI", -int(10 * scale)), fg=GOLD if spec.percent is not None and
                 spec.percent < 10 else FG_DIM, bg=BG, anchor="w").place(x=text_x, y=height - int(20 * scale), width=wrap)

        self._show(win, width, height, foreground)

    def _show(self, win: tk.Toplevel, width: int, height: int, foreground: int, radius: int = 0) -> None:
        win.geometry(f"{width}x{height}+{-10000}+{-10000}")
        self._visible.append(win)
        self._layout(width, height)
        win.update_idletasks()
        _make_unobtrusive(win, foreground)
        if radius:
            _round_corners(win, width, height, radius)
        play_sound(self.config)
        self._fade(win, 0.0, 0.08)
        win.after(int(float(self.config.popup_seconds) * 1000), lambda: self._fade(win, 0.96, -0.08, close=True))

    def _create_purple(self, spec: PopupSpec) -> None:
        """Purple card: 'Achievement unlocked' header, icon with a rarity badge and a golden glow
        for rare achievements, then the name and description."""
        from tkinter import font as tkfont

        dpi = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        s = max(0.5, float(self.config.popup_scale or 1.0)) * dpi
        width, height, radius = int(392 * s), int(136 * s), int(12 * s)
        icon_size, icon_x, icon_y = int(58 * s), int(21 * s), int(52 * s)
        text_x = int(114 * s)
        wrap = width - text_x - int(18 * s)
        foreground = _foreground_window()

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        canvas = tk.Canvas(win, width=width, height=height, bg=STEAM_BG, highlightthickness=0, bd=0)
        canvas.place(x=0, y=0)
        _round_rect(canvas, 1, 1, width - 2, height - 2, radius, outline=STEAM_BORDER, width=max(2, int(2 * s)),
                    fill=STEAM_BG)

        header_font = ("Segoe UI", -int(12 * s))
        canvas.create_text(int(26 * s), int(27 * s), text="🏆", font=("Segoe UI Emoji", -int(11 * s)),
                           fill=STEAM_HEADER, anchor="w")
        canvas.create_text(int(46 * s), int(27 * s), text=spec.header.capitalize() if spec.header.isupper() else spec.header,
                           font=header_font, fill=STEAM_HEADER, anchor="w")

        rare = spec.percent is not None and spec.percent < RARE_PERCENT
        images = []
        if rare:
            glow = _glow_image(icon_size, int(14 * s))
            if glow is not None:
                images.append(glow)
                canvas.create_image(icon_x + icon_size // 2, icon_y + icon_size // 2, image=glow)
        icon = load_icon(spec.icon, icon_size)
        if icon is not None:
            images.append(icon)
            canvas.create_image(icon_x, icon_y, image=icon, anchor="nw")
        else:
            canvas.create_text(icon_x + icon_size // 2, icon_y + icon_size // 2, text="🏆",
                               font=("Segoe UI Emoji", -int(34 * s)), fill=GOLD)
        if spec.percent is not None:
            label = f"{spec.percent:.1f}%" if spec.percent < 1 else f"{spec.percent:.0f}%"
            badge_font = ("Segoe UI", -int(11 * s), "bold")
            text_w = tkfont.Font(root=self.root, font=badge_font).measure(label)
            bw, bh = text_w + int(12 * s), int(17 * s)
            bx, by = icon_x + icon_size // 2 - bw // 2, icon_y + icon_size - bh // 2
            _round_rect(canvas, bx, by, bx + bw, by + bh, bh // 2, fill=STEAM_BADGE, outline="")
            canvas.create_text(bx + bw // 2, by + bh // 2, text=label, font=badge_font, fill="#ffffff")

        title_font = tkfont.Font(root=self.root, family="Segoe UI", size=-int(19 * s))
        canvas.create_text(text_x, int(61 * s), text=_elide(spec.title, title_font, wrap), font=title_font,
                           fill="#ffffff", anchor="w")
        desc_font = tkfont.Font(root=self.root, family="Segoe UI", size=-int(13 * s))
        canvas.create_text(text_x, int(78 * s), text=_fit_lines(spec.description, desc_font, wrap, 3), font=desc_font,
                           fill=STEAM_DESC, anchor="nw", width=wrap)
        win._images = images  # keep references so Tk doesn't drop them
        self._show(win, width, height, foreground, radius)

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
