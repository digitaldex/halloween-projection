#!/usr/bin/env bash
#
# Richtet einen Raspberry Pi als Beamer-Client ein.
#
# Auf dem Pi ausfuehren, nachdem das Repository nach ~/halloween
# geklont wurde (siehe README.md):
#
#     bash ~/halloween/deploy/install_pi.sh
#
# Idempotent: laesst sich gefahrlos wiederholen. Vorhandene settings.yaml
# und das MQTT-Passwort werden nicht ueberschrieben.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/halloween}"
SERVICE_NAME="halloween-beamer"
PASS_FILE="/etc/halloween/mqtt.pass"
RUN_USER="$(id -un)"

echo "==> Projektverzeichnis: $PROJECT_DIR (Benutzer: $RUN_USER)"
for required in src/halloween/client/main.py deploy/${SERVICE_NAME}.service; do
    if [[ ! -f "$PROJECT_DIR/$required" ]]; then
        echo "FEHLER: $PROJECT_DIR/$required fehlt - erst das Repository klonen." >&2
        exit 1
    fi
done

echo "==> Pakete installieren"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    mpv ffmpeg python3-paho-mqtt python3-yaml

echo "==> Gruppen fuer Grafikausgabe pruefen"
# Ohne video/render kommt mpv nicht an /dev/dri/card0 und damit nicht aufs Bild.
for grp in video render input; do
    if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$grp"; then
        echo "    ergaenze Gruppe $grp"
        sudo usermod -aG "$grp" "$RUN_USER"
    fi
done

echo "==> Konfiguration"
mkdir -p "$PROJECT_DIR/config" "$PROJECT_DIR/media"
if [[ -f "$PROJECT_DIR/config/settings.yaml" ]]; then
    echo "    config/settings.yaml existiert bereits - bleibt unveraendert"
else
    cp "$PROJECT_DIR/config/settings.example.yaml" "$PROJECT_DIR/config/settings.yaml"
    echo "    config/settings.yaml aus der Vorlage angelegt - Broker/beamer_id pruefen!"
fi

echo "==> Idle-Clip"
# Legt media/idle_black.mp4 an, falls es fehlt (Vorgabe fuer media.idle_clip).
python3 "$PROJECT_DIR/scripts/generate_idle_clip.py"

echo "==> MQTT-Passwort"
if sudo test -f "$PASS_FILE"; then
    echo "    $PASS_FILE existiert bereits - bleibt unveraendert"
else
    # Das Passwort steht bewusst NICHT in der settings.yaml (die liegt im
    # Home des Benutzers und ist fuer jeden lesbar), sondern in einer Datei,
    # die root gehoert und nur die Dienstgruppe lesen darf. Der Client liest
    # sie byte-genau selbst (mqtt.password_file) - kein Umweg ueber eine
    # Umgebungsvariable, deren Quoting-Regeln Sonderzeichen verbiegen.
    # printf '%s' ohne \n: der Inhalt soll exakt das Passwort sein.
    read -rsp "    MQTT-Passwort fuer den Broker (leer = ohne Anmeldung): " mqtt_password
    echo
    sudo mkdir -p "$(dirname "$PASS_FILE")"
    printf '%s' "$mqtt_password" | sudo tee "$PASS_FILE" >/dev/null
    sudo chown "root:$RUN_USER" "$PASS_FILE"
    sudo chmod 640 "$PASS_FILE"
    echo "    $PASS_FILE angelegt (root:$RUN_USER, 640)"
fi

echo "==> systemd-Dienst"
# User/Pfade in der Unit auf die tatsaechliche Umgebung umschreiben, damit
# das Skript auch auf einem Pi mit anderem Benutzernamen funktioniert.
sed -e "s|^User=.*|User=$RUN_USER|" \
    -e "s|^Group=.*|Group=$RUN_USER|" \
    -e "s|/home/pi/halloween|$PROJECT_DIR|g" \
    "$PROJECT_DIR/deploy/${SERVICE_NAME}.service" \
    | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo

video_count="$(find "$PROJECT_DIR/media" -maxdepth 1 -name '*.mp4' | wc -l)"
echo "Fertig. $video_count Videodatei(en) in $PROJECT_DIR/media."
echo
echo "Noch pruefen:  $PROJECT_DIR/config/settings.yaml  (mqtt.host, client.beamer_id, media.idle_clip)"
echo "Katalog:       PYTHONPATH=$PROJECT_DIR/src python3 -m halloween.client.main --list-videos"
echo "Starten:       sudo systemctl start $SERVICE_NAME"
echo "Log:           journalctl -u $SERVICE_NAME -f"
