"""Duenner Wrapper um paho-mqtt: Verbindung, Reconnect-Subscribe, Topic-Dispatch."""

from __future__ import annotations

import logging
from typing import Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# (topic, payload, retained). `retained` unterscheidet eine frisch
# gesendete Nachricht von einer, die der Broker beim Abonnieren aus seinem
# Speicher nachliefert - fuer Steuerbefehle ein wichtiger Unterschied.
MessageHandler = Callable[[str, bytes, bool], None]


class MqttClient:
    """MQTT-Client mit Handler-Registrierung pro Topic.

    Handler werden aus dem paho-Netzwerk-Thread aufgerufen, nicht aus dem
    Hauptthread. Wer daraus eine Oberflaeche aktualisiert, muss selbst in
    deren Thread wechseln, statt Widgets direkt aus dem Handler anzufassen.
    """

    def __init__(
        self,
        client_id: str,
        host: str = "localhost",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        will_topic: str | None = None,
        will_payload: str = "offline",
    ):
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self._client.username_pw_set(username, password)
        if will_topic:
            # Last Will: stirbt der Client unangekuendigt (Strom weg am
            # Beamer-Pi, WLAN-Abriss, Absturz), setzt der Broker dieses
            # retained Topic selbst auf "offline". Ohne das stuende dort
            # ewig der letzte Stand von vor dem Ausfall.
            self._client.will_set(will_topic, will_payload, qos=1, retain=True)
        self._host = host
        self._port = port
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._connected = False

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Wird nach jedem erfolgreichen (Wieder-)Verbinden aufgerufen, aus
        # dem paho-Netzwerk-Thread. Gedacht fuer alles, was der Broker nach
        # einem eigenen Neustart vergessen hat - retained Topics wie
        # Verfuegbarkeit und Videokatalog muessen dann neu raus.
        self.on_connected: Callable[[], None] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def on(self, topic: str, handler: MessageHandler) -> None:
        """Registriert einen Handler fuer ein Topic.

        Mehrere Handler pro Topic sind ausdruecklich erlaubt - mehrere
        Beamer hoeren z. B. alle auf denselben `halloween/all/cmd`-Broadcast.
        """

        is_new_topic = topic not in self._handlers
        self._handlers.setdefault(topic, []).append(handler)
        if is_new_topic and self._connected:
            self._client.subscribe(topic)

    def off(self, topic: str, handler: MessageHandler) -> None:
        """Entfernt einen einzelnen Handler; deabonniert das Topic, wenn
        dadurch keine Handler mehr dafuer registriert sind."""

        handlers = self._handlers.get(topic)
        if handlers is None or handler not in handlers:
            return
        handlers.remove(handler)
        if not handlers:
            del self._handlers[topic]
            if self._connected:
                self._client.unsubscribe(topic)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            logger.error("MQTT-Verbindung fehlgeschlagen: %s", reason_code)
            return
        self._connected = True
        for topic in self._handlers:
            client.subscribe(topic)
        logger.info("MQTT verbunden an %s:%s, %d Topic(s) abonniert", self._host, self._port, len(self._handlers))
        if self.on_connected is not None:
            try:
                self.on_connected()
            except Exception:
                logger.exception("Fehler im on_connected-Callback")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._connected = False
        logger.warning("MQTT-Verbindung getrennt: %s", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        for handler in list(self._handlers.get(msg.topic, ())):
            try:
                handler(msg.topic, msg.payload, bool(msg.retain))
            except Exception:
                logger.exception("Fehler im MQTT-Handler fuer Topic %s", msg.topic)

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> None:
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def connect(self, keepalive: int = 30, retry_forever: bool = False) -> None:
        """Verbindet und startet den Netzwerk-Thread.

        `retry_forever=True` (die Beamer-Clients) kehrt sofort zurueck, auch
        wenn der Broker noch gar nicht da ist, und laesst paho im Hintergrund
        mit wachsender Wartezeit weiterprobieren. Beim Hochfahren nach einem
        Stromausfall ist voellig offen, ob Broker oder Beamer-Pi zuerst
        oben sind - ein harter Fehler beim Start wuerde den Client dann nur
        in eine systemd-Neustartschleife schicken.

        `retry_forever=False` verbindet synchron und wirft bei Misserfolg -
        sinnvoll fuer Werkzeuge, bei denen man den Tippfehler in der
        Konfiguration sofort sehen will, statt stumm ohne Fernsteuerung
        dazustehen.
        """

        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        if retry_forever:
            self._client.connect_async(self._host, self._port, keepalive=keepalive)
        else:
            self._client.connect(self._host, self._port, keepalive=keepalive)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
