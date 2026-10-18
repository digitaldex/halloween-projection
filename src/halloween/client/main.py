"""Beamer-Client fuer die echten Abspielrechner (Linux, Vollbild, kein Desktop).

Genau EIN mpv im Vollbild, direkt auf der Grafikkarte (KMS/DRM, ohne X11
oder Wayland). Keine GUI - der Prozess haengt als systemd-Dienst am
Konsolen-TTY.

Ablauf:

1. mpv startet einmal und laeuft dauerhaft (`--idle=yes`), zeigt zunaechst
   Schwarz und wartet auf `{"action": "init"}`; danach laeuft der Idle-Clip
   in Endlosschleife.
2. Ein MQTT-Kommando auf `<base>/<beamer_id>/cmd` oder `<base>/all/cmd`
   laedt den Effektclip in denselben mpv - kein Prozessneustart, damit
   zwischen den Clips nichts aufblitzt.
3. Am Ende des Clips (end-file/eof) faellt der Player selbsttaetig in die
   Idle-Loop zurueck.

Nach aussen meldet der Client drei retained Topics:

    <base>/<beamer_id>/state          {"state": "waiting"|"idle"|"playing", ...}
    <base>/<beamer_id>/availability   "online" / "offline" (Last Will)
    <base>/<beamer_id>/videos         Katalog aller abspielbaren Clips

Der Katalog ist dafuer da, dass Home Assistant eine Auswahlliste bauen kann,
ohne das Media-Verzeichnis des Pi zu kennen.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from halloween.core.media_library import MediaLibrary
from halloween.core.mpv_backend import IpcMpvBackend
from halloween.core.mqtt_client import MqttClient
from halloween.core.settings import SettingsError, load_settings, read_mqtt_password
from halloween.player.player import BeamerPlayer

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"

# mpv-Argumente, wenn settings.yaml unter mpv.args nichts vorgibt. Die
# Begruendung zu jedem Flag steht in config/settings.example.yaml.
DEFAULT_MPV_ARGS = [
    "--vo=gpu",
    "--gpu-context=drm",
    "--hwdec=no",
    "--profile=fast",
]


class BeamerClient:
    """Haelt mpv, MQTT und Player zusammen und raeumt beim Beenden auf."""

    def __init__(self, settings: dict[str, Any], beamer_id: str):
        self.beamer_id = beamer_id
        mqtt_cfg = settings.get("mqtt") or {}
        media_cfg = settings.get("media") or {}
        mpv_cfg = settings.get("mpv") or {}

        self._base_topic = mqtt_cfg.get("base_topic", "halloween")
        self._availability_topic = f"{self._base_topic}/{beamer_id}/availability"
        self._catalog_topic = f"{self._base_topic}/{beamer_id}/videos"

        media_dir = Path(media_cfg.get("dir", "media"))
        if not media_dir.is_absolute():
            media_dir = (PROJECT_ROOT / media_dir).resolve()
        self._idle_clip = media_cfg.get("idle_clip", "idle_black.mp4")

        self.library = MediaLibrary(media_dir, aliases=media_cfg.get("aliases") or {})
        self.library.scan()
        if self.library.resolve(self._idle_clip) is None:
            raise SettingsError(
                f"Idle-Clip {self._idle_clip!r} liegt nicht in {media_dir}. "
                f"Ohne ihn hat der Beamer zwischen den Effekten kein Bild."
            )

        self.mqtt = MqttClient(
            client_id=f"{mqtt_cfg.get('client_id_prefix', 'halloween')}-{beamer_id}-{socket.gethostname()}",
            host=mqtt_cfg.get("host", "localhost"),
            port=int(mqtt_cfg.get("port", 1883)),
            username=mqtt_cfg.get("username"),
            password=read_mqtt_password(mqtt_cfg),
            will_topic=self._availability_topic,
        )
        self.mqtt.on_connected = self._announce

        self.backend = IpcMpvBackend(
            name=beamer_id,
            mpv_binary=mpv_cfg.get("binary", "mpv"),
            fullscreen=True,
            extra_args=list(mpv_cfg.get("args") or DEFAULT_MPV_ARGS),
        )
        self.player = BeamerPlayer(
            beamer_id=beamer_id,
            media_dir=media_dir,
            idle_clip=self._idle_clip,
            mqtt_client=self.mqtt,
            base_topic=self._base_topic,
            backend=self.backend,
            library=self.library,
        )
        self.player.add_state_listener(self._log_state)

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # Der MQTT-Verbindungsaufbau laeuft asynchron und kehrt sofort
        # zurueck, auch wenn der Broker noch nicht da ist - deshalb zuerst.
        # `player.start()` faehrt danach mpv hoch, laesst ihn aber auf
        # Schwarz stehen und wartet auf `action: "init"`. Die Topic-Abos
        # setzt `player.start()` selbst; der MqttClient holt sie nach,
        # sobald die Verbindung steht.
        self.mqtt.connect(retry_forever=True)
        self.player.start()
        logger.info(
            "mpv laeuft und zeigt Schwarz - warte auf "
            '{"action": "init"}, danach Idle-Loop %s',
            self._idle_clip,
        )
        logger.info(
            "Beamer %r hoert auf %s/%s/cmd und %s/all/cmd",
            self.beamer_id, self._base_topic, self.beamer_id, self._base_topic,
        )

    def stop(self) -> None:
        # "offline" bewusst selbst setzen und nicht dem Last Will ueberlassen:
        # bei einem sauberen Stop schickt der Broker das Will gar nicht.
        try:
            self.mqtt.publish(self._availability_topic, "offline", retain=True, qos=1)
        except Exception:
            logger.debug("Konnte 'offline' nicht mehr melden", exc_info=True)
        try:
            self.player.stop()
        finally:
            if self.mqtt.connected:
                self.mqtt.disconnect()

    # -- Meldungen nach aussen ----------------------------------------------

    def _announce(self) -> None:
        """Retained Topics nach jedem (Wieder-)Verbinden neu setzen.

        Nach einem Broker-Neustart sind alle retained Topics weg. Da der
        Client nur beim Start publiziert, stuende der Beamer sonst bis zum
        naechsten Statuswechsel als unbekannt da.
        """

        self.mqtt.publish(self._availability_topic, "online", retain=True, qos=1)
        self.mqtt.publish(
            self._catalog_topic,
            json.dumps(self.library.catalog(idle_clip=self._idle_clip), ensure_ascii=False),
            retain=True,
        )
        logger.info("Verfuegbarkeit und Videokatalog (%d Clips) gemeldet", len(self.library.entries()))

    @staticmethod
    def _log_state(beamer_id: str, state: str, video: Optional[str]) -> None:
        logger.info("[%s] %s%s", beamer_id, state, f" - {video}" if video else "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halloween-client",
        description="MQTT-gesteuerter Vollbild-Videoplayer fuer einen Beamer.",
    )
    parser.add_argument(
        "--settings", type=Path, default=DEFAULT_SETTINGS,
        help=f"Pfad zur settings.yaml (Default: {DEFAULT_SETTINGS})",
    )
    parser.add_argument(
        "--beamer-id", default=None,
        help="ID dieses Beamers; bestimmt die Topics. Default: client.beamer_id "
             "aus settings.yaml, sonst der Hostname.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--list-videos", action="store_true",
        help="Nur den Medienkatalog ausgeben und beenden (kein mpv, kein MQTT).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        settings = load_settings(args.settings)
    except SettingsError as exc:
        logger.error("%s", exc)
        return 2

    beamer_id = (
        args.beamer_id
        or (settings.get("client") or {}).get("beamer_id")
        or socket.gethostname()
    )

    if args.list_videos:
        return _list_videos(settings)

    try:
        client = BeamerClient(settings, beamer_id)
    except (SettingsError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        return 2

    try:
        client.start()
    except Exception:
        logger.exception("Start fehlgeschlagen")
        client.stop()
        return 1

    # Ab hier passiert alles in den Hintergrund-Threads (MQTT-Netzwerk-Thread,
    # mpv-Event-Thread). Der Hauptthread wartet nur noch auf das Abschalt-
    # signal, das systemd beim Stoppen schickt.
    shutdown = threading.Event()

    def on_signal(signum, _frame):
        logger.info("Signal %s empfangen, fahre herunter", signal.Signals(signum).name)
        shutdown.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    shutdown.wait()

    client.stop()
    logger.info("Beendet")
    return 0


def _list_videos(settings: dict[str, Any]) -> int:
    """`--list-videos`: zeigt, unter welchen Namen die Clips ansprechbar sind."""

    media_cfg = settings.get("media") or {}
    media_dir = Path(media_cfg.get("dir", "media"))
    if not media_dir.is_absolute():
        media_dir = (PROJECT_ROOT / media_dir).resolve()

    library = MediaLibrary(media_dir, aliases=media_cfg.get("aliases") or {})
    library.scan()
    idle = media_cfg.get("idle_clip")
    print(f"{len(library.entries())} Clip(s) in {media_dir}\n")
    print(f"{'ID':<22} {'Dauer':>8}  Datei")
    print("-" * 100)
    for entry in library.entries():
        duration = f"{entry.duration:.1f}s" if entry.duration else "-"
        mark = "  (idle)" if entry.filename == idle else ""
        print(f"{entry.id:<22} {duration:>8}  {entry.filename}{mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
