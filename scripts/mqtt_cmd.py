"""Kleines CLI zum Testen der MQTT-Steuerung.

Liest Broker/Zugang aus config/settings.yaml; mit --host (und ggf.
--username/--password) geht es auch ohne settings.yaml. Beispiele:

    python scripts/mqtt_cmd.py init beamer1                       # Beamer scharf schalten (startet die Idle-Loop)
    python scripts/mqtt_cmd.py idle beamer1                       # zurueck in die schwarze Idle-Loop
    python scripts/mqtt_cmd.py play beamer1 raven                 # Clip einmal abspielen, danach Idle
    python scripts/mqtt_cmd.py play beamer1 xyz.mp4 --loop        # Clip endlos
    python scripts/mqtt_cmd.py play beamer1 xyz.mp4 --start 12    # ab Sekunde 12 im Clip starten
    python scripts/mqtt_cmd.py play all xyz.mp4 --at +5           # alle Beamer synchron in 5 s starten
    python scripts/mqtt_cmd.py play all xyz.mp4 --at 1730000000   # ... zu einem absoluten Unix-Zeitpunkt
    python scripts/mqtt_cmd.py idle all
    python scripts/mqtt_cmd.py state                              # retained Status aller Beamer anzeigen
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"

# Laeuft auch ohne `pip install -e .` - das Skript soll direkt aus einem
# frischen Checkout heraus benutzbar sein.
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from halloween.core.settings import SettingsError, load_settings, read_mqtt_password  # noqa: E402


def connect(cfg: dict) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="halloween-mqtt-cmd")
    if cfg.get("username"):
        client.username_pw_set(cfg["username"], cfg.get("password"))
    client.connect(cfg["host"], int(cfg.get("port", 1883)), keepalive=30)
    client.loop_start()
    return client


def publish(client: mqtt.Client, topic: str, payload: dict) -> None:
    data = json.dumps(payload)
    info = client.publish(topic, data, qos=1)
    info.wait_for_publish(timeout=5)
    print(f"-> {topic}  {data}")


def cmd_play(client: mqtt.Client, base: str, args: argparse.Namespace) -> None:
    payload: dict = {"video": args.video, "loop": bool(args.loop)}
    if args.start:
        payload["start"] = args.start
    if args.volume is not None:
        payload["volume"] = args.volume
    if args.at:
        payload["at"] = time.time() + float(args.at[1:]) if args.at.startswith("+") else float(args.at)
        print(f"   startet um {time.strftime('%H:%M:%S', time.localtime(payload['at']))} "
              f"(in {payload['at'] - time.time():.1f} s)")
    publish(client, f"{base}/{args.beamer}/cmd", payload)


def cmd_idle(client: mqtt.Client, base: str, args: argparse.Namespace) -> None:
    publish(client, f"{base}/{args.beamer}/cmd", {"action": "stop"})


def cmd_init(client: mqtt.Client, base: str, args: argparse.Namespace) -> None:
    """Schaltet einen frisch gebooteten Beamer scharf.

    Vor dem init zeigt er Schwarz und verwirft alle play-Befehle.
    """

    publish(client, f"{base}/{args.beamer}/cmd", {"action": "init"})


def cmd_state(client: mqtt.Client, base: str, args: argparse.Namespace) -> None:
    seen: dict[str, str] = {}

    def on_message(_c, _u, msg):
        seen[msg.topic] = msg.payload.decode(errors="replace")

    client.on_message = on_message
    client.subscribe(f"{base}/+/state")
    time.sleep(args.wait)
    if not seen:
        print("(keine retained States empfangen)")
    for topic in sorted(seen):
        print(f"{topic}: {seen[topic]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MQTT-Testkommandos fuer die Halloween-Projektion")
    parser.add_argument("--settings", type=Path, default=SETTINGS)
    parser.add_argument("--host", help="Broker-Adresse (ueberschreibt settings.yaml)")
    parser.add_argument("--port", type=int, help="Broker-Port (Default 1883)")
    parser.add_argument("--username", help="Broker-Benutzer")
    parser.add_argument("--password", help="Broker-Passwort")
    parser.add_argument("--base-topic", help="Basis-Topic (Default 'halloween')")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_play = sub.add_parser("play", help="Clip abspielen")
    p_play.add_argument("beamer", help="beamer1, beamer2, ... oder 'all'")
    p_play.add_argument("video", help="Kurzname, Slug oder Dateiname (siehe --list-videos auf dem Client)")
    p_play.add_argument("--loop", action="store_true", help="endlos wiederholen")
    p_play.add_argument("--start", type=float, default=0.0, help="Startposition im Clip in Sekunden")
    p_play.add_argument("--volume", type=int, help="0..100")
    p_play.add_argument("--at", help="absoluter Unix-Zeitpunkt oder '+SEK' (synchroner Start)")
    p_play.set_defaults(func=cmd_play)

    p_idle = sub.add_parser("idle", help="zurueck in die schwarze Idle-Loop")
    p_idle.add_argument("beamer", help="beamer1, ... oder 'all'")
    p_idle.set_defaults(func=cmd_idle)

    p_init = sub.add_parser("init", help="Beamer scharf schalten (startet die Idle-Loop)")
    p_init.add_argument("beamer", help="beamer1, ... oder 'all'")
    p_init.set_defaults(func=cmd_init)

    p_state = sub.add_parser("state", help="retained Status aller Beamer anzeigen")
    p_state.add_argument("--wait", type=float, default=1.5, help="Sekunden auf retained Nachrichten warten")
    p_state.set_defaults(func=cmd_state)

    args = parser.parse_args()
    cfg: dict = {}
    # Mit --host ist die settings.yaml optional - so laesst sich das Skript
    # von jedem Rechner im Netz aus benutzen, ohne dort etwas einzurichten.
    if args.host is None or args.settings.is_file():
        try:
            cfg = dict(load_settings(args.settings).get("mqtt") or {})
            if args.password is None:
                cfg["password"] = read_mqtt_password(cfg)
        except SettingsError as exc:
            print(exc, file=sys.stderr)
            return 1
    for key in ("host", "port", "username", "password", "base_topic"):
        value = getattr(args, key)
        if value is not None:
            cfg[key] = value

    client = connect(cfg)
    try:
        time.sleep(0.3)
        args.func(client, cfg.get("base_topic", "halloween"), args)
    finally:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
