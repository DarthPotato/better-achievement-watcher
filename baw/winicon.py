"""Windows' own UAC shield icon as a Tk image (the 'needs admin' mark on buttons)."""

import ctypes
import ctypes.wintypes as w
import sys
from typing import Optional

SIID_SHIELD = 77
SHGSI_ICON, SHGSI_SMALLICON = 0x100, 0x1


class SHSTOCKICONINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hIcon", w.HICON), ("iSysImageIndex", ctypes.c_int),
                ("iIcon", ctypes.c_int), ("szPath", w.WCHAR * 260)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


def shield_rgba(size: int) -> Optional[bytes]:
    """The shield drawn at size x size, as top-down RGBA bytes; None off Windows or on failure."""
    if sys.platform != "win32":
        return None
    try:
        shell32, user32, gdi32 = ctypes.windll.shell32, ctypes.windll.user32, ctypes.windll.gdi32
        info = SHSTOCKICONINFO(cbSize=ctypes.sizeof(SHSTOCKICONINFO))
        flags = SHGSI_ICON | (SHGSI_SMALLICON if size <= 16 else 0)
        if shell32.SHGetStockIconInfo(SIID_SHIELD, flags, ctypes.byref(info)) != 0:
            return None
        header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=size, biHeight=-size,
                                  biPlanes=1, biBitCount=32)
        bits = ctypes.c_void_p()
        # Handles are 64-bit: declare the types, or ctypes cuts them down to 32-bit ints.
        gdi32.CreateCompatibleDC.restype = w.HDC
        gdi32.CreateCompatibleDC.argtypes = [w.HDC]
        gdi32.CreateDIBSection.restype = w.HBITMAP
        gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.c_void_p, w.UINT, ctypes.POINTER(ctypes.c_void_p),
                                           w.HANDLE, w.DWORD]
        gdi32.SelectObject.restype = w.HGDIOBJ
        gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
        gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [w.HDC]
        user32.DrawIconEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, w.HICON, ctypes.c_int, ctypes.c_int,
                                      w.UINT, w.HBRUSH, w.UINT]
        user32.DestroyIcon.argtypes = [w.HICON]
        shell32.SHGetStockIconInfo.argtypes = [ctypes.c_int, w.UINT, ctypes.c_void_p]
        dc = gdi32.CreateCompatibleDC(None)
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(dc, bitmap)
        user32.DrawIconEx(dc, 0, 0, info.hIcon, size, size, 0, None, 3)  # DI_NORMAL
        bgra = ctypes.string_at(bits, size * size * 4)
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        user32.DestroyIcon(info.hIcon)
    except Exception:
        return None
    out = bytearray(len(bgra))
    out[0::4], out[1::4], out[2::4], out[3::4] = bgra[2::4], bgra[1::4], bgra[0::4], bgra[3::4]
    return bytes(out)


def shield_image(size: int = 16):
    """A Tk PhotoImage of the shield, or None (needs Pillow for transparency)."""
    data = shield_rgba(size)
    if data is None:
        return None
    try:
        from PIL import Image, ImageTk

        return ImageTk.PhotoImage(Image.frombytes("RGBA", (size, size), data))
    except Exception:
        return None
