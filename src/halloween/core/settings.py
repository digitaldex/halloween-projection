"""Laden der Laufzeit-Einstellungen (config/settings.yaml).

`settings.yaml` enthaelt Zugangsdaten (MQTT-Passwort) und gehoert deshalb
NICHT ins Repository - sie steht in `.gitignore`. Im Repo liegt nur
`settings.example.yaml` als Vorlage mit Platzhaltern.

Wer das Passwort auch lokal nicht in einer Datei stehen haben will, kann in
jedem String-Wert Platzhalter benutzen, die beim Laden aus der Prozess-
Umgebung ersetzt werden:

    password: "${HALLOWEEN_MQTT_PASSWORD}"           # Pflicht, sonst Fehler
    host: "${HALLOWEEN_MQTT_HOST:-192.168.1.10}"     # mit Default

Fehlt eine Variable ohne Default, bricht das Laden mit einer verstaendlichen
Meldung ab, statt sich spaeter als "MQTT-Verbindung fehlgeschlagen" zu tarnen.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

# ${NAME} oder ${NAME:-default}
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class SettingsError(RuntimeError):
    """Einstellungen fehlen oder sind unvollstaendig."""


def load_settings(path: str | Path) -> dict[str, Any]:
    """Laedt settings.yaml und ersetzt ${...}-Platzhalter aus der Umgebung."""

    path = Path(path)
    if not path.is_file():
        raise SettingsError(
            f"Einstellungen nicht gefunden: {path}\n"
            f"Vorlage kopieren und ausfuellen:\n"
            f"    cp {path.parent / 'settings.example.yaml'} {path}"
        )

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise SettingsError(f"{path} enthaelt kein YAML-Objekt")
    return _expand(data, path)


def read_mqtt_password(mqtt_cfg: dict[str, Any]) -> Optional[str]:
    """Broker-Passwort, bevorzugt aus `mqtt.password_file`.

    Warum eine eigene Datei statt `password:` bzw. `${HALLOWEEN_MQTT_PASSWORD}`:
    das Passwort darf nicht in der settings.yaml stehen (die liegt im Home und
    ist fuer jeden lesbar), und der Umweg ueber eine Umgebungsvariable ist
    heikel, sobald Sonderzeichen im Spiel sind - jede Schicht dazwischen
    (systemd EnvironmentFile, ein `source` im Shell-Skript) hat ihre eigenen
    Quoting-Regeln und frisst Backslashes oder Anfuehrungszeichen. In der
    Praxis genau so passiert: das Passwort kam um ein Zeichen laenger auf
    der anderen Seite an, der Broker antwortete mit "Not authorized".

    Die Datei wird byte-genau gelesen, nur ein abschliessender Zeilenumbruch
    faellt weg (den setzt jeder Editor automatisch).
    """

    password_file = mqtt_cfg.get("password_file")
    if not password_file:
        return mqtt_cfg.get("password")

    path = Path(password_file).expanduser()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(
            f"Passwortdatei {path} nicht lesbar: {exc}\n"
            f"Anlegen mit:  sudo install -o root -g {Path('~').expanduser().name} -m 640 /dev/null {path}"
        ) from exc
    return content.rstrip("\r\n")


def _expand(node: Any, path: Path) -> Any:
    if isinstance(node, dict):
        return {key: _expand(value, path) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand(item, path) for item in node]
    if isinstance(node, str):
        return _expand_str(node, path)
    return node


def _expand_str(value: str, path: Path) -> str:
    def substitute(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        from_env = os.environ.get(name)
        if from_env is not None:
            return from_env
        if default is not None:
            return default
        raise SettingsError(
            f"Umgebungsvariable {name} ist nicht gesetzt, wird aber in {path} "
            f"gebraucht (Wert: {value!r})."
        )

    return _PLACEHOLDER.sub(substitute, value)
