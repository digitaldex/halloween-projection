"""MQTT-Payload-Schema und Validierung.

Alle Clients hoeren auf exakt dieselben Topics und Payloads - dieses
Modul ist die gemeinsame Definition davon.

Topics (base_topic konfigurierbar, Default "halloween"):
    halloween/<beamer_id>/cmd     - Steuerbefehl an einen einzelnen Client
    halloween/all/cmd             - Broadcast an alle Clients (synchroner Start)
    halloween/<beamer_id>/state   - Retained Telemetrie vom Client

Die drei Aktionen:
    init   Beamer scharf schalten - startet die Idle-Loop. Vorher nimmt der
           Client keine anderen Befehle an (siehe player.py).
    play   Effektclip abspielen; danach faellt der Client in die Idle-Loop.
    stop   Sofort zurueck in die Idle-Loop.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

Action = Literal["init", "play", "stop"]


class PayloadValidationError(ValueError):
    """Payload entspricht nicht dem erwarteten Schema."""


@dataclass(frozen=True)
class PlayCommand:
    """Steuerbefehl-Payload fuer halloween/<id>/cmd bzw. halloween/all/cmd."""

    action: Action = "play"
    video: Optional[str] = None
    start: float = 0.0
    loop: bool = False
    volume: Optional[int] = None
    at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def parse_play_command(payload: bytes | str | dict) -> PlayCommand:
    """Validiert und parsed eine eingehende cmd-Payload.

    Wirft PayloadValidationError mit einer verstaendlichen Meldung bei
    ungueltigen Daten - Home-Assistant-Templates produzieren gelegentlich
    fehlerhafte Payloads, das darf den Client nicht abstuerzen lassen.
    """

    if isinstance(payload, (bytes, str)):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PayloadValidationError(f"Payload ist kein gueltiges JSON: {exc}") from exc
    else:
        data = payload

    if not isinstance(data, dict):
        raise PayloadValidationError("Payload muss ein JSON-Objekt sein")

    action = data.get("action", "play")
    if action not in ("init", "play", "stop"):
        raise PayloadValidationError(
            f"Ungueltige action: {action!r} (erlaubt: init, play, stop)"
        )

    video = data.get("video")
    if action == "play":
        if not isinstance(video, str) or not video:
            raise PayloadValidationError("Feld 'video' ist bei action=play erforderlich")
    elif video is not None and not isinstance(video, str):
        raise PayloadValidationError("Feld 'video' muss ein String sein")

    start = data.get("start", 0.0)
    if not isinstance(start, (int, float)) or start < 0:
        raise PayloadValidationError(f"Feld 'start' muss eine Zahl >= 0 sein, war: {start!r}")

    loop = data.get("loop", False)
    if not isinstance(loop, bool):
        raise PayloadValidationError(f"Feld 'loop' muss ein Bool sein, war: {loop!r}")

    volume = data.get("volume")
    if volume is not None:
        if not isinstance(volume, (int, float)) or not (0 <= volume <= 100):
            raise PayloadValidationError(f"Feld 'volume' muss 0..100 sein, war: {volume!r}")
        volume = int(volume)

    at = data.get("at")
    if at is not None and not isinstance(at, (int, float)):
        raise PayloadValidationError(f"Feld 'at' muss ein Unix-Timestamp (Zahl) sein, war: {at!r}")

    return PlayCommand(
        action=action,
        video=video,
        start=float(start),
        loop=loop,
        volume=volume,
        at=float(at) if at is not None else None,
    )


@dataclass(frozen=True)
class StateMessage:
    """Telemetrie-Payload fuer das retained state-Topic.

    "waiting" = hochgefahren und mit dem Broker verbunden, wartet aber noch
    auf `action: "init"`; bis dahin zeigt der Beamer Schwarz.
    """

    state: Literal["waiting", "idle", "playing"]
    video: Optional[str] = None
    position: float = 0.0
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))
