"""Start a GOG game with logged_on_hook.js loaded (research only; needs ``pip install frida==16.4.10``).

    python run_hook.py "C:\\GOG Games\\IRON NEST Heavy Turret Simulator\\Iron Nest Heavy Turret Simulator.exe"

Run it from a normal terminal (Frida can't create its temp folder inside Claude Code's sandbox), with
galaxy_standin.py (or the app) already listening on 127.0.0.1:9977. Ctrl+C detaches.
"""

import sys
import time
from pathlib import Path

import frida


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    exe = sys.argv[1]
    scripts = sys.argv[2:] or ["logged_on_hook.js"]  # e.g. also probe_leaderboard.js
    source = "\n".join(Path(__file__).with_name(name).read_text(encoding="utf-8") for name in scripts)
    device = frida.get_local_device()
    pid = device.spawn([exe], cwd=str(Path(exe).parent))
    session = device.attach(pid)
    script = session.create_script(source)
    script.on("message", lambda m, _d: print("[hook]", m.get("payload", {}).get("msg", m)))
    script.load()
    device.resume(pid)
    print(f"started pid {pid}; Ctrl+C to detach")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        session.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
