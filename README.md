# Halloween-Fensterprojektion

MQTT-gesteuerter Vollbild-Videoplayer für Halloween-Fensterprojektionen
(z. B. mit AtmosFX-Clips). Ein Raspberry Pi hängt per HDMI am Beamer, spielt
zwischen den Effekten eine schwarze Endlosschleife ab und startet auf
Kommando einen Clip – ausgelöst von Home Assistant, Node-RED, einem
Bewegungsmelder oder einfach von der Kommandozeile.

- **Kein Desktop nötig:** mpv gibt direkt auf die Grafikkarte aus (KMS/DRM),
  der Beamer zeigt nie Menüs, Mauszeiger oder Konsolentext.
- **Nahtlose Übergänge:** zwischen Idle-Schleife und Effektclip blitzt
  nichts auf.
- **Mehrere Beamer:** jeder Pi hat eine eigene ID, über `halloween/all/cmd`
  lassen sich alle gleichzeitig – auf Wunsch zeitsynchron – starten.
- **Bequeme Video-Namen:** statt `"JOL2_The Raven_Trio_Pumpkin.mp4"` reicht
  `"raven"`.
- **Ausfallsicher:** startet als systemd-Dienst beim Booten, meldet Status
  und Verfügbarkeit per MQTT und wartet nach einem Stromausfall, bis er
  wieder freigegeben wird.

Die Steuerung per MQTT ist in **[docs/MQTT.md](docs/MQTT.md)** beschrieben,
technische Hintergründe in [docs/TECHNIK.md](docs/TECHNIK.md).

## Was man braucht

| | |
|---|---|
| Abspielrechner | Raspberry Pi 3 oder neuer mit 64-Bit **Raspberry Pi OS Lite** (ohne Desktop). Getestet auf einem Pi 3 mit 1080p30-Material. |
| Beamer | per HDMI am Pi |
| MQTT-Broker | z. B. Mosquitto oder das Mosquitto-Add-on von Home Assistant |
| Videos | `.mp4`-Dateien, z. B. von AtmosFX. Videos sind **nicht** Teil dieses Repositorys. |

## Installation

Die Anleitung geht von einem Pi mit dem Benutzer `pi` und dem
Projektverzeichnis `~/halloween` aus. Andere Benutzernamen funktionieren
genauso – der Installer passt den Dienst an.

### 1. Raspberry Pi vorbereiten

