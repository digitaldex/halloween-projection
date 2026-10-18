"""Gemeinsame Player-Logik: MQTT-zu-mpv-Bruecke.

Ein BeamerPlayer = ein dauerhaft laufender mpv (ueber ein `MpvBackend`) +
MQTT-Anbindung. Wie mpv angesteuert wird, entscheidet allein das
uebergebene Backend.

Bewusst ohne jede GUI-Abhaengigkeit: auf einem Beamer-Rechner laeuft das
Modul typischerweise auf einem kleinen Linux ohne Desktop, wo ein
GUI-Toolkit weder installiert noch sinnvoll installierbar ist.
Statusmeldungen gehen darum ueber schlichte Callbacks raus
(`add_state_listener`); ein Frontend kann daran eine Signal-Bruecke
haengen.

Ablauf (die drei Zustaende, die auch im state-Topic stehen):

    waiting   mpv laeuft und zeigt Schwarz, aber noch kein Idle-Clip.
              Nur `action: "init"` wird angenommen - alles andere wird
              protokolliert und verworfen.
    idle      Nach `init`: der Idle-Clip laeuft in Endlosschleife.
    playing   Ein Effektclip laeuft. Am Clip-Ende faellt der Player
              selbsttaetig nach `idle` zurueck - ausgeloest vom Backend,
              das dafuer mpvs `eof-reached` beobachtet.

Das Warten auf `init` ist Absicht: faellt mitten in der Nacht der Strom aus,
soll der Beamer nach dem Neustart nicht von sich aus wieder loslegen,
sondern warten, bis die Showsteuerung ihn scharf schaltet.

mpv wird dabei nie beendet oder neu gestartet - das ist eine der beiden
Grundlagen dafuer, dass zwischen Idle-Loop und Effektclip nichts
aufblitzt. Die andere ist `--keep-open=yes`: mpv laesst am Dateiende den
letzten Frame stehen, sodass er die Ladezeit des naechsten Clips
ueberdeckt (siehe mpv_process.py).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from halloween.core.media_library import MediaLibrary
from halloween.core.mpv_backend import MpvBackend, MpvBackendError
from halloween.core.mqtt_client import MqttClient
from halloween.core.mqtt_schema import (
    PayloadValidationError,
    PlayCommand,
    StateMessage,
    parse_play_command,
)

logger = logging.getLogger(__name__)

# (beamer_id, state: "waiting"|"idle"|"playing", video: str|None)
StateListener = Callable[[str, str, Optional[str]], None]


class BeamerPlayer:
    """Steuert einen Beamer: dauerhaft laufender mpv, per MQTT ferngesteuert."""

    def __init__(
        self,
        beamer_id: str,
        media_dir: Path,
        idle_clip: str,
        mqtt_client: MqttClient,
        base_topic: str,
        backend: MpvBackend,
        library: Optional[MediaLibrary] = None,
        auto_init: bool = False,
    ):
        """`auto_init=True` ueberspringt das Warten auf `init`.

        Gedacht fuer Werkzeuge, bei denen das Startsignal nur im Weg waere
        (Vorschau, Einrichtung). Ein Beamer im Einsatz laesst es auf False,
        damit er nach einem Neustart nicht von allein losspielt.
        """

        self.beamer_id = beamer_id
        self._media_dir = Path(media_dir).resolve()
        self._idle_clip = idle_clip
        self._library = library
        self._mqtt = mqtt_client
        self._cmd_topic = f"{base_topic}/{beamer_id}/cmd"
        self._all_topic = f"{base_topic}/all/cmd"
        self._state_topic = f"{base_topic}/{beamer_id}/state"

        self._backend = backend
        self._backend.on_end_file = self._on_end_file

        self._auto_init = auto_init
        self._initialised = False
        self._current_video: Optional[str] = None
        self._is_idle = True
        self._lock = threading.RLock()
        self._pending_timer: Optional[threading.Timer] = None

        self._state_listeners: list[StateListener] = []

    # -- Statusmeldungen an ein Frontend ------------------------------------

    def add_state_listener(self, listener: StateListener) -> None:
        """Registriert einen Callback fuer Statuswechsel.

        Wird aus dem MQTT-Netzwerk-Thread bzw. einem `at`-Timer-Thread
        aufgerufen, nicht aus dem Hauptthread. Wer damit eine Oberflaeche
        anfasst, muss selbst in deren Thread wechseln - etwa, indem er hier
        ein Signal des jeweiligen Toolkits ausloest.
        """

        self._state_listeners.append(listener)

    @property
    def initialised(self) -> bool:
        """False, solange der Beamer noch auf `action: "init"` wartet."""

        return self._initialised

    # -- Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Startet mpv und abonniert die Kommando-Topics.

        Der Idle-Clip laeuft hier noch NICHT an - mpv zeigt bis zum
        `init`-Befehl seinen schwarzen Hintergrund. Fuer den Zuschauer sieht
        das genauso aus wie die schwarze Idle-Loop, kostet aber keine
        Decoder-Last, und der Beamer bleibt bis zum Startsignal stumm.
        """

        self._backend.start()

        self._mqtt.on(self._cmd_topic, self._on_mqtt_message)
        self._mqtt.on(self._all_topic, self._on_mqtt_message)

        if self._auto_init:
            logger.info("[%s] automatisch scharf geschaltet (auto_init)", self.beamer_id)
            self._initialise()
        else:
            self._publish_state()
            logger.info("[%s] wartet auf init", self.beamer_id)

    def stop(self) -> None:
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
        self._mqtt.off(self._cmd_topic, self._on_mqtt_message)
        self._mqtt.off(self._all_topic, self._on_mqtt_message)
        self._backend.stop()

    # -- MQTT ----------------------------------------------------------------

    def _on_mqtt_message(self, topic: str, payload: bytes, retained: bool = False) -> None:
        if retained:
            # Ein Steuerbefehl ist ein Ereignis, kein Zustand. Laege er
            # retained auf dem Topic, wuerde der Broker ihn bei jedem
            # Verbindungsaufbau erneut ausliefern - ein Reconnect mitten in
            # der Nacht (WLAN-Aussetzer, Broker-Neustart) startete dann
            # ungefragt den Clip von vorhin. Nur protokollieren.
            logger.warning(
                "[%s] retained Befehl auf %s verworfen - Kommandos duerfen "
                "nicht mit retain=true gesendet werden",
                self.beamer_id, topic,
            )
            return
        try:
            cmd = parse_play_command(payload)
        except PayloadValidationError as exc:
            logger.error("Ungueltige Payload auf %s: %s", topic, exc)
            return
        self.handle_command(cmd)

    def handle_command(self, cmd: PlayCommand) -> None:
        with self._lock:
            if cmd.action == "init":
                logger.info(
                    "[%s] init empfangen%s",
                    self.beamer_id,
                    "" if not self._initialised else " (lief bereits - zurueck auf die Idle-Loop)",
                )
                self._initialise()
                return

            if not self._initialised:
                # Bewusst nur protokollieren statt puffern: ein Befehl, der
                # vor dem Startsignal eintrudelt, ist mit hoher
                # Wahrscheinlichkeit eine Altlast (retained Payload, ein
                # Automationslauf von vorhin) und soll die Show nicht
                # verspaetet noch anstossen.
                logger.warning(
                    "[%s] Befehl ignoriert, Beamer wartet noch auf init: %s",
                    self.beamer_id, cmd.to_dict(),
                )
                return

            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None

            if cmd.action == "stop":
                self._play_idle_loop()
                return

            assert cmd.video is not None  # von parse_play_command garantiert
            video_path = self._resolve_media(cmd.video)
            if video_path is None:
                logger.error("Video nicht gefunden oder ungueltig: %s", cmd.video)
                return

            if cmd.at is not None:
                delay = cmd.at - time.time()
                if delay > 0.05:
                    self._pending_timer = threading.Timer(
                        delay, self._execute_play, args=(video_path, cmd)
                    )
                    self._pending_timer.daemon = True
                    self._pending_timer.start()
                    return
                # `at` liegt in der Vergangenheit: um den Versatz vorspulen,
                # damit mehrere Beamer trotzdem an derselben Stelle stehen.
                cmd = replace(cmd, start=cmd.start + max(0.0, -delay), at=None)

            self._execute_play(video_path, cmd)

    def _initialise(self) -> None:
        """`action: "init"`: Idle-Clip starten, ab jetzt Befehle annehmen.

        Mehrfaches `init` ist harmlos - es setzt den Beamer jedes Mal auf
        die Idle-Loop zurueck, was als Notbremse ganz nuetzlich ist.
        """

        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None
            self._initialised = True
        self._play_idle_loop()

    def _resolve_media(self, filename: str) -> Optional[Path]:
        """Video-ID aus dem Payload -> Datei im Media-Verzeichnis.

        Mit `library` sind ausser dem Dateinamen auch Slugs und Aliase
        erlaubt ("raven" statt "JOL2_The Raven_Trio_Pumpkin_deutsch.mp4").
        Ohne Library bleibt es beim reinen Dateinamen. In beiden Faellen
        muss das Ergebnis unterhalb von `media_dir` liegen - sonst koennte
        eine Payload mit "../../etc/passwd" aus dem Verzeichnis ausbrechen.
        """

        if self._library is not None:
            candidate = self._library.resolve(filename)
            if candidate is None:
                return None
        else:
            candidate = self._media_dir / filename

        candidate = candidate.resolve()
        if self._media_dir not in candidate.parents:
            logger.error("Video-Pfad ausserhalb media_dir abgelehnt: %s", filename)
            return None
        return candidate if candidate.is_file() else None

    # -- mpv-Steuerung ---------------------------------------------------------

    def _execute_play(self, video_path: Path, cmd: PlayCommand) -> bool:
        with self._lock:
            self._pending_timer = None
            try:
                self._backend.set_property("loop-file", "inf" if cmd.loop else "no")
                if cmd.volume is not None:
                    self._backend.set_property("volume", cmd.volume)
                self._backend.loadfile(str(video_path), start=cmd.start)
                self._is_idle = False
                # Der aufgeloeste Dateiname, nicht cmd.video: kam der Befehl
                # ueber einen Alias ("raven"), soll der Status trotzdem
                # zeigen, was tatsaechlich laeuft.
                self._current_video = video_path.name
            except MpvBackendError:
                logger.exception("Konnte Effektclip nicht starten: %s", video_path)
                return False
        self._publish_state()
        return True

    def _play_idle_loop(self) -> bool:
        idle_path = self._media_dir / self._idle_clip
        with self._lock:
            try:
                self._backend.set_property("loop-file", "inf")
                self._backend.loadfile(str(idle_path))
                self._is_idle = True
                self._current_video = None
            except MpvBackendError:
                logger.exception("Konnte Idle-Loop nicht starten: %s", idle_path)
                return False
        self._publish_state()
        return True

    def _on_end_file(self, reason: str) -> None:
        # Nur ein echtes "natuerliches" Clip-Ende (nicht: durch replace
        # gestoppt) loest den Ruecksprung in die Idle-Loop aus. Sonst wuerde
        # das Nachladen eines neuen Effekts waehrend ein anderer noch laeuft
        # kurz auf die Idle-Loop zurueckfallen, bevor der neue Clip da ist.
        if reason != "eof":
            return
        with self._lock:
            if self._is_idle or not self._initialised:
                return
        self._play_idle_loop()

    # -- Status ------------------------------------------------------------

    def _publish_state(self) -> None:
        if not self._initialised:
            state = "waiting"
        elif self._is_idle:
            state = "idle"
        else:
            state = "playing"
        msg = StateMessage(state=state, video=self._current_video)
        self._mqtt.publish(self._state_topic, msg.to_json(), retain=True)
        for listener in list(self._state_listeners):
            try:
                listener(self.beamer_id, state, self._current_video)
            except Exception:
                # Ein kaputtes Frontend darf die Wiedergabe nicht mitreissen.
                logger.exception("Fehler in einem State-Listener")
