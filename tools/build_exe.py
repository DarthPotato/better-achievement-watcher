"""Build the one-file exe (used by CI; works locally too: python tools/build_exe.py).

Leaving out what the app never imports keeps the exe about 30% smaller. Pillow is only used to
show PNG/JPEG icons, so its AVIF/WebP/colour-profile/font modules (AVIF alone is ~8 MB) go.
Anything excluded here must not be imported by baw/, or the exe fails at runtime.
"""

import subprocess
import sys
from pathlib import Path

EXCLUDE = [
    # Pillow: only Image + ImageTk with the PNG/JPEG plugins are needed
    "PIL._avif", "PIL.AvifImagePlugin", "PIL._webp", "PIL.WebPImagePlugin", "PIL._imagingcms", "PIL.ImageCms",
    "PIL._imagingft", "PIL.ImageFont", "PIL._imagingmath", "PIL.ImageMath", "PIL._imagingmorph",
    "PIL.ImageMorph", "PIL.ImageQt", "PIL.ImageShow", "PIL.ImageGrab", "PIL.PdfImagePlugin",
    "PIL.FpxImagePlugin", "PIL.MicImagePlugin",
    # standard library the app doesn't use
    "unittest", "doctest", "pdb", "pydoc", "pydoc_data", "_pyrepl", "xmlrpc", "multiprocessing", "asyncio",
    "concurrent", "xml", "pyexpat", "_elementtree", "lzma", "_lzma", "bz2", "_bz2", "compression.zstd", "_zstd",
    "tomllib", "decimal", "_decimal",
    # packages that get picked up from a developer's environment
    "setuptools", "pkg_resources", "_distutils_hack", "packaging", "requests", "urllib3", "cryptography",
    "certifi", "charset_normalizer", "idna", "defusedxml", "numpy",
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
               "--name", "BetterAchievementWatcher", *sys.argv[1:]]
    for module in EXCLUDE:
        command += ["--exclude-module", module]
    command.append(str(root / "BetterAchievementWatcher.pyw"))
    return subprocess.call(command, cwd=root)


if __name__ == "__main__":
    sys.exit(main())
