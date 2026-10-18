"""Katalog der abspielbaren Videodateien.

Das MQTT-Feld `video` soll sich bequem tippen und in Home-Assistant-
Automationen einsetzen lassen. Die Dateinamen aus dem AtmosFX-Material
eignen sich dafuer schlecht ("JOL2_The Raven_Trio_Pumpkin_deutsch.mp4" -
Leerzeichen, Versalien, Rauschen). Diese Klasse legt darum ueber das
Media-Verzeichnis eine Namensschicht:

    "JOL2_The Raven_Trio_Pumpkin_deutsch.mp4"   der echte Dateiname
    "JOL2_The Raven_Trio_Pumpkin_deutsch"       ohne Endung
    "jol2-the-raven-trio-pumpkin-deutsch"       automatischer Slug
    "raven"                                     Alias aus settings.yaml

Alle vier treffen dieselbe Datei; die Suche ignoriert Gross-/Kleinschreibung.
Trifft nichts davon, wird als Bequemlichkeit noch ein *eindeutiger*
Praefix- bzw. Teilstring-Treffer akzeptiert ("witching" -> genau eine Datei).
Mehrdeutiges schlaegt bewusst fehl, statt zu raten - in einer laufenden Show
ist ein falsches Video schlimmer als eine Fehlermeldung im Log.

Ausserdem liefert `catalog()` die Liste fuer das retained Topic
`<base>/<beamer_id>/videos`, damit ein Frontend (Home Assistant) die
Auswahl anzeigen kann, ohne das Media-Verzeichnis zu kennen.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_EXTENSIONS = (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")

# Dateiname -> Laufzeit. ffprobe ueber ~20 Dateien kostet auf einem Pi 3
# spuerbar Zeit, das Ergebnis aendert sich aber nur, wenn sich die Datei
# aendert - daher der Cache neben dem Material.
_DURATION_CACHE = ".durations.json"


def slugify(value: str) -> str:
    """Macht aus "JOL2_The Raven_Trio" den Slug "jol2-the-raven-trio"."""

    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


@dataclass
class MediaEntry:
    """Eine Videodatei mit allen Namen, unter denen sie ansprechbar ist."""

    filename: str
    path: Path
    slug: str
    aliases: list[str] = field(default_factory=list)
    duration: Optional[float] = None

    @property
    def id(self) -> str:
        """Bevorzugter Kurzname: der erste Alias, sonst der Slug."""

        return self.aliases[0] if self.aliases else self.slug

    def keys(self) -> list[str]:
        """Alle Schreibweisen, unter denen diese Datei gefunden werden soll."""

        stem = Path(self.filename).stem
        return [self.filename, stem, self.slug, slugify(self.filename), *self.aliases]


class MediaLibrary:
    """Index ueber das Media-Verzeichnis; loest MQTT-Video-IDs auf Pfade auf."""

    def __init__(
        self,
        media_dir: Path | str,
        aliases: Optional[dict[str, str]] = None,
        extensions: Iterable[str] = DEFAULT_EXTENSIONS,
        probe_durations: bool = True,
    ):
        self.media_dir = Path(media_dir).resolve()
        self._extensions = {ext.lower() for ext in extensions}
        # settings.yaml schreibt {alias: dateiname}; intern brauchen wir die
        # Gegenrichtung, und je Datei duerfen es mehrere Aliase sein.
        self._alias_map: dict[str, list[str]] = {}
        for alias, filename in (aliases or {}).items():
            self._alias_map.setdefault(str(filename), []).append(str(alias))
        self._probe_durations = probe_durations

        self._entries: list[MediaEntry] = []
        self._index: dict[str, MediaEntry] = {}

    # -- Aufbau ------------------------------------------------------------

    def scan(self) -> None:
        """Liest das Verzeichnis neu ein. Auch zur Laufzeit aufrufbar."""

        if not self.media_dir.is_dir():
            raise FileNotFoundError(f"Media-Verzeichnis fehlt: {self.media_dir}")

        durations = self._load_duration_cache()
        entries: list[MediaEntry] = []
        for path in sorted(self.media_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in self._extensions:
                continue
            entry = MediaEntry(
                filename=path.name,
                path=path,
                slug=slugify(path.stem),
                aliases=list(self._alias_map.get(path.name, ())),
            )
            entry.duration = self._duration_for(entry, durations)
            entries.append(entry)

        self._entries = entries
        self._index = self._build_index(entries)
        self._save_duration_cache(durations)
        logger.info(
            "Medienkatalog: %d Datei(en) in %s, %d Suchbegriff(e)",
            len(entries),
            self.media_dir,
            len(self._index),
        )

    @staticmethod
    def _build_index(entries: list[MediaEntry]) -> dict[str, MediaEntry]:
        """Baut die Nachschlagetabelle; mehrdeutige Begriffe fliegen raus.

        Zwei Dateien, die sich denselben Alias teilen, duerfen nicht dazu
        fuehren, dass zufaellig eine davon gewinnt - dann lieber beide nur
        noch unter ihren eindeutigen Namen erreichbar.
        """

        ambiguous: set[str] = set()
        index: dict[str, MediaEntry] = {}
        for entry in entries:
            for key in entry.keys():
                key = key.lower()
                if not key:
                    continue
                if key in index and index[key] is not entry:
                    ambiguous.add(key)
                index[key] = entry
        for key in ambiguous:
            logger.warning("Mehrdeutiger Video-Name %r - wird ignoriert", key)
            index.pop(key, None)
        return index

    # -- Laufzeiten ---------------------------------------------------------

    def _cache_path(self) -> Path:
        return self.media_dir / _DURATION_CACHE

    def _load_duration_cache(self) -> dict[str, Any]:
        try:
            with self._cache_path().open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_duration_cache(self, durations: dict[str, Any]) -> None:
        try:
            with self._cache_path().open("w", encoding="utf-8") as fh:
                json.dump(durations, fh, indent=1, sort_keys=True)
        except OSError:
            logger.debug("Laufzeit-Cache nicht schreibbar: %s", self._cache_path())

    def _duration_for(self, entry: MediaEntry, cache: dict[str, Any]) -> Optional[float]:
        """Laufzeit aus dem Cache oder per ffprobe; Groesse+mtime als Cache-Schluessel."""

        if not self._probe_durations:
            return None
        stat = entry.path.stat()
        stamp = [stat.st_size, int(stat.st_mtime)]
        cached = cache.get(entry.filename)
        if isinstance(cached, dict) and cached.get("stamp") == stamp:
            return cached.get("duration")

        duration = _ffprobe_duration(entry.path)
        cache[entry.filename] = {"stamp": stamp, "duration": duration}
        return duration

    # -- Abfrage ------------------------------------------------------------

    def entries(self) -> list[MediaEntry]:
        return list(self._entries)

    def resolve(self, key: str) -> Optional[Path]:
        """Findet die Datei zu einer Video-ID aus dem MQTT-Payload.

        Reihenfolge: exakter Treffer (case-insensitiv) -> eindeutiger
        Praefix -> eindeutiger Teilstring. Mehrdeutig oder unbekannt -> None.
        """

        needle = (key or "").strip().lower()
        if not needle:
            return None

        entry = self._index.get(needle)
        if entry is not None:
            return entry.path

        for match_fn in (str.startswith, str.__contains__):
            hits = {
                found.filename: found
                for indexed, found in self._index.items()
                if match_fn(indexed, needle)
            }
            if len(hits) == 1:
                found = next(iter(hits.values()))
                logger.info("Video-ID %r unscharf aufgeloest auf %s", key, found.filename)
                return found.path
            if len(hits) > 1:
                logger.error(
                    "Video-ID %r ist mehrdeutig (%s) - kein Treffer",
                    key,
                    ", ".join(sorted(hits)),
                )
                return None
        return None

    def catalog(self, idle_clip: Optional[str] = None) -> dict[str, Any]:
        """Inhalt fuer das retained Topic <base>/<beamer_id>/videos."""

        return {
            "count": len(self._entries),
            "idle": idle_clip,
            "videos": [
                {
                    "id": entry.id,
                    "file": entry.filename,
                    "slug": entry.slug,
                    "aliases": entry.aliases,
                    "duration": round(entry.duration, 2) if entry.duration else None,
                }
                for entry in self._entries
            ],
        }


def _ffprobe_duration(path: Path) -> Optional[float]:
    """Laufzeit in Sekunden; None, wenn ffprobe fehlt oder nichts liefert."""

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None
