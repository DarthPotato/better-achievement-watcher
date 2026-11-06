"""One-time setup for GOG games: GOG's peer library and the GalaxyCommunication service.

- The peer (``GalaxyPeer.dll`` / ``GalaxyPeer64.dll``) is GOG's own code that games load to talk
  to Galaxy. It's downloaded from GOG's public component manifest, like Comet and Heroic do,
  and checked against the manifest's SHA-256. Not redistributed with this app.
- GOG's SDK only connects after starting a Windows service called ``GalaxyCommunication``.
  Without GOG Galaxy installed there is none, so an idle stand-in (Comet's dummy service,
  Apache-2.0) is registered once. That needs admin rights, so it runs elevated (one UAC prompt).
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__
from .config import data_dir

PEER_MANIFEST = "https://cfg.gog.com/desktop-galaxy-peer/7/master/files-windows.json"
SERVICE_NAME = "GalaxyCommunication"
DUMMY_URL = "https://github.com/imLinguin/comet/releases/download/v0.4.1/dummy-service.zip"
DUMMY_ZIP_SHA256 = "e89e837fb33d11e4757fe4ff46255ea35d95e1ed32750ef804a28bee8ef7de53"
DUMMY_EXE_SHA256 = "9c834cfbc106bbab314c78ce3ca8b66807fa3e2f59efee62c2a5b0728ea8d1e3"
SERVICE_EXE = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "GOG.com" / "Galaxy" / "redists" / "GalaxyCommunication.exe"
# Everyone may start/stop it (games run as normal users); the rest is Windows' default.
SERVICE_SDDL = ("D:(A;;RPWPRC;;;WD)(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)"
                "(A;;CCLCSWLOCRRC;;;IU)(A;;CCLCSWLOCRRC;;;SU)")
CREATE_NO_WINDOW = 0x08000000


@dataclass
class Status:
    service: bool
    peer: bool

    @property
    def ready(self) -> bool:
        return self.service and self.peer


def peer_dir() -> Path:
    return data_dir() / "galaxy" / "peer"


class DownloadError(Exception):
    pass


def _get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"BetterAchievementWatcher/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (OSError, ValueError) as exc:  # URLError, HTTPError, timeouts, SSL errors
        reason = getattr(exc, "reason", exc)
        raise DownloadError(f"couldn't download from {urllib.parse.urlparse(url).hostname}: {reason}") from exc


def peer_installed() -> bool:
    return any(peer_dir().glob("msvc-*/GalaxyPeer*.dll"))


def install_peer() -> str:
    """Download (or update) GOG's peer library. Returns its version."""
    manifest = json.loads(_get(PEER_MANIFEST, 20).decode("utf-8"))
    version = str(manifest.get("version", ""))
    target = peer_dir()
    stamp = target / "version.txt"
    try:
        if stamp.read_text(encoding="utf-8").strip() == version and peer_installed():
            return version
    except OSError:
        pass
    base = manifest["baseURI"].rstrip("/")
    for entry in manifest["files"]:
        rel = Path(entry["path"])
        if rel.parts[0] == "peer":  # files are listed as peer/msvc-<n>/<dll>
            rel = Path(*rel.parts[1:])
        packed = _get(f"{base}/{entry['resource']}")
        with zipfile.ZipFile(io.BytesIO(packed)) as archive:
            data = archive.read(archive.namelist()[0])
        if hashlib.sha256(data).hexdigest().lower() != str(entry.get("sha256", "")).lower():
            raise ValueError(f"{rel} doesn't match GOG's checksum")
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".part")
        temp.write_bytes(data)
        os.replace(temp, path)
    stamp.write_text(version, encoding="utf-8")
    return version


def service_installed() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(["sc.exe", "query", SERVICE_NAME], capture_output=True,
                                creationflags=CREATE_NO_WINDOW, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0  # 1060 = doesn't exist


def status() -> Status:
    return Status(service_installed(), peer_installed())


def _dummy_exe() -> bytes:
    packed = _get(DUMMY_URL)
    if hashlib.sha256(packed).hexdigest() != DUMMY_ZIP_SHA256:
        raise ValueError("the downloaded dummy service doesn't match its checksum")
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        data = archive.read("GalaxyCommunication.exe")
    if hashlib.sha256(data).hexdigest() != DUMMY_EXE_SHA256:
        raise ValueError("GalaxyCommunication.exe doesn't match its checksum")
    return data


def install_service() -> bool:
    """Register the idle GalaxyCommunication service (one UAC prompt). True when it exists after."""
    if service_installed():
        return True
    staging = data_dir() / "galaxy" / "GalaxyCommunication.exe"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(_dummy_exe())
    _run_elevated("install", [
        f"New-Item -ItemType Directory -Force -Path '{SERVICE_EXE.parent}' | Out-Null",
        f"Copy-Item -Force -LiteralPath '{staging}' -Destination '{SERVICE_EXE}'",
        f"& sc.exe create {SERVICE_NAME} binPath= '\"{SERVICE_EXE}\"' start= demand | Out-Null",
        f"& sc.exe sdset {SERVICE_NAME} '{SERVICE_SDDL}' | Out-Null",
    ])
    return service_installed()


def uninstall_service() -> bool:
    """Remove the service again (one UAC prompt). True when it's gone after."""
    if not service_installed():
        return True
    _run_elevated("uninstall", [
        f"& sc.exe stop {SERVICE_NAME} | Out-Null",
        f"& sc.exe delete {SERVICE_NAME} | Out-Null",
        f"Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath '{SERVICE_EXE}'",
    ])
    return not service_installed()


def remove_peer() -> None:
    """Delete GOG's downloaded helper library (achievements recorded by the app are kept)."""
    import shutil

    shutil.rmtree(peer_dir(), ignore_errors=True)


def _run_elevated(name: str, commands) -> None:
    """Run PowerShell commands as admin: Windows shows its UAC prompt, and we wait for it."""
    script_path = Path(tempfile.gettempdir()) / f"baw-{name}-galaxy-service.ps1"
    script_path.write_text("\n".join(["$ErrorActionPreference = 'Continue'", *commands]), encoding="utf-8")
    launcher = (f"Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
                f"'-NoProfile','-ExecutionPolicy','Bypass','-File','\"{script_path}\"'")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", launcher], creationflags=CREATE_NO_WINDOW,
                       timeout=300, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass  # UAC declined or PowerShell missing: the caller checks the service afterwards
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


def setup() -> Status:
    """Everything GOG games need. Raises on download problems."""
    install_peer()
    install_service()
    return status()


def uninstall_command() -> str:
    return f"sc.exe delete {SERVICE_NAME}; Remove-Item '{SERVICE_EXE}'"
