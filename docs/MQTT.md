# Steuerung per MQTT

Jeder Beamer-Client hört auf zwei Topics und meldet seinen Zustand auf drei
weiteren. In den Beispielen ist das Basis-Topic `halloween` (einstellbar
über `mqtt.base_topic`) und die Beamer-ID `beamer1` (`client.beamer_id`).

## Topics

| Topic | Richtung | Retained | Inhalt |
|---|---|---|---|
| `halloween/beamer1/cmd` | → Beamer | **nein** | Befehl an genau diesen Beamer |
| `halloween/all/cmd` | → alle Beamer | **nein** | Befehl an alle Beamer gleichzeitig |
| `halloween/beamer1/state` | ← Beamer | ja | aktueller Zustand |
| `halloween/beamer1/availability` | ← Beamer | ja | `online` / `offline` |
| `halloween/beamer1/videos` | ← Beamer | ja | Katalog aller abspielbaren Clips |

## Ablauf: vom Einschalten bis zum Video

```
  Einschalten / Dienststart
    │
    ▼
  waiting   mpv läuft und zeigt Schwarz. Der Client ist mit dem Broker
    │       verbunden und meldet seinen Katalog – nimmt aber NUR
    │       {"action": "init"} an. Alle anderen Befehle landen im Log.
    │  ◄─── {"action": "init"}
    ▼
  idle      Der Idle-Clip (media.idle_clip) läuft in Endlosschleife.
    │  ◄─── {"video": "raven"}
    ▼
  playing   Der Effektclip läuft.
    │
    └────── Clip zu Ende oder {"action": "stop"}  ──►  zurück zu idle
```

**Warum `init`?** Fällt nachts der Strom aus, soll der Beamer nach dem
Neustart nicht von selbst wieder loslegen, sondern warten, bis die
Steuerung (z. B. eine Home-Assistant-Automation zum Showbeginn) ihn scharf
schaltet. Ein erneutes `init` im laufenden Betrieb ist erlaubt und setzt
den Beamer sofort auf die Idle-Schleife zurück – praktisch als Notbremse.

Befehle, die vor dem `init` eintreffen, werden bewusst **verworfen und
nicht nachgeholt**.

## Befehle

Alle Befehle sind JSON-Objekte auf `…/<beamer_id>/cmd` oder `…/all/cmd`.

