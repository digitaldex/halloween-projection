"""Erzeugt den schwarzen Idle-Loop-Clip per ffmpeg.

mpv braucht auch im Ruhezustand eine geladene Datei (idle=yes allein reicht
nicht fuer einen sauberen Dauerbetrieb ohne Fensterwechsel/Aufblitzen) -
dieses Skript legt media/idle_black.mp4 an, falls sie fehlt.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "media" / "idle_black.mp4"


def main() -> int:
    if OUTPUT.exists():
        print(f"Existiert bereits: {OUTPUT}")
        return 0

    if shutil.which("ffmpeg") is None:
        print("ffmpeg nicht im PATH gefunden. Bitte installieren oder idle_black.mp4 manuell ablegen.")
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30",
        "-t", "10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(OUTPUT),
    ]
    subprocess.run(cmd, check=True)
    print(f"Erzeugt: {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
