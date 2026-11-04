"""Check GitHub for a newer release of the app."""

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from . import __version__

REPO = "DarthPotato/better-achievement-watcher"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
EXE_NAME = "BetterAchievementWatcher.exe"


@dataclass(frozen=True)
class Release:
    version: str  # without the leading "v"
    page_url: str
    exe_url: str


def parse_version(text: str) -> Tuple[int, ...]:
    """"v1.7.1" -> (1, 7, 1); () when it isn't a version number."""
    match = re.match(r"^\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in match.group(1).split(".")) if match else ()


def is_newer(latest: str, current: str = __version__) -> bool:
    latest_v, current_v = parse_version(latest), parse_version(current)
    return bool(latest_v and current_v) and latest_v > current_v


def latest_release(timeout: float = 10.0) -> Optional[Release]:
    req = urllib.request.Request(LATEST_URL, headers={
        "User-Agent": f"BetterAchievementWatcher/{__version__}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    exe = next((a.get("browser_download_url") for a in data.get("assets") or []
                if a.get("name", "").lower() == EXE_NAME.lower()), None)
    tag = str(data.get("tag_name", ""))
    if not exe or not parse_version(tag):
        return None  # the release build hasn't attached the exe yet
    return Release(tag.lstrip("vV"), str(data.get("html_url", "")), exe)


def download(url: str, target: Path, timeout: float = 60.0) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": f"BetterAchievementWatcher/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as out:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
    part.replace(target)
    return target
