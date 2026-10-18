"""Start und Verwaltung eines dauerhaft laufenden mpv-Prozesses.

mpv wird genau einmal gestartet und laeuft permanent (idle=yes) im
Hintergrund. Kein Beenden/Neustarten waehrend des Betriebs - das ist die
Grundlage dafuer, dass Uebergaenge zwischen Idle-Loop und Effektclip ohne
sichtbares Aufblitzen ablaufen.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

IS_WINDOWS = sys.platform == "win32"


def make_ipc_path(name: str) -> str:
    """Erzeugt einen eindeutigen IPC-Endpunkt-Pfad fuer eine mpv-Instanz."""

    if IS_WINDOWS:
        return rf"\\.\pipe\halloween-mpv-{name}-{uuid.uuid4().hex[:8]}"
    return str(Path(tempfile.gettempdir()) / f"halloween-mpv-{name}-{uuid.uuid4().hex[:8]}.sock")


class MpvProcess:
    """Startet mpv mit IPC-Server, optional eingebettet in ein Fenster (--wid).

    `wid` ist die native ID eines vorhandenen Fensters, in das mpv sich
    einbetten soll. Fuer den Vollbild-Betrieb auf einem Beamer bleibt
    `wid=None` und stattdessen `fullscreen=True` gesetzt.
    """

    def __init__(
        self,
        ipc_path: str,
        mpv_binary: str = "mpv",
        wid: Optional[int] = None,
        fullscreen: bool = False,
        extra_args: Optional[list[str]] = None,
    ):
        self._ipc_path = ipc_path
        self._mpv_binary = mpv_binary
        self._wid = wid
        self._fullscreen = fullscreen
        self._extra_args = extra_args or []
        self._proc: Optional[subprocess.Popen] = None

    @property
    def ipc_path(self) -> str:
        return self._ipc_path

    def _build_args(self) -> list[str]:
        args = [
            self._mpv_binary,
            f"--input-ipc-server={self._ipc_path}",
            "--idle=yes",
            # keep-open=yes ist ausdruecklich gewollt: mpv laesst am Dateiende
            # den letzten Frame stehen, statt die Datei zu entladen und auf
            # seinen schwarzen Hintergrund zurueckzufallen. Genau dieser kurze
            # Schwarzmoment war sonst beim Ruecksprung in die Idle-Loop
            # sichtbar - zwischen "Clip zu Ende" und "erster Frame der
            # Idle-Loop steht" liegen auf einem Pi 3 einige hundert
            # Millisekunden (Event -> IPC -> loadfile -> Demux -> Decode). Der
            # stehende letzte Frame ueberdeckt diese Luecke, genau wie in der
            # Gegenrichtung (Idle -> Clip), wo das alte Bild bis zum ersten
            # neuen Frame stehenbleibt.
            #
            # Der Preis: mpv meldet dann KEIN end-file/eof mehr. Den
            # Ruecksprung loest stattdessen die Property `eof-reached` aus,
            # die IpcMpvBackend abonniert und in dasselbe eof-Signal
            # uebersetzt (siehe mpv_backend.py).
            "--keep-open=yes",
            "--force-window=yes",
            "--no-terminal",
            "--no-config",
            "--osc=no",
            "--osd-level=0",
            "--border=no",
            "--cursor-autohide=always",
            "--background-color=#000000",
            "--hr-seek=yes",
            "--really-quiet",
        ]
        if self._wid is not None:
            args.append(f"--wid={self._wid}")
        if self._fullscreen:
            args.append("--fullscreen=yes")
        args.extend(self._extra_args)
        return args

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("mpv-Prozess laeuft bereits")
        creationflags = 0
        if IS_WINDOWS:
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        self._proc = subprocess.Popen(
            self._build_args(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
