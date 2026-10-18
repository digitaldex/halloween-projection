# Technischer Hintergrund

## Architektur

```
src/halloween/
├── core/
│   ├── media_library.py  Videokatalog: Dateiname/Slug/Alias -> Pfad, Laufzeiten
│   ├── mpv_backend.py    MpvBackend-Interface + IpcMpvBackend (eigener mpv-Prozess, JSON-IPC)
│   ├── mpv_ipc.py        JSON-IPC-Client (Unix-Socket / Windows Named Pipe, 2 Verbindungen)
│   ├── mpv_process.py    mpv-Subprozess (idle=yes, keep-open=yes, keine Flackerquellen)
│   ├── mqtt_client.py    paho-Wrapper, mehrere Handler pro Topic, Last Will
│   ├── mqtt_schema.py    Payload-Validierung (PlayCommand/StateMessage)
│   └── settings.py       settings.yaml laden, ${ENV}-Platzhalter, Passwortdatei
├── player/
│   └── player.py         BeamerPlayer: MQTT -> Backend, Zustände waiting -> idle -> playing
└── client/
    └── main.py           Einstiegspunkt: Vollbild per KMS/DRM, systemd-Dienst,
                          meldet state/availability/videos retained
```

`BeamerPlayer` kennt nur das `MpvBackend`-Interface und hat keine
GUI-Abhängigkeiten. Statuswechsel gibt er zusätzlich über einfache
Callbacks (`add_state_listener`) weiter, sodass sich weitere Frontends
anhängen lassen.

## Nahtlose Übergänge

Zwischen Idle-Schleife und Effektclip ist kein Schwarz zu sehen. Zwei Dinge
sorgen dafür:

1. mpv läuft durchgehend als **derselbe Prozess** (`--idle=yes`); gewechselt
   wird nur die Datei per `loadfile`. Beim Umschalten zeigt mpv das alte
   Bild weiter, bis der erste Frame der neuen Datei dekodiert ist.
2. **`--keep-open=yes`** lässt mpv am Dateiende auf dem letzten Frame
   stehen, statt die Datei zu entladen und auf seinen schwarzen
   Hintergrund zurückzufallen.

Ohne Punkt 2 blitzte es beim Rücksprung Clip → Idle kurz schwarz: mit
`--keep-open=no` hatte mpv die Datei am Clip-Ende bereits entladen, bevor
der Client das Event überhaupt bekam – und die Kette Event → IPC →
`loadfile` → Demux → Decode dauert auf einem Pi 3 einige hundert
Millisekunden.

Der Preis von `--keep-open=yes`: mpv meldet am natürlichen Clip-Ende kein
`end-file`/`eof` mehr. Den Rücksprung löst stattdessen die Property
`eof-reached` aus, die `IpcMpvBackend` abonniert und in dasselbe
eof-Signal übersetzt, das der Player ohnehin auswertet.

**Fallstrick beim Abonnieren:** mpv führt beobachtete Properties *pro
Client-Verbindung*. Der IPC-Client hält zwei Verbindungen (eine für
Kommandos, eine für Events); `observe_property` muss auf der
**Event-Verbindung** abgesetzt werden. Auf der Kommando-Verbindung
abonniert, kommen die `property-change`-Meldungen im Antwortstrom an und
werden dort verworfen – der Beamer bliebe dann am Clip-Ende auf dem
letzten Frame stehen.

Nachgemessen durch Abtasten des mpv-Zustands im 25-ms-Raster über den
Übergang: kein einziger Abtastwert ohne geladene Datei.

## Hardware-Eigenheiten des Raspberry Pi 3

Die Vorgaben in `config/settings.example.yaml` (`mpv.args`) beruhen auf
diesen Messungen.

**Software-Decoding ist schneller als der Hardware-Decoder.** Mit einem
1080p30-Clip bei 9 Mbit/s, 60 s Laufzeit:

| Einstellung | verworfene Frames |
|---|---|
| `--hwdec=no --profile=fast` | **0** |
| `--hwdec=no` (ohne `--profile=fast`) | 50 |
| `--hwdec=v4l2m2m-copy` | 1235 |

Der v4l2m2m-Wrapper hält auf dieser Hardware nicht mit, die vier A53-Kerne
schaffen 1080p30 dagegen problemlos (ca. 280 % von 400 % CPU).
`--profile=fast` schaltet die teuren Scaler- und Dither-Stufen ab und macht
den Rest aus. Neuere Pi-Modelle haben mehr Reserve; die Einstellungen
schaden dort aber nicht.

**Überblendungen sind auf dem Pi 3 nicht möglich.** Ein früherer Versuch
blendete über mpvs Video-Equalizer (`brightness`). Der VideoCore IV kann
keine High-Bit-Depth-FBOs, deshalb schaltet `vo=gpu` in den „dumb mode“
und blittet das Bild an der Shader-Pipeline vorbei – `brightness` bleibt
wirkungslos (per Screenshot-Vergleich bei 0 / −50 / −100 nachgewiesen).
`--gpu-dumb-mode=no` hilft nicht, `vo=gpu-next` startet auf dieser GPU gar
nicht erst.

## Passwort in eigener Datei

Das MQTT-Passwort steht standardmäßig in `/etc/halloween/mqtt.pass`
(Besitzer root, Gruppe des Dienstbenutzers, Modus 640) und nicht in der
`settings.yaml`, die im Home-Verzeichnis liegt. Der Client liest die Datei
byte-genau selbst ein. Der Umweg über eine Umgebungsvariable per systemd
`EnvironmentFile` hat sich als unzuverlässig erwiesen: dessen eigene
Quoting-Regeln verändern Passwörter mit Sonderzeichen.

## systemd-Dienst

`deploy/halloween-beamer.service` startet den Client auf `/dev/tty1`
(`StandardInput=tty-force`), damit mpv die Konsole übernehmen kann und kein
Textcursor durchs Bild blinkt. `Restart=always` mit
`StartLimitIntervalSec=0` sorgt dafür, dass der Dienst auch nach mehreren
Fehlstarts in Folge weiter neu startet – eine Halloween-Nacht läuft
unbeaufsichtigt. `KillMode=control-group` beendet beim Stoppen auch den
mpv-Kindprozess, damit die Grafikkarte wieder frei wird.