| Feld | Typ | Pflicht | Standard | Bedeutung |
|---|---|---|---|---|
| `action` | `"init"`, `"play"`, `"stop"` | nein | `"play"` | `init` schaltet den Beamer scharf, `play` spielt einen Clip, `stop` kehrt sofort zur Idle-Schleife zurück |
| `video` | String | bei `play` | – | Clip: Dateiname, Kurzname oder Slug (siehe [Video-Namen](#video-namen)) |
| `start` | Zahl ≥ 0 | nein | `0` | Startposition im Clip in Sekunden |
| `loop` | Bool | nein | `false` | Clip endlos wiederholen statt danach zur Idle-Schleife zurückzukehren |
| `volume` | 0–100 | nein | – | Lautstärke; gilt auch für alle folgenden Clips, bis sie erneut gesetzt wird |
| `at` | Unix-Zeitstempel | nein | – | absoluter Startzeitpunkt, z. B. für den synchronen Start mehrerer Beamer |

Beispiele:

```json
{"action": "init"}
{"video": "raven"}
{"video": "raven", "loop": true}
{"video": "raven", "start": 12, "volume": 80}
{"video": "raven", "at": 1761930000}
{"action": "stop"}
```

Ein neuer `play`-Befehl unterbricht einen laufenden Clip sofort. Ein noch
nicht gestarteter, per `at` geplanter Clip wird durch jeden neuen Befehl
abgebrochen.

Ungültige Payloads (kein JSON, unbekannte `action`, fehlendes `video` …)
werden mit einer Meldung im Log verworfen – der Client stürzt nicht ab.

### Niemals mit `retain` senden

Ein Befehl ist ein Ereignis, kein Zustand. Läge er retained auf dem Topic,
würde der Broker ihn bei jedem Verbindungsaufbau erneut ausliefern – ein
WLAN-Aussetzer mitten in der Nacht würde dann ungefragt den letzten Clip
starten. Der Client erkennt retained Befehle und verwirft sie mit einer
Warnung im Log.

Wurde versehentlich doch ein Befehl retained gesendet, lässt er sich mit
einer leeren Nachricht löschen:

```bash
mosquitto_pub -h <broker> -u <user> -P '<passwort>' -t halloween/beamer1/cmd -r -n
```

### Synchroner Start mehrerer Beamer

Für einen gemeinsamen Start einen Befehl mit einem Zeitpunkt in der nahen
Zukunft an `halloween/all/cmd` schicken:

```json
{"video": "raven", "at": 1761930000}
```

Jeder Beamer lädt den Clip erst zum angegebenen Zeitpunkt. Liegt `at`
bereits in der Vergangenheit (z. B. weil die Nachricht verspätet ankam),
startet der Clip sofort und spult um die Verspätung vor, sodass alle
Beamer an derselben Stelle stehen. Voraussetzung: die Uhren aller Pis sind
per NTP synchron (unter Raspberry Pi OS Standard). Zwei bis drei Sekunden
Vorlauf haben sich bewährt.

## Video-Namen

Dateinamen von gekauftem Material taugen schlecht als Payload
(`"JOL2_The Raven_Trio_Pumpkin.mp4"` – Leerzeichen, Großbuchstaben). Der
Client akzeptiert deshalb mehrere Schreibweisen, Groß-/Kleinschreibung ist
egal:

```
"JOL2_The Raven_Trio_Pumpkin.mp4"   der Dateiname
"JOL2_The Raven_Trio_Pumpkin"       ohne Endung
"jol2-the-raven-trio-pumpkin"       automatischer Slug
"raven"                             Kurzname aus media.aliases
```

Passt nichts davon exakt, wird noch ein **eindeutiger** Anfang oder
Teilstring akzeptiert (`"witching"`). Mehrdeutige Namen lehnt der Client
bewusst ab – in einer laufenden Show ist ein falsches Video schlimmer als
gar keins.

Alle gültigen Namen zeigt auf dem Pi:

```bash
cd ~/halloween && PYTHONPATH=src python3 -m halloween.client.main --list-videos
```

## Statusmeldungen

Alle Statusmeldungen sind retained: wer das Topic abonniert, bekommt sofort
den aktuellen Stand. Nach jedem (Wieder-)Verbinden mit dem Broker setzt der
Client sie neu.

### `state`

```json
{"state": "playing", "video": "JOL2_The Raven_Trio_Pumpkin.mp4", "position": 0.0, "ts": 1761930000.4}
```

| Feld | Bedeutung |
|---|---|
| `state` | `waiting` (wartet auf `init`), `idle` (Idle-Schleife) oder `playing` (Effektclip) |
| `video` | Dateiname des laufenden Effektclips, sonst `null` |
| `ts` | Unix-Zeitpunkt der Meldung |

Gemeldet wird bei jedem Zustandswechsel, nicht fortlaufend.

### `availability`

`online` nach dem Verbinden, `offline` beim sauberen Beenden. Fällt der Pi
hart aus (Strom weg, WLAN-Abriss), setzt der Broker über den Last Will
selbst `offline`.

### `videos`

```json
{
  "count": 2,
  "idle": "idle_black.mp4",
  "videos": [
    {"id": "black", "file": "idle_black.mp4", "slug": "idle-black", "aliases": ["black"], "duration": 10.0},
    {"id": "raven", "file": "JOL2_The Raven_Trio_Pumpkin.mp4", "slug": "jol2-the-raven-trio-pumpkin", "aliases": ["raven"], "duration": 94.5}
  ]
}
```

`id` ist der bevorzugte Name (erster Kurzname, sonst der Slug), `duration`
die Laufzeit in Sekunden. Damit lässt sich z. B. in Home Assistant eine
Auswahlliste bauen, ohne die Dateinamen doppelt zu pflegen.

## Beispiele

### mosquitto_pub / mosquitto_sub

Paket `mosquitto-clients` (Linux) bzw. Mosquitto für Windows.

```bash
B="-h <broker> -u <user> -P <passwort>"

mosquitto_pub $B -t halloween/beamer1/cmd -m '{"action": "init"}'
mosquitto_pub $B -t halloween/beamer1/cmd -m '{"video": "raven"}'
mosquitto_pub $B -t halloween/beamer1/cmd -m '{"video": "raven", "loop": true}'
mosquitto_pub $B -t halloween/beamer1/cmd -m '{"action": "stop"}'

# alle Beamer synchron in 3 Sekunden
mosquitto_pub $B -t halloween/all/cmd -m "{\"video\": \"raven\", \"at\": $(( $(date +%s) + 3 ))}"

# Status aller Beamer beobachten
mosquitto_sub $B -t 'halloween/#' -v
```

In der Windows-PowerShell müssen Anführungszeichen im JSON verdoppelt
werden:

```powershell
mosquitto_pub -h <broker> -u <user> -P <passwort> -t halloween/beamer1/cmd -m "{""video"": ""raven""}"
```

### Mitgeliefertes Python-Skript

`scripts/mqtt_cmd.py` braucht keine mosquitto-Tools, nur Python 3.10+ und
die Abhängigkeiten:

```bash
pip install -r requirements.txt
```

Broker und Zugangsdaten liest es aus `config/settings.yaml`, alternativ
per Option:

```bash
python scripts/mqtt_cmd.py --host 192.168.1.10 --username halloween --password geheim init beamer1
```

```bash
python scripts/mqtt_cmd.py init beamer1                     # scharf schalten
python scripts/mqtt_cmd.py init all                         # alle Beamer scharf schalten
python scripts/mqtt_cmd.py play beamer1 raven               # Clip einmal abspielen
python scripts/mqtt_cmd.py play beamer1 raven --loop        # endlos
python scripts/mqtt_cmd.py play beamer1 raven --start 12    # ab Sekunde 12
python scripts/mqtt_cmd.py play beamer1 raven --volume 80   # mit Lautstärke
python scripts/mqtt_cmd.py play all raven --at +5           # alle synchron in 5 s
python scripts/mqtt_cmd.py idle beamer1                     # zurück zur Idle-Schleife
python scripts/mqtt_cmd.py state                            # Status aller Beamer
```

### Home Assistant

Voraussetzung ist die eingerichtete MQTT-Integration. Befehle werden mit
der Aktion `mqtt.publish` gesendet.

**Skript: Clip abspielen**

```yaml
alias: Beamer 1 – Raven
sequence:
  - action: mqtt.publish
    data:
      topic: halloween/beamer1/cmd
      payload: '{"video": "raven"}'
```

**Automation: Show zu Beginn der Dämmerung scharf schalten**

```yaml
alias: Halloween – Beamer scharf schalten
triggers:
  - trigger: sun
    event: sunset
actions:
  - action: mqtt.publish
    data:
      topic: halloween/all/cmd
      payload: '{"action": "init"}'
```

**Automation: zufälliger Clip, wenn jemand vor dem Haus steht**

```yaml
alias: Halloween – Clip bei Bewegung
mode: single
triggers:
  - trigger: state
    entity_id: binary_sensor.bewegung_einfahrt
    to: "on"
conditions:
  - condition: state
    entity_id: sensor.beamer_1_status
    state: idle
actions:
  - action: mqtt.publish
    data:
      topic: halloween/beamer1/cmd
      payload: >-
        {"video": "{{ ['raven', 'welcome', 'goblins'] | random }}"}
```

Die Bedingung verhindert, dass ein laufender Clip unterbrochen wird.

**Alle Beamer synchron in 3 Sekunden starten**

```yaml
action: mqtt.publish
data:
  topic: halloween/all/cmd
  payload: >-
    {"video": "raven", "at": {{ (now().timestamp() + 3) | int }}}
```

**Status als Sensor**

MQTT-Sensoren ohne Auto-Discovery werden in der `configuration.yaml`
angelegt:

```yaml
mqtt:
  sensor:
    - name: "Beamer 1 Status"
      unique_id: halloween_beamer1_state
      state_topic: halloween/beamer1/state
      value_template: "{{ value_json.state }}"
      json_attributes_topic: halloween/beamer1/state
      availability_topic: halloween/beamer1/availability
```

Der Sensor zeigt `waiting`, `idle` oder `playing`, das Attribut `video` den
laufenden Clip. Ist der Pi nicht erreichbar, wird der Sensor
„nicht verfügbar“.
