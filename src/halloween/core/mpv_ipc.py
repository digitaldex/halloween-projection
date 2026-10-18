"""JSON-IPC-Client fuer mpv (--input-ipc-server).

Plattformuebergreifend: Unix Domain Socket unter Linux/macOS,
Named Pipe unter Windows. mpv erwartet/liefert in beiden Faellen
newline-getrennte JSON-Objekte ueber die Verbindung.

Es werden bewusst ZWEI separate Verbindungen zum selben IPC-Endpunkt
aufgebaut (mpv erlaubt/broadcastet Events an beliebig viele gleichzeitige
Clients): eine fuer Kommandos (write+read synchron, ein Request nach dem
anderen), eine ausschliesslich lesend fuer Events. Grund: Windows Named
Pipes serialisieren blockierende Read-/Write-Operationen auf demselben
Handle faktisch - ein Kommando-write() aus dem Hauptthread haengt sich
sonst auf, sobald der Event-Reader-Thread gerade in einem blockierenden
read() auf genau diesem Handle steckt (klassischer Pipe-Deadlock).
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from typing import Any, Callable, Optional, Sequence

IS_WINDOWS = sys.platform == "win32"


class MpvIPCError(RuntimeError):
    """Fehler bei der Kommunikation mit mpv ueber IPC."""


class MpvIPC:
    """Verbindung zu einem laufenden mpv-Prozess ueber die JSON-IPC-Schnittstelle.

    `command()` ist threadsicher (intern serialisiert). Events (z. B.
    `end-file`) werden ueber den `on_event`-Callback aus einem eigenen,
    rein lesenden Event-Thread gemeldet.
    """

    def __init__(self, ipc_path: str, on_event: Optional[Callable[[dict], None]] = None):
        self._path = ipc_path
        self._on_event = on_event
        self._cmd_conn: Any = None
        self._event_conn: Any = None
        self._cmd_lock = threading.Lock()
        self._request_id = 0
        self._request_id_lock = threading.Lock()
        self._event_thread: Optional[threading.Thread] = None
        self._running = False
        self._mpv_version: Optional[tuple[int, ...]] = None

    @property
    def connected(self) -> bool:
        return self._running and self._cmd_conn is not None

    def connect(
        self,
        timeout: float = 10.0,
        retry_interval: float = 0.2,
        observe: Optional[Sequence[str]] = None,
    ) -> None:
        """Verbindet mit dem IPC-Socket/-Pipe, wartet dabei bis mpv ihn angelegt hat.

        `cmd_conn` bleibt beim Aufbau von `event_conn` bewusst offen: mpv
        braucht nach dem Annehmen der ersten Verbindung kurz (typischerweise
        <2s), bis die naechste Pipe-Instanz fuer einen weiteren Client bereit
        steht - ein Retry, der dabei `cmd_conn` staendig schliesst/neu
        oeffnet, wuerde sich mit `event_conn` gegenseitig die frei werdende
        Instanz wegschnappen und nie fertig werden.

        `observe` nennt Properties, deren Aenderungen als
        `property-change`-Events beim `on_event`-Callback ankommen sollen.
        Das MUSS auf der Event-Verbindung abonniert werden: mpv fuehrt die
        beobachteten Properties pro Client-Verbindung und schickt die
        Meldungen nur an genau die Verbindung, die sie bestellt hat.
        Auf `cmd_conn` abonniert landeten sie dort im Antwortstrom - und
        `_read_response` verwirft alles, was nicht die erwartete
        request_id traegt.

        Abonniert wird noch VOR dem Start des Event-Threads, damit niemand
        gleichzeitig auf derselben Verbindung liest und schreibt (unter
        Windows waere das der klassische Named-Pipe-Deadlock, siehe
        Modul-Docstring).
        """

        deadline = time.monotonic() + timeout
        self._cmd_conn = self._retry_open(deadline, retry_interval)
        self._event_conn = self._retry_open(deadline, retry_interval)

        for observe_id, name in enumerate(observe or (), start=1):
            payload = json.dumps(
                {"command": ["observe_property", observe_id, name], "request_id": 0}
            ) + "\n"
            try:
                self._write(self._event_conn, payload.encode("utf-8"))
            except OSError as exc:
                raise MpvIPCError(f"Konnte {name!r} nicht abonnieren: {exc}") from exc

        self._running = True
        self._event_thread = threading.Thread(
            target=self._event_loop, name=f"mpv-ipc-events-{self._path}", daemon=True
        )
        self._event_thread.start()

    def _open_connection(self) -> Any:
        if IS_WINDOWS:
            # Named Pipes verhalten sich unter Windows wie normale Dateien.
            return open(self._path, "r+b", buffering=0)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._path)
        return sock

    def _retry_open(self, deadline: float, retry_interval: float) -> Any:
        last_err: Optional[OSError] = None
        while time.monotonic() < deadline:
            try:
                return self._open_connection()
            except OSError as exc:
                last_err = exc
                time.sleep(retry_interval)
        raise MpvIPCError(
            f"Konnte nicht mit mpv IPC verbinden ({self._path})"
        ) from last_err

    def close(self) -> None:
        self._running = False
        for attr in ("_cmd_conn", "_event_conn"):
            conn = getattr(self, attr)
            setattr(self, attr, None)
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def _write(self, conn: Any, data: bytes) -> None:
        if IS_WINDOWS:
            conn.write(data)
        else:
            conn.sendall(data)

    def _read_chunk(self, conn: Any) -> bytes:
        if IS_WINDOWS:
            return conn.read(4096)
        return conn.recv(4096)

    def _event_loop(self) -> None:
        buf = b""
        while self._running:
            try:
                chunk = self._read_chunk(self._event_conn)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if "event" in msg and self._on_event is not None:
                    self._on_event(msg)

    def command(self, *args: Any, timeout: float = 10.0) -> Any:
        """Fuehrt ein mpv-IPC-Kommando aus und wartet synchron auf die Antwort.

        Ein Request nach dem anderen (per `_cmd_lock` serialisiert) auf der
        dedizierten Kommando-Verbindung - dadurch ist eindeutig, dass die
        naechste passende `request_id`-Zeile auf dieser Verbindung immer zur
        eigenen Anfrage gehoert.
        """

        with self._cmd_lock:
            if self._cmd_conn is None:
                raise MpvIPCError("IPC-Verbindung ist nicht offen")

            with self._request_id_lock:
                self._request_id += 1
                request_id = self._request_id

            payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
            if not IS_WINDOWS:
                self._cmd_conn.settimeout(timeout)
            try:
                self._write(self._cmd_conn, payload.encode("utf-8"))
                return self._read_response(request_id)
            except OSError as exc:
                raise MpvIPCError(f"mpv-IPC-Fehler bei {args!r}: {exc}") from exc
            finally:
                if not IS_WINDOWS:
                    self._cmd_conn.settimeout(None)

    def _read_response(self, request_id: int) -> Any:
        # Hinweis: Unter Windows gibt es fuer Named-Pipe-Handles kein einfaches
        # Read-Timeout (settimeout() existiert nur bei Sockets) - ein haengender
        # mpv-Prozess wuerde hier blockieren. In der Praxis unkritisch, da mpv
        # lokal und dauerhaft laeuft; unter Unix greift der Socket-Timeout oben.
        buf = b""
        while True:
            chunk = self._read_chunk(self._cmd_conn)
            if not chunk:
                raise MpvIPCError("mpv-IPC-Verbindung wurde geschlossen")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if msg.get("request_id") != request_id:
                    # Broadcast-Event oder fremde Antwort auf derselben
                    # Verbindung - Events kommen zuverlaessig separat ueber
                    # die dedizierte Event-Verbindung, hier ignorieren.
                    continue
                if msg.get("error") != "success":
                    raise MpvIPCError(f"mpv-Fehler bei request_id={request_id}: {msg.get('error')}")
                return msg.get("data")

    def set_property(self, name: str, value: Any) -> None:
        self.command("set_property", name, value)

    def get_property(self, name: str, timeout: float = 10.0) -> Any:
        return self.command("get_property", name, timeout=timeout)

    def loadfile(self, path: str, mode: str = "replace", start: float = 0.0) -> None:
        """Laedt `path`; `start` > 0 wird als Load-Option `start=<s>` mitgegeben.

        mpv hat die Signatur des Kommandos in 0.38.0 geaendert: seitdem steht
        zwischen `mode` und dem Options-Argument zusaetzlich ein `index`
        (-1 = ans Playlist-Ende). Wird der Options-String an der falschen
        Position uebergeben, quittiert mpv das mit "invalid parameter" - daher
        die Versionsabfrage. Ohne Startposition bleibt es beim bisherigen
        3-Argument-Aufruf, der bei jeder Version funktioniert.
        """

        if start <= 0:
            self.command("loadfile", path, mode)
            return
        options = f"start={start:.3f}"
        if self._version_tuple() >= (0, 38, 0):
            self.command("loadfile", path, mode, -1, options)
        else:
            self.command("loadfile", path, mode, options)

    def _version_tuple(self) -> tuple[int, ...]:
        """mpv-Version als Tupel, einmalig abgefragt und gecacht.

        Unbekanntes Format -> (0, 0, 0), also der alte 4-Argument-Aufruf.
        """

        if self._mpv_version is None:
            raw = ""
            try:
                raw = str(self.get_property("mpv-version") or "")
            except MpvIPCError:
                pass
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
            self._mpv_version = tuple(int(g) for g in match.groups()) if match else (0, 0, 0)
        return self._mpv_version