Mit dem [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
*Raspberry Pi OS Lite (64-bit)* auf die SD-Karte schreiben und in den
Einstellungen des Imagers Hostname, Benutzer, WLAN und SSH festlegen.
Pi starten und per SSH verbinden:

```bash
ssh pi@<ip-des-pi>
```

### 2. Projekt herunterladen

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/digitaldex/halloween-projection.git ~/halloween
```

### 3. Installer ausführen

```bash
bash ~/halloween/deploy/install_pi.sh
```

Der Installer

- installiert `mpv`, `ffmpeg`, `python3-paho-mqtt` und `python3-yaml`,
- nimmt den Benutzer in die Gruppen `video`, `render` und `input` auf
  (nötig für die Bildausgabe ohne Desktop),
- legt `config/settings.yaml` aus der Vorlage an,
- erzeugt den schwarzen Idle-Clip `media/idle_black.mp4`,
- fragt einmal nach dem **MQTT-Passwort** und speichert es in
  `/etc/halloween/mqtt.pass` (nur für root und die Dienstgruppe lesbar;
  leer lassen, wenn der Broker keine Anmeldung verlangt),
- richtet den systemd-Dienst `halloween-beamer` ein und aktiviert ihn für
  den Systemstart.

Das Skript ist idempotent: es darf beliebig oft laufen und überschreibt
weder eine vorhandene `settings.yaml` noch das gespeicherte Passwort.

> **paho-mqtt 2.x erforderlich.** Der Client nutzt die API von paho-mqtt 2.
> Raspberry Pi OS auf Basis von Debian 13 („Trixie“) bringt diese Version
> mit. Bei älteren Systemen siehe [Fehlersuche](#fehlersuche).

Gruppenänderungen werden erst nach einem Neustart wirksam:

```bash
sudo reboot
```

### 4. Konfiguration anpassen

```bash
nano ~/halloween/config/settings.yaml
```

Mindestens diese Werte prüfen:

| Schlüssel | Bedeutung |
|---|---|
| `client.beamer_id` | Name dieses Beamers, bestimmt die Topics (`halloween/<beamer_id>/…`). Bei mehreren Beamern **muss** jeder eine eigene ID haben. |
| `mqtt.host`, `mqtt.port` | Adresse des Brokers |
| `mqtt.username` | Benutzer am Broker (leer = ohne Anmeldung) |
| `media.idle_clip` | Clip, der zwischen den Effekten in Schleife läuft. Standard: `idle_black.mp4` |
| `media.aliases` | Kurznamen für die Videos (siehe unten) |
| `mpv.args` | mpv-Optionen. Die Vorgaben sind für einen Pi 3 optimiert, inklusive Tonausgabe über HDMI. |

Alle Optionen sind in [`config/settings.example.yaml`](config/settings.example.yaml)
kommentiert. Statt der Passwortdatei kann das Passwort auch per
Umgebungsvariable gesetzt werden – jeder Wert in der `settings.yaml` darf
`${VARIABLE}` oder `${VARIABLE:-standardwert}` enthalten.

### 5. Videos kopieren

Die Clips gehören nach `~/halloween/media/`. Vom eigenen Rechner aus:

```bash
# Linux / macOS
scp ~/Videos/Halloween/*.mp4 pi@<ip-des-pi>:halloween/media/
```

```powershell
# Windows (PowerShell)
scp "$HOME\Videos\Halloween\*.mp4" pi@<ip-des-pi>:halloween/media/
```

Anschließend auf dem Pi prüfen, unter welchen Namen die Clips ansprechbar
sind:

```bash
cd ~/halloween && PYTHONPATH=src python3 -m halloween.client.main --list-videos
```

Für lange Dateinamen lohnen sich Kurznamen in der `settings.yaml`:

```yaml
media:
  aliases:
    raven:   "JOL2_The Raven_Trio_Pumpkin.mp4"
    welcome: "JOL2_Welcome To Our Haunt_Trio_Pumpkin.mp4"
```

### 6. Starten und testen

```bash
sudo systemctl start halloween-beamer
journalctl -u halloween-beamer -f
```

Nach dem Start zeigt der Beamer Schwarz und **wartet auf das Startsignal**.
Erst ein `init` schaltet ihn scharf, dann läuft der Idle-Clip an. Zum
Testen von einem beliebigen Rechner mit den `mosquitto-clients`:

```bash
mosquitto_pub -h <broker> -u <user> -P '<passwort>' -t halloween/beamer1/cmd -m '{"action": "init"}'
mosquitto_pub -h <broker> -u <user> -P '<passwort>' -t halloween/beamer1/cmd -m '{"video": "raven"}'
```

Damit ist die Installation abgeschlossen. Alles Weitere zur Steuerung –
Payload-Felder, Statusmeldungen, Home-Assistant-Beispiele, synchroner
Start mehrerer Beamer – steht in **[docs/MQTT.md](docs/MQTT.md)**.

### Weitere Beamer

Für jeden weiteren Pi die Schritte 1–6 wiederholen und in Schritt 4 eine
eigene `client.beamer_id` vergeben (`beamer2`, `beamer3`, …). Ohne eigene
ID hören mehrere Pis auf dieselben Topics. Ist `beamer_id` leer, wird der
Hostname des Pi verwendet.

### Aktualisieren

```bash
cd ~/halloween && git pull
sudo systemctl restart halloween-beamer
```

`config/settings.yaml` und die Videos in `media/` sind nicht versioniert
und bleiben bei einem Update unverändert.

## Betrieb

```bash
sudo systemctl status halloween-beamer     # läuft er?
sudo systemctl restart halloween-beamer    # nach jeder Änderung an settings.yaml
journalctl -u halloween-beamer -f          # Log mitlesen
journalctl -u halloween-beamer -b -1       # Log des vorherigen Boots
```

Der Dienst startet nach Abstürzen automatisch neu und beendet beim Stoppen
auch mpv, sodass die Grafikkarte wieder frei ist.

**Idle-Clip ändern:** `media.idle_clip` auf eine andere Datei aus `media/`
setzen und den Dienst neu starten. Danach wartet der Beamer wieder auf
`init`.

## Steuerung per MQTT – Kurzfassung

| Topic | Richtung | Inhalt |
|---|---|---|
| `halloween/<beamer_id>/cmd` | an einen Beamer | Befehl |
| `halloween/all/cmd` | an alle Beamer | Befehl |
| `halloween/<beamer_id>/state` | vom Beamer, retained | `waiting` / `idle` / `playing` |
| `halloween/<beamer_id>/availability` | vom Beamer, retained | `online` / `offline` |
| `halloween/<beamer_id>/videos` | vom Beamer, retained | Katalog aller Clips |

| Payload | Wirkung |
|---|---|
| `{"action": "init"}` | Beamer scharf schalten (nach jedem Start nötig) |
| `{"video": "raven"}` | Clip einmal abspielen, danach zurück in die Idle-Schleife |
| `{"video": "raven", "loop": true}` | Clip endlos wiederholen |
| `{"video": "raven", "start": 12}` | ab Sekunde 12 abspielen |
| `{"video": "raven", "at": 1761930000}` | zu einem Unix-Zeitpunkt starten (synchron über mehrere Beamer) |
| `{"action": "stop"}` | sofort zurück in die Idle-Schleife |

Befehle **niemals mit `retain`** senden – der Client verwirft sie.
Ausführlich: **[docs/MQTT.md](docs/MQTT.md)**.

## Fehlersuche

| Symptom | Ursache / Abhilfe |
|---|---|
| Beamer bleibt schwarz, `state` meldet `waiting` | Normalzustand nach dem Start. `{"action": "init"}` senden. |
| Befehle bewirken nichts, Log: „wartet noch auf init“ | Ebenso – erst `init` senden. |
| Log: „retained Befehl … verworfen“ | Der Befehl wurde mit `retain=true` gesendet. Ohne Retain senden und das hängende Retain-Topic löschen: `mosquitto_pub … -t halloween/beamer1/cmd -r -n` |
| `Not authorized` im Log | Passwort in `/etc/halloween/mqtt.pass` prüfen. Die Datei muss das Passwort **exakt** enthalten, ohne Anführungszeichen. |
| `AttributeError: … CallbackAPIVersion` | paho-mqtt ist älter als 2.0. Neuere Version installieren: `sudo apt install -y python3-pip && sudo pip install --break-system-packages "paho-mqtt>=2.0"` |
| Kein Bild, Dienst läuft | Ist der Benutzer in den Gruppen `video` und `render` (`id`)? Nach dem Installer ist ein Reboot nötig. |
| Bild ruckelt | `--hwdec=no` und `--profile=fast` müssen in `mpv.args` stehen (siehe [docs/TECHNIK.md](docs/TECHNIK.md)). Material über 1080p30 überfordert einen Pi 3. |
| Kein Ton | `mpv --audio-device=help` ausführen und das passende Gerät in `mpv.args` eintragen. |
| Video wird nicht gefunden | `--list-videos` zeigt alle gültigen Namen. Mehrdeutige Kurznamen lehnt der Client bewusst ab (Log: „ist mehrdeutig“). |
| Start dauert ca. 20 s | Normal beim Kaltstart auf einem Pi 3. |
| Idle-Clip fehlt beim Start | `python3 ~/halloween/scripts/generate_idle_clip.py` erzeugt `media/idle_black.mp4`. |

## Projektstruktur

```
config/settings.example.yaml     Vorlage für config/settings.yaml
deploy/install_pi.sh             Installer für den Raspberry Pi
deploy/halloween-beamer.service  systemd-Dienst
scripts/mqtt_cmd.py              MQTT-Befehle von der Kommandozeile senden
scripts/generate_idle_clip.py    schwarzen Idle-Clip per ffmpeg erzeugen
src/halloween/
├── client/main.py               Einstiegspunkt des Beamer-Clients
├── player/player.py             Zustandsmaschine waiting → idle → playing
└── core/                        mpv-Steuerung, MQTT, Medienkatalog, Einstellungen
docs/MQTT.md                     Steuerung per MQTT
docs/TECHNIK.md                  Architektur und Hardware-Erkenntnisse
```

## Bekannte Grenzen

- Übergänge sind harte Schnitte, Überblendungen gibt es nicht. Mit einem
  schwarzen Idle-Clip fällt das nicht auf.
- Ein Pi 3 schafft 1080p30 (ca. 280 % von 400 % CPU) – nicht aber 4K oder
  höhere Bildraten.
- Der synchrone Start per `at` hängt an der Systemuhr. Für mehrere Beamer im
  Gleichtakt sollten alle per NTP synchronisiert sein (unter Raspberry Pi
  OS Standard).
- Es gibt noch keine automatisierten Tests.

## Lizenz

[MIT](LICENSE). Videomaterial ist nicht enthalten und unterliegt den
Lizenzbedingungen des jeweiligen Anbieters.
