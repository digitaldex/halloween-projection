"""Backend-Abstraktion fuer die mpv-Steuerung.

`BeamerPlayer` (MQTT-zu-mpv-Bruecke) spricht nur noch gegen dieses kleine
Interface.

`IpcMpvBackend` (hier) startet einen eigenen mpv-Prozess und steuert ihn
ueber JSON-IPC - das ist der Weg fuer einen Beamer im Vollbild. Das
Interface laesst daneben andere Umsetzungen zu, etwa libmpv in-process mit
Software-Renderer, wenn ein Frontend die Frames selbst zeichnen will.

Jede Umsetzung reicht dieselben mpv-Kommandos durch (`loadfile`, `set_property`,
`get_property`) und melden das `end-file`-Event mit dem mpv-Grund
("eof", "stop", "quit", "error", "redirect").

`loadfile()` nimmt die Startposition als Load-Option entgegen (mpv-Option
`start`), statt sie der Aufrufer hinterher per `time-pos` seeken zu lassen:
`loadfile` ist asynchron - bis mpv die neue Datei wirklich geoeffnet hat,
laeuft noch die alte (die Idle-Loop), und ein Seek in diesem Fenster trifft
genau die. Als Load-Option wendet mpv die Position beim Oeffnen selbst an.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from halloween.core.mpv_ipc import MpvIPC, MpvIPCError
from halloween.core.mpv_process import MpvProcess, make_ipc_path

EndFileCallback = Callable[[str], None]


class MpvBackendError(RuntimeError):
    """Fehler bei der Kommunikation mit mpv (backend-unabhaengig)."""


class MpvBackend(ABC):
    def __init__(self) -> None:
        self.on_end_file: Optional[EndFileCallback] = None

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def set_property(self, name: str, value: Any) -> None: ...

    @abstractmethod
    def get_property(self, name: str) -> Any: ...

    @abstractmethod
    def loadfile(self, path: str, start: float = 0.0) -> None:
        """Laedt `path` (mode=replace); `start` = Startposition in Sekunden."""

    def _emit_end_file(self, reason: str) -> None:
        if self.on_end_file is not None:
            self.on_end_file(reason)


class IpcMpvBackend(MpvBackend):
    """Eigener mpv-Prozess, gesteuert ueber --input-ipc-server (JSON-IPC)."""

    def __init__(
        self,
        name: str,
        mpv_binary: str = "mpv",
        wid: Optional[int] = None,
        fullscreen: bool = False,
        extra_args: Optional[list[str]] = None,
    ):
        super().__init__()
        self._ipc_path = make_ipc_path(name)
        self._proc = MpvProcess(
            self._ipc_path,
            mpv_binary=mpv_binary,
            wid=wid,
            fullscreen=fullscreen,
            extra_args=extra_args,
        )
        self._ipc: Optional[MpvIPC] = None

    def start(self) -> None:
        self._proc.start()
        self._ipc = MpvIPC(self._ipc_path, on_event=self._on_event)
        try:
            self._ipc.connect(observe=["eof-reached"])
        except MpvIPCError as exc:
            raise MpvBackendError(str(exc)) from exc

    def stop(self) -> None:
        if self._ipc is not None:
            self._ipc.close()
            self._ipc = None
        self._proc.stop()

    def _on_event(self, event: dict) -> None:
        """Uebersetzt mpv-Events in das eof-Signal, das der Player erwartet.

        Weil der Prozess mit `--keep-open=yes` laeuft (letzter Frame bleibt
        stehen, kein sichtbares Schwarz beim Umschalten), meldet mpv am
        natuerlichen Clip-Ende KEIN end-file mehr. Der Trigger dafuer ist
        stattdessen `eof-reached`, das dabei auf true springt.

        `end-file` bleibt trotzdem abonniert - es kommt weiterhin bei
        Abbruechen (reason "stop", wenn ein loadfile die laufende Datei
        ersetzt) und Ladefehlern (reason "error"). Der Player wertet davon
        ohnehin nur "eof" aus.
        """

        name = event.get("event")
        if name == "end-file":
            self._emit_end_file(str(event.get("reason", "unknown")))
        elif (
            name == "property-change"
            and event.get("name") == "eof-reached"
            # Nur die Flanke nach true zaehlt. Beim Abonnieren und bei jedem
            # neu geladenen Clip schickt mpv hier null bzw. false.
            and event.get("data") is True
        ):
            self._emit_end_file("eof")

    def _require_ipc(self) -> MpvIPC:
        if self._ipc is None:
            raise MpvBackendError("mpv-Backend ist nicht gestartet")
        return self._ipc

    def set_property(self, name: str, value: Any) -> None:
        try:
            self._require_ipc().set_property(name, value)
        except MpvIPCError as exc:
            raise MpvBackendError(str(exc)) from exc

    def get_property(self, name: str) -> Any:
        try:
            return self._require_ipc().get_property(name)
        except MpvIPCError as exc:
            raise MpvBackendError(str(exc)) from exc

    def loadfile(self, path: str, start: float = 0.0) -> None:
        try:
            self._require_ipc().loadfile(path, start=start)
        except MpvIPCError as exc:
            raise MpvBackendError(str(exc)) from exc
