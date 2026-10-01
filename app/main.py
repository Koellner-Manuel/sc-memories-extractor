#!/usr/bin/env python3
"""
sc-extractor (app/main.py) - Snapchat-Memories-Export verlustfrei sichern.

Liest memories_history.json aus dem Snapchat "My Data"-Export, besorgt jede
Mediendatei (lokal aus memories/ oder per Download-Link), schreibt die
Original-Metadaten (Aufnahmedatum, GPS) hinein und erzeugt einen
Verifikations-Report.

Das Original wird NICHT neu kodiert. exiftool tauscht bei JPG nur den
EXIF-Block und bei MP4 nur die Container-Atome aus.

Wenn zu einem Memory ein Overlay (Caption, Zeichnung, Sticker, Filter)
existiert, wird zusaetzlich eine zweite Datei "<name>_overlay.<ext>" mit
eingebranntem Overlay erzeugt. Diese Variante ist zwangslaeufig neu kodiert -
das Original daneben bleibt bitidentisch.

Die Overlay-Quelle selbst (transparentes PNG) wird als "<name>_overlay_src.png"
mitgesichert - sie gehoert zu den Originaldaten und erlaubt, die
_overlay-Variante jederzeit neu zu erzeugen.

Dateinamen enthalten die Media-ID ("..._<id>.jpg"). Dadurch erkennt ein
erneuter Lauf bereits gesicherte Dateien auch dann, wenn sich Ortszeit oder
Ordner geaendert haben (z.B. anderes --tz-window), und benennt sie um statt
Duplikate anzulegen.

Voraussetzungen und Start: siehe README.md. Unter Windows bringt der Ordner
alle Tools portabel mit (1_setup.cmd), gestartet wird ueber 2_check.cmd /
3_run.cmd. Direkter Aufruf:
    python app/main.py <export-ordner> --out <ziel> [--verify-only] ...

<export-ordner> enthaelt den entpackten Snapchat-Export. Alle Ordner namens
"memories" darunter werden durchsucht, auch die aus Folge-ZIPs (-1, -2, ...).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    sys.exit("Fehlt: pip install requests")

try:
    from PIL import Image
except ImportError:
    Image = None  # nur noetig, wenn Overlays verarbeitet werden

# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

DEFAULT_TZ = "Europe/Vienna"
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus"}
IGNORE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
GEO_RE = re.compile(r"(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)")
ZIP_MAGIC = b"PK\x03\x04"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Ausgabename: <datum>_<zeit>_<id>[_overlay|_overlay_src|_joined].<ext>
OUT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_([0-9a-z]+(?:-\d+)?)"
    r"(_overlay_src|_overlay|_joined|_extra\d+)?(\.[0-9a-z]+)$", re.I)

# Halbfertige Dateien tragen dieses Praefix und werden erst nach Abschluss
# umbenannt. Ein Abbruch hinterlaesst so nie eine scheinbar fertige Datei.
TMP_PREFIX = "~tmp_"


# --------------------------------------------------------------------------
# Datenmodell
# --------------------------------------------------------------------------

@dataclass
class Memory:
    index: int
    taken_utc: datetime
    media_type: str                       # "image" | "video"
    download_url: str | None = None
    media_id: str | None = None           # "mid" aus dem Download-Link
    key: str = ""                         # stabile Kurz-ID fuer Dateinamen
    lat: float | None = None
    lon: float | None = None
    tz_name: str | None = None            # ermittelte Zeitzone der Aufnahme
    tz_source: str = "default"            # gps | nachbar | default
    source: Path | None = None            # Originaldatei im Export
    match: str = ""                       # id | datum | "" (= Download)
    local_overlay: Path | None = None     # Overlay-PNG im Export
    local_extras: list[Path] = field(default_factory=list)   # z.B. Audio im Export
    extras: list[Path] = field(default_factory=list)         # gesicherte _extraN
    overlay_source: Path | None = None    # gesichertes _overlay_src im Ziel
    target: Path | None = None            # geschriebenes Original
    overlay_target: Path | None = None    # erzeugte _overlay-Variante
    joined_target: Path | None = None     # zusammengefuegter Multi-Snap
    status: str = "pending"
    overlay_status: str = "-"
    join_status: str = "-"                # ok | fehler | teil | -
    group_id: int | None = None           # Multi-Snap-Gruppe
    group_pos: int = 0                    # Position 1..n in der Gruppe
    group_size: int = 0
    duration: float | None = None         # Sekunden, via ffprobe
    fingerprint: tuple | None = None      # Stream-Parameter fuer Vergleich
    note: str = ""
    sha256: str = ""

    @property
    def is_video(self) -> bool:
        return self.media_type == "video"

    def tz(self, fallback: str) -> ZoneInfo:
        try:
            return ZoneInfo(self.tz_name or fallback)
        except Exception:                          # noqa: BLE001
            return ZoneInfo(fallback)

    def local(self, fallback: str) -> datetime:
        """Aufnahmezeitpunkt in der Ortszeit des Aufnahmeorts."""
        return self.taken_utc.astimezone(self.tz(fallback))

    def dest_parts(self, out_dir: Path, fallback: str) -> tuple[Path, str]:
        """(Zielordner, Dateistamm ohne Endung) nach Ortszeit der Aufnahme."""
        local = self.local(fallback)
        base_dir = out_dir / local.strftime("%Y") / local.strftime("%Y-%m")
        return base_dir, f"{local.strftime('%Y-%m-%d_%H-%M-%S')}_{self.key}"


# --------------------------------------------------------------------------
# 1) JSON parsen
# --------------------------------------------------------------------------

def parse_json(path: Path) -> list[Memory]:
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)

    entries = None
    if isinstance(raw, dict):
        for key in ("Saved Media", "Memories", "saved_media"):
            if isinstance(raw.get(key), list):
                entries = raw[key]
                break
        if entries is None:
            for value in raw.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    entries = value
                    break
    elif isinstance(raw, list):
        entries = raw

    if not entries:
        raise SystemExit(f"Keine Memories-Liste in {path} gefunden.")

    out: list[Memory] = []
    for i, entry in enumerate(entries):
        lower = {k.lower().strip(): v for k, v in entry.items()}

        taken = _parse_date(lower.get("date") or lower.get("create time") or "")
        if taken is None:
            continue

        mtype = str(lower.get("media type", "image")).strip().lower()
        mtype = "video" if "video" in mtype else "image"

        lat, lon = _parse_geo(lower.get("location") or lower.get("location data") or "")
        link = lower.get("download link") or lower.get("download_link") or None
        mid = lower.get("media id") or lower.get("mid") or _mid_from_link(link)

        out.append(
            Memory(
                index=i,
                taken_utc=taken,
                media_type=mtype,
                download_url=link,
                media_id=str(mid).strip().lower() if mid else None,
                lat=lat,
                lon=lon,
            )
        )

    # Kurz-ID fuer die Dateinamen; doppelte IDs bekommen ein -2, -3, ...
    seen: dict[str, int] = {}
    for mem in out:
        base = re.sub(r"[^0-9a-z]", "", mem.media_id or "")[:12] or f"n{mem.index:05d}"
        seen[base] = seen.get(base, 0) + 1
        mem.key = base if seen[base] == 1 else f"{base}-{seen[base]}"
    return out


def _mid_from_link(link: str | None) -> str | None:
    """Die Media-ID steckt im Download-Link: ...memories?uid=..&mid=<UUID>&..."""
    if not link:
        return None
    query = parse_qs(urlparse(str(link)).query)
    for name in ("mid", "media_id"):
        if query.get(name):
            return query[name][0]
    return None


def _parse_date(value: str) -> datetime | None:
    m = DATE_RE.search(str(value))
    if not m:
        return None
    dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)      # Snapchat liefert UTC


def _parse_geo(value: str) -> tuple[float | None, float | None]:
    m = GEO_RE.search(str(value))
    if not m:
        return None, None
    lat, lon = float(m.group(1)), float(m.group(2))
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def resolve_timezones(memories: list[Memory], default_tz: str,
                      window_hours: float, enabled: bool) -> None:
    """Bestimmt je Memory die Zeitzone des Aufnahmeorts.

    1. Aus den GPS-Koordinaten (offline, via timezonefinder).
    2. Ohne GPS: von der zeitlich naechsten Aufnahme MIT GPS, sofern sie
       innerhalb von window_hours liegt. Deckt Snaps im selben Urlaub ab,
       bei denen Snapchat keine Position gespeichert hat.
    3. Sonst die Standardzeitzone.
    """
    finder = None
    if enabled:
        try:
            from timezonefinder import TimezoneFinder
            finder = TimezoneFinder(in_memory=True)
        except ImportError:
            print("Hinweis: timezonefinder fehlt (pip install timezonefinder) - "
                  f"es wird durchgaengig {default_tz} verwendet.")

    if finder is not None:
        cache: dict[tuple[float, float], str | None] = {}
        for mem in memories:
            if mem.lat is None:
                continue
            key = (round(mem.lat, 2), round(mem.lon, 2))   # ~1 km, spart Lookups
            if key not in cache:
                try:
                    cache[key] = finder.timezone_at(lat=mem.lat, lng=mem.lon)
                except Exception:                          # noqa: BLE001
                    cache[key] = None
            if cache[key]:
                mem.tz_name, mem.tz_source = cache[key], "gps"

    known = sorted((m for m in memories if m.tz_source == "gps"),
                   key=lambda x: x.taken_utc)
    if known and window_hours > 0:
        import bisect
        stamps = [k.taken_utc for k in known]
        limit = window_hours * 3600
        for mem in memories:
            if mem.tz_name:
                continue
            i = bisect.bisect_left(stamps, mem.taken_utc)
            best, best_delta = None, None
            for j in (i - 1, i):
                if 0 <= j < len(known):
                    delta = abs((known[j].taken_utc - mem.taken_utc).total_seconds())
                    if best_delta is None or delta < best_delta:
                        best, best_delta = known[j], delta
            if best is not None and best_delta is not None and best_delta <= limit:
                mem.tz_name, mem.tz_source = best.tz_name, "nachbar"

    for mem in memories:
        if not mem.tz_name:
            mem.tz_name, mem.tz_source = default_tz, "default"

    stats: dict[str, int] = {}
    zones: dict[str, int] = {}
    for mem in memories:
        stats[mem.tz_source] = stats.get(mem.tz_source, 0) + 1
        zones[mem.tz_name or default_tz] = zones.get(mem.tz_name or default_tz, 0) + 1
    print("Zeitzonen: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    top = sorted(zones.items(), key=lambda kv: -kv[1])[:6]
    print("  haeufigste: " + ", ".join(f"{k} ({v})" for k, v in top))


# --------------------------------------------------------------------------
# 2) Dateien im Export indizieren und den Memories zuordnen
# --------------------------------------------------------------------------

@dataclass
class LocalIndex:
    mains_by_uuid: dict[str, Path]
    overlays_by_uuid: dict[str, Path]
    overlays_by_key: dict[str, Path]
    by_date: dict[str, list[Path]]
    total_mains: int = 0
    total_overlays: int = 0
    # Dateien, die weder Bild noch Video sind (z.B. Audio). Mit UUID im Namen
    # werden sie dem Memory zugeordnet und als _extraN mitgesichert.
    extras_by_uuid: dict[str, list[Path]] = field(default_factory=dict)
    unknown: list[Path] = field(default_factory=list)          # alle davon
    unassigned_extras: list[Path] = field(default_factory=list)


def _norm_key(stem: str) -> str:
    """Normalisiert Dateinamen, damit Original und Overlay zusammenfinden.

    "2021-08-14_abc123-main"     -> "20210814abc123"
    "2021-08-14_abc123-overlay"  -> "20210814abc123"
    "media~abc123" / "overlay~abc123" -> "abc123"
    """
    s = stem.lower()
    for token in ("overlay", "media", "main"):
        s = s.replace(token, "")
    return re.sub(r"[^a-z0-9]", "", s)


def _file_uuid(stem: str) -> str | None:
    m = UUID_RE.search(stem)
    return m.group(0).lower() if m else None


def index_local_files(root: Path) -> LocalIndex:
    """Indiziert alle Ordner namens "memories" unter root (auch Folge-ZIPs)."""
    idx = LocalIndex({}, {}, {}, {})
    dirs = [d for d in root.rglob("*") if d.is_dir() and d.name.lower() == "memories"]
    files = sorted({
        p.resolve() for d in dirs for p in d.rglob("*")
        if p.is_file() and p.name.lower() not in IGNORE_NAMES
    })
    for p in files:
        uid = _file_uuid(p.stem)
        if p.suffix.lower() not in IMAGE_EXT | VIDEO_EXT:
            idx.unknown.append(p)
            if uid:
                idx.extras_by_uuid.setdefault(uid, []).append(p)
            continue
        if "overlay" in p.stem.lower():
            idx.total_overlays += 1
            if uid:
                idx.overlays_by_uuid.setdefault(uid, p)
            idx.overlays_by_key.setdefault(_norm_key(p.stem), p)
            continue
        idx.total_mains += 1
        if uid:
            idx.mains_by_uuid.setdefault(uid, p)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
        if m:
            idx.by_date.setdefault(m.group(1), []).append(p)
    return idx


def assign_local(memories: list[Memory], idx: LocalIndex) -> None:
    """Ordnet jedem Memory hoechstens eine lokale Datei zu, jede Datei hoechstens einmal.

    1. Ueber die Media-ID (mid aus dem Download-Link) = UUID im Dateinamen.
    2. Sonst ueber das Datum - aber nur, wenn an diesem Tag genau EIN Memory
       ohne Datei und genau EINE freie Datei desselben Typs uebrig sind, die
       nicht per ID zu einem anderen Memory gehoert. Lieber herunterladen als
       das falsche Foto zuordnen.
    """
    claimed = {m.media_id for m in memories if m.media_id}
    used: set[Path] = set()

    for mem in memories:
        p = idx.mains_by_uuid.get(mem.media_id) if mem.media_id else None
        if p is not None and p not in used:
            mem.source, mem.match = p, "id"
            used.add(p)

    pending: dict[tuple[str, bool], list[Memory]] = {}
    for mem in memories:
        if mem.source is None:
            pending.setdefault((mem.taken_utc.strftime("%Y-%m-%d"), mem.is_video), []).append(mem)
    for (day, is_video), mems in pending.items():
        cands = [p for p in idx.by_date.get(day, [])
                 if p not in used
                 and (p.suffix.lower() in VIDEO_EXT) == is_video
                 and _file_uuid(p.stem) not in claimed]
        if len(mems) == 1 and len(cands) == 1:
            mems[0].source, mems[0].match = cands[0], "datum"
            used.add(cands[0])

    for mem in memories:
        if mem.source is None:
            continue
        uid = mem.media_id if mem.match == "id" else _file_uuid(mem.source.stem)
        ov = idx.overlays_by_uuid.get(uid) if uid else None
        mem.local_overlay = ov or idx.overlays_by_key.get(_norm_key(mem.source.stem))

    # Unbekannte Dateitypen (z.B. Audio) mit passender UUID mitnehmen
    taken: set[Path] = set()
    for mem in memories:
        uid = mem.media_id if mem.match != "datum" else _file_uuid(mem.source.stem)
        if uid and uid in idx.extras_by_uuid:
            mem.local_extras = idx.extras_by_uuid[uid]
            taken.update(mem.local_extras)
    idx.unassigned_extras = [p for p in idx.unknown if p not in taken]


def print_unknown(memories: list[Memory], idx: LocalIndex) -> None:
    """Warnt vor Dateien im Export, die weder Bild noch Video sind."""
    if not idx.unknown:
        return
    assigned = [p for m in memories for p in m.local_extras]
    print(f"\nACHTUNG: {len(idx.unknown)} Dateien im Export sind weder Bild noch Video:")
    print(f"  {describe_unknown(idx.unknown)}")
    if assigned:
        print(f"  {len(assigned)} davon gehoeren per ID zu einem Memory und werden "
              "als _extraN mitgesichert.")
    if idx.unassigned_extras:
        print(f"  {len(idx.unassigned_extras)} ohne Zuordnung - werden NICHT gesichert:")
        print(f"  {describe_unknown(idx.unassigned_extras)}")


def describe_unknown(paths: list[Path], limit: int = 5) -> str:
    """'3x .m4a, 1x .txt  (z.B. a.m4a, b.txt)'"""
    counts: dict[str, int] = {}
    for p in paths:
        counts[p.suffix.lower() or "(ohne)"] = counts.get(p.suffix.lower() or "(ohne)", 0) + 1
    kinds = ", ".join(f"{n}x {ext}" for ext, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return f"{kinds}  (z.B. {', '.join(p.name for p in paths[:limit])})"


# --------------------------------------------------------------------------
# 2b) Zieldateien: atomar schreiben, bestehende wiederfinden
# --------------------------------------------------------------------------

def _tmp(path: Path) -> Path:
    return path.with_name(TMP_PREFIX + path.name)


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = _tmp(dst)
    shutil.copy2(src, tmp)                      # Bytes 1:1
    os.replace(tmp, dst)


def _atomic_write(dst: Path, data: bytes) -> None:
    tmp = _tmp(dst)
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _find_output(base_dir: Path, stem: str, variant: str = "") -> Path | None:
    """Fertige Zieldatei "<stem><variant>.<ext>" oder None."""
    for p in sorted(base_dir.glob(f"{stem}{variant}.*")):
        if not p.name.startswith(TMP_PREFIX):
            return p
    return None


def clean_temp_files(out_dir: Path) -> int:
    """Entfernt Reste abgebrochener Laeufe (eigene und von exiftool)."""
    n = 0
    for p in list(out_dir.rglob(TMP_PREFIX + "*")) + list(out_dir.rglob("*_exiftool_tmp")):
        if p.is_file():
            p.unlink(missing_ok=True)
            n += 1
    return n


def relocate_existing(memories: list[Memory], out_dir: Path, fallback_tz: str) -> int:
    """Benennt bereits gesicherte Dateien um, deren Zielname sich geaendert hat.

    Der Name haengt von der Ortszeit ab. Aendert sich die ermittelte Zeitzone
    (anderes --tz / --tz-window), findet die Media-ID im Namen die alte Datei
    wieder - sie wird verschoben statt ein Duplikat anzulegen.
    """
    found: dict[str, list[Path]] = {}
    for p in out_dir.rglob("*"):
        if p.is_file() and not p.name.startswith(TMP_PREFIX):
            m = OUT_RE.match(p.name)
            if m:
                found.setdefault(m.group(1).lower(), []).append(p)

    moved = 0
    for mem in memories:
        base_dir, stem = mem.dest_parts(out_dir, fallback_tz)
        for p in found.get(mem.key, []):
            m = OUT_RE.match(p.name)
            new = base_dir / f"{stem}{m.group(2) or ''}{m.group(3)}"
            if new == p or new.exists():
                continue
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, new)
            moved += 1

    if moved:   # leer gewordene Jahres-/Monatsordner entfernen
        for d in sorted((d for d in out_dir.rglob("*") if d.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
    return moved


# --------------------------------------------------------------------------
# 3) Download: POST -> signierte URL -> GET. Antwort kann ZIP sein.
# --------------------------------------------------------------------------

def resolve_and_download(mem: Memory, session: requests.Session, dest: Path,
                         retries: int = 4) -> tuple[Path, Path | None]:
    """Gibt (original, overlay_or_None) zurueck.

    Memories mit Overlay liefert Snapchat als ZIP mit zwei Membern
    (media~... und overlay~...).
    """
    url = mem.download_url
    if not url:
        raise RuntimeError("weder lokale Datei noch Download-Link vorhanden")

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            if "app.snapchat.com" in url or "/dmd/" in url:
                r = session.post(url, timeout=60)
                r.raise_for_status()
                real = r.text.strip()
                if not real.startswith("http"):
                    raise RuntimeError(f"unerwartete Antwort: {real[:120]!r}")
            else:
                real = url

            tmp = _tmp(dest.with_suffix(".download"))
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with session.get(real, stream=True, timeout=180) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        fh.write(chunk)

            with tmp.open("rb") as fh:
                head = fh.read(16)

            if head[:4] == ZIP_MAGIC:
                result = _unpack_zip(tmp, dest, mem)
                tmp.unlink(missing_ok=True)
                return result

            final = dest.with_suffix(_sniff_ext(head) or (".mp4" if mem.is_video else ".jpg"))
            os.replace(tmp, final)
            return final, None

        except Exception as exc:                # noqa: BLE001
            last_err = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Download fehlgeschlagen: {last_err}")


def _sniff_ext(head: bytes) -> str | None:
    """Dateiendung anhand der ersten Bytes; None, wenn unbekannt."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return ".mp3"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc"):
            return ".heic"
        if brand in (b"M4A ", b"M4B "):
            return ".m4a"
        return ".mov" if brand == b"qt  " else ".mp4"
    return None


def _unpack_zip(archive: Path, dest: Path, mem: Memory) -> tuple[Path, Path | None]:
    """Entpackt ein Download-ZIP: Original, Overlay und ggf. weitere Dateien.

    Original ist das Bild/Video mit "main" im Namen (sonst das erste). Alles
    Weitere - z.B. eine Audiospur - wird als <name>_extraN.<ext> gesichert,
    statt das Original zu ueberschreiben.
    """
    with zipfile.ZipFile(archive) as zf:
        members = [(n, zf.read(n)) for n in zf.namelist() if not n.endswith("/")]

    def ext_of(name: str, data: bytes) -> str:
        ext = Path(name).suffix.lower()
        if ext in IMAGE_EXT | VIDEO_EXT | AUDIO_EXT:
            return ext
        return _sniff_ext(data[:16]) or ext or ".bin"

    typed = [(n, d, ext_of(n, d)) for n, d in members]
    overlays = [t for t in typed if "overlay" in t[0].lower()
                and t[2] in IMAGE_EXT | VIDEO_EXT]
    media = [t for t in typed if t not in overlays and t[2] in IMAGE_EXT | VIDEO_EXT]
    if not media:
        raise RuntimeError("ZIP enthielt keine Mediendatei: "
                           + ", ".join(n for n, _, _ in typed))
    main = next((t for t in media if "main" in t[0].lower()), media[0])

    base_out = dest.with_suffix(main[2])
    _atomic_write(base_out, main[1])
    overlay_out: Path | None = None
    if overlays:
        overlay_out = dest.with_name(dest.name + "_overlay_src" + overlays[0][2])
        _atomic_write(overlay_out, overlays[0][1])

    rest = [t for t in typed if t is not main and (not overlays or t is not overlays[0])]
    for n, (_, data, ext) in enumerate(rest, start=1):
        _atomic_write(dest.with_name(f"{dest.name}_extra{n}{ext}"), data)
    return base_out, overlay_out


# --------------------------------------------------------------------------
# 4) Overlay einbrennen (erzeugt eine ZWEITE Datei, Original bleibt)
# --------------------------------------------------------------------------

def _no_window() -> dict:
    """Unterdrueckt unter Windows das Konsolenfenster je ffmpeg-Aufruf."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
    return {}


def compose_image_overlay(base: Path, overlay: Path, out: Path, quality: int) -> None:
    if Image is None:
        raise RuntimeError("Pillow fehlt: pip install pillow")
    with Image.open(base) as bg_raw, Image.open(overlay) as ov_raw:
        bg = bg_raw.convert("RGBA")
        ov = ov_raw.convert("RGBA")
        if ov.size != bg.size:
            ov = ov.resize(bg.size, Image.LANCZOS)
        merged = Image.alpha_composite(bg, ov).convert("RGB")
        merged.save(out, "JPEG", quality=quality, subsampling=0, optimize=True)


def compose_video_overlay(base: Path, overlay: Path, out: Path, crf: int) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg fehlt")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(base), "-i", str(overlay),
        "-filter_complex",
        "[1:v][0:v]scale2ref=flags=lanczos[ov][bg];[bg][ov]overlay=0:0[out]",
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffmpeg-Fehler").strip()[:200])


# --------------------------------------------------------------------------
# 5) Metadaten schreiben (ein exiftool-Prozess fuer alle Dateien)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 4b) Multi-Snaps erkennen und verlustfrei zusammenfuegen
# --------------------------------------------------------------------------

def probe_video(path: Path) -> tuple[float | None, tuple | None]:
    """Liefert (Dauer, Stream-Fingerprint) via ffprobe."""
    if not shutil.which("ffprobe"):
        return None, None
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
    if proc.returncode != 0:
        return None, None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, None

    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None

    video = next((st for st in data.get("streams", [])
                  if st.get("codec_type") == "video"), None)
    audio = next((st for st in data.get("streams", [])
                  if st.get("codec_type") == "audio"), None)
    if video is None:
        return duration, None

    fingerprint = (
        video.get("codec_name"),
        video.get("width"),
        video.get("height"),
        video.get("pix_fmt"),
        video.get("r_frame_rate"),
        audio.get("codec_name") if audio else None,
        audio.get("sample_rate") if audio else None,
        audio.get("channels") if audio else None,
    )
    return duration, fingerprint


def group_multisnaps(memories: list[Memory], tolerance: float,
                     min_segment: float) -> list[list[Memory]]:
    """Kettet Video-Snaps zu Aufnahmesessions.

    Zwei aufeinanderfolgende Clips gehoeren zusammen, wenn
      - der zeitliche Abstand ungefaehr der Dauer des ersten entspricht,
      - der erste ein volles Segment ist (Snapchat schneidet bei ~10 s),
      - beide dieselben Stream-Parameter haben.
    """
    clips = sorted(
        (m for m in memories
         if m.status == "ok" and m.target and m.target.suffix.lower() in VIDEO_EXT
         and m.duration and m.fingerprint),
        key=lambda x: x.taken_utc,
    )

    groups: list[list[Memory]] = []
    current: list[Memory] = []
    for prev, nxt in zip(clips, clips[1:] + [None]):
        if not current:
            current = [prev]
        if nxt is None:
            groups.append(current)
            break
        gap = (nxt.taken_utc - prev.taken_utc).total_seconds()
        chains = (
            prev.fingerprint == nxt.fingerprint
            and prev.duration is not None
            and prev.duration >= min_segment          # voller Clip, nicht das Ende
            and abs(gap - prev.duration) <= tolerance
        )
        if chains:
            current.append(nxt)
        else:
            groups.append(current)
            current = []
    return [g for g in groups if len(g) > 1]


def join_group(group: list[Memory], out: Path) -> None:
    """Verbindet die Segmente per concat-Demuxer ohne Re-Encoding."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg fehlt")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as fh:
        for mem in group:
            escaped = str(mem.target.resolve()).replace("'", "'\\''")
            fh.write(f"file '{escaped}'\n")
        listfile = fh.name
    try:
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-fflags", "+genpts", "-f", "concat", "-safe", "0",
               "-i", listfile, "-c", "copy", "-movflags", "+faststart", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "ffmpeg-Fehler").strip()[:200])
    finally:
        os.unlink(listfile)


def _tag_lines(mem: Memory, path: Path, fallback_tz: str) -> list[str]:
    local = mem.local(fallback_tz)
    stamp = local.strftime("%Y:%m:%d %H:%M:%S")
    offset = local.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"
    utc = mem.taken_utc.strftime("%Y:%m:%d %H:%M:%S")
    is_video = path.suffix.lower() in VIDEO_EXT

    lines: list[str] = []
    if is_video:
        # QuickTime speichert UTC. Mit QuickTimeUTC=1 behandelt exiftool eine
        # Zeit OHNE Zone als Ortszeit des PCs und rechnet sie um - je nach
        # Zeitzone des Rechners falsch. Daher die Zone explizit mitgeben.
        utc += "+00:00"
        lines += [
            "-api", "QuickTimeUTC=1",
            f"-QuickTime:CreateDate={utc}",
            f"-QuickTime:ModifyDate={utc}",
            f"-TrackCreateDate={utc}",
            f"-TrackModifyDate={utc}",
            f"-MediaCreateDate={utc}",
            f"-MediaModifyDate={utc}",
            f"-Keys:CreationDate={stamp}{offset}",
        ]
        if mem.lat is not None:
            iso = f"{mem.lat:+.5f}{mem.lon:+010.5f}/"      # ISO 6709
            lines += [
                f"-Keys:GPSCoordinates={iso}",
                f"-UserData:GPSCoordinates={iso}",
            ]
    else:
        lines += [
            f"-EXIF:DateTimeOriginal={stamp}",
            f"-EXIF:CreateDate={stamp}",
            f"-EXIF:ModifyDate={stamp}",
            f"-EXIF:OffsetTimeOriginal={offset}",
            f"-EXIF:OffsetTimeDigitized={offset}",
            f"-XMP:DateCreated={stamp}",
        ]
        if mem.lat is not None:
            lines += [
                f"-EXIF:GPSLatitude={abs(mem.lat)}",
                f"-EXIF:GPSLatitudeRef={'N' if mem.lat >= 0 else 'S'}",
                f"-EXIF:GPSLongitude={abs(mem.lon)}",
                f"-EXIF:GPSLongitudeRef={'E' if mem.lon >= 0 else 'W'}",
            ]
    lines += ["-overwrite_original", "-m", str(path), "-execute"]
    return lines


def build_exiftool_args(memories: list[Memory], fallback_tz: str) -> str:
    lines: list[str] = []
    for mem in memories:
        if mem.status == "ok" and mem.target:
            lines += _tag_lines(mem, mem.target, fallback_tz)
        if mem.status == "ok" and mem.overlay_source:
            lines += _tag_lines(mem, mem.overlay_source, fallback_tz)
        if mem.overlay_status == "ok" and mem.overlay_target:
            lines += _tag_lines(mem, mem.overlay_target, fallback_tz)
        if mem.join_status == "ok" and mem.joined_target:
            lines += _tag_lines(mem, mem.joined_target, fallback_tz)
    if lines and lines[-1] == "-execute":
        lines.pop()          # sonst startet exiftool einen leeren Durchlauf
    return "\n".join(lines) + "\n"


def run_exiftool(argfile_text: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(argfile_text)
        argfile = fh.name
    try:
        proc = subprocess.run(["exiftool", "-charset", "filename=UTF8", "-@", argfile],
                              capture_output=True, text=True)
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        if tail:
            print("  exiftool:", " | ".join(tail))
    finally:
        os.unlink(argfile)


def set_filesystem_time(path: Path, when: datetime) -> None:
    if path.exists():
        ts = when.timestamp()
        os.utime(path, (ts, ts))


# --------------------------------------------------------------------------
# 6) Verifikation
# --------------------------------------------------------------------------

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_report(memories: list[Memory], out_dir: Path, do_hash: bool,
                 fallback_tz: str) -> None:
    report = out_dir / "_verification.csv"
    with report.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["index", "id", "datum_utc", "datum_lokal", "zeitzone", "tz_quelle",
                    "typ", "zuordnung", "status", "datei", "bytes", "sha256",
                    "overlay_status", "overlay_datei", "extras",
                    "gruppe", "join_status", "joined_datei", "dauer_s",
                    "gps", "hinweis"])
        for m in sorted(memories, key=lambda x: x.index):
            size = m.target.stat().st_size if m.target and m.target.exists() else 0
            if do_hash and m.target and m.target.exists() and not m.sha256:
                m.sha256 = sha256(m.target)
            w.writerow([
                m.index,
                m.key,
                m.taken_utc.strftime("%Y-%m-%d %H:%M:%S"),
                m.local(fallback_tz).strftime("%Y-%m-%d %H:%M:%S %z"),
                m.tz_name or fallback_tz,
                m.tz_source,
                m.media_type,
                m.match or "download",
                m.status,
                m.target.name if m.target else "",
                size,
                m.sha256,
                m.overlay_status,
                m.overlay_target.name if m.overlay_target else "",
                " ".join(p.name for p in m.extras),
                f"G{m.group_id:04d} {m.group_pos}/{m.group_size}" if m.group_id else "",
                m.join_status,
                m.joined_target.name if m.joined_target else "",
                f"{m.duration:.2f}" if m.duration else "",
                f"{m.lat},{m.lon}" if m.lat is not None else "",
                m.note,
            ])
    print(f"\nReport: {report}")


# --------------------------------------------------------------------------
# Hauptablauf
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mydata", type=Path,
                    help="Primaerer Export-Ordner (enthaelt json/ und memories/)")
    ap.add_argument("--out", type=Path, default=Path("./memories_fixed"))
    ap.add_argument("--tz", default=DEFAULT_TZ,
                    help=f"Fallback-Zeitzone ohne Geodaten (Default {DEFAULT_TZ})")
    ap.add_argument("--no-geo-tz", action="store_true",
                    help="Zeitzone nicht aus den Geodaten bestimmen")
    ap.add_argument("--tz-window", type=float, default=12.0,
                    help="Stunden, in denen ein Snap ohne GPS die Zeitzone des "
                         "zeitlich naechsten Snaps mit GPS uebernimmt (0 = aus)")
    ap.add_argument("--workers", type=int, default=6, help="parallele Downloads")
    ap.add_argument("--hash", action="store_true", help="SHA-256 je Original berechnen")
    ap.add_argument("--verify-only", action="store_true",
                    help="nichts schreiben, nur Abgleich JSON <-> Dateien")
    ap.add_argument("--no-overlays", action="store_true",
                    help="keine _overlay-Varianten erzeugen")
    ap.add_argument("--overlay-quality", type=int, default=95,
                    help="JPEG-Qualitaet der _overlay-Bilder (Default 95)")
    ap.add_argument("--overlay-crf", type=int, default=18,
                    help="x264-CRF der _overlay-Videos (Default 18)")
    ap.add_argument("--join-multisnap", action="store_true",
                    help="zusammenhaengende Video-Segmente zusaetzlich zu einer "
                         "_joined.mp4 verbinden (Segmente bleiben erhalten)")
    ap.add_argument("--join-tolerance", type=float, default=2.5,
                    help="erlaubte Abweichung in Sekunden zwischen Clipdauer und "
                         "Zeitstempelabstand (Default 2.5)")
    ap.add_argument("--join-min-segment", type=float, default=8.0,
                    help="Mindestdauer, ab der ein Clip als volles Snapchat-"
                         "Segment gilt (Default 8.0)")
    args = ap.parse_args()

    root: Path = args.mydata.expanduser().resolve()
    json_paths = sorted(root.rglob("memories_history.json"))
    if not json_paths:
        sys.exit(f"memories_history.json nicht gefunden unter {root}")
    json_path = json_paths[0]
    if len(json_paths) > 1:
        print(f"Hinweis: {len(json_paths)}x memories_history.json gefunden, verwende {json_path}")

    memories = parse_json(json_path)
    with_id = sum(1 for m in memories if m.media_id)
    print(f"{len(memories)} Eintraege in {json_path.name} ({with_id} mit Media-ID)")
    resolve_timezones(memories, args.tz, args.tz_window, not args.no_geo_tz)

    idx = index_local_files(root)
    print(f"{idx.total_mains} Originale und {idx.total_overlays} Overlays lokal gefunden")
    assign_local(memories, idx)
    by_id = sum(1 for m in memories if m.match == "id")
    by_day = sum(1 for m in memories if m.match == "datum")
    with_ov = sum(1 for m in memories if m.local_overlay)
    unused = idx.total_mains - by_id - by_day

    if args.verify_only:
        print(f"Zuordenbar: {by_id + by_day} / {len(memories)} "
              f"(per ID {by_id}, per Datum {by_day})")
        print(f"Davon mit Overlay: {with_ov}")
        print(f"Per Download-Link nachzuladen: {len(memories) - by_id - by_day}")
        if unused > 0:
            print(f"Lokale Originale ohne Zuordnung: {unused}")
        print_unknown(memories, idx)
        return

    do_overlays = not args.no_overlays
    if do_overlays and Image is None:
        sys.exit("Pillow fehlt: pip install pillow  (oder --no-overlays)")
    if not shutil.which("exiftool"):
        sys.exit("exiftool nicht gefunden - zuerst 1_setup.cmd ausfuehren.")
    if do_overlays and not shutil.which("ffmpeg"):
        print("Hinweis: ffmpeg fehlt - Video-Overlays werden uebersprungen.")

    out_dir: Path = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print_unknown(memories, idx)
    n = clean_temp_files(out_dir)
    if n:
        print(f"{n} unfertige Dateien eines abgebrochenen Laufs entfernt")
    n = relocate_existing(memories, out_dir, args.tz)
    if n:
        print(f"{n} bereits gesicherte Dateien umbenannt (Ortszeit hat sich geaendert)")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    def collect(mem: Memory) -> Memory:
        base_dir, stem = mem.dest_parts(out_dir, args.tz)
        dest = base_dir / stem
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
            existing = _find_output(base_dir, stem)
            if existing is not None:
                mem.target, mem.note = existing, "vorhanden"
            elif mem.source is not None:
                mem.target = dest.with_suffix(mem.source.suffix.lower())
                _atomic_copy(mem.source, mem.target)
                mem.note = "lokal"
            else:
                mem.target, _ = resolve_and_download(mem, session, dest)
                mem.note = "download"
            mem.status = "ok"

            # Overlay-Quelle neben dem Original sichern (Download legt sie
            # bereits dort ab). Auch bei bereits vorhandenem Original, damit
            # fehlende oder fehlgeschlagene Overlays nachgeholt werden.
            ov_src = _find_output(base_dir, stem, "_overlay_src")
            if ov_src is None and mem.local_overlay is not None:
                ov_src = base_dir / f"{stem}_overlay_src{mem.local_overlay.suffix.lower()}"
                _atomic_copy(mem.local_overlay, ov_src)
            mem.overlay_source = ov_src
            burned = _find_output(base_dir, stem, "_overlay")
            if burned is not None:
                mem.overlay_target, mem.overlay_status = burned, "ok"
            elif ov_src is not None:
                mem.overlay_status = "pending"

            # Weitere Dateien zum Memory (z.B. Audio) unveraendert mitsichern
            for n, x in enumerate(mem.local_extras, start=1):
                dst = base_dir / f"{stem}_extra{n}{x.suffix.lower()}"
                if not dst.exists():
                    _atomic_copy(x, dst)
            mem.extras = [p for p in sorted(base_dir.glob(f"{stem}_extra*"))
                          if not p.name.startswith(TMP_PREFIX)]
            if mem.extras:
                mem.note += " | extras: " + ", ".join(p.suffix for p in mem.extras)
        except Exception as exc:                        # noqa: BLE001
            mem.status = "fehler"
            mem.note = str(exc)[:200]
        return mem

    print(f"\nSammle Dateien nach {out_dir} ...")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for _ in pool.map(collect, memories):
            done += 1
            if done % 100 == 0 or done == len(memories):
                print(f"  {done}/{len(memories)}")

    # --- Overlays einbrennen -------------------------------------------------
    todo = [m for m in memories if do_overlays and m.overlay_status == "pending"]
    if todo:
        print(f"\nBrenne {len(todo)} Overlays ein (Original bleibt erhalten) ...")

        def burn(mem: Memory) -> Memory:
            assert mem.target and mem.overlay_source
            out = mem.target.with_name(mem.target.stem + "_overlay" + mem.target.suffix)
            try:
                if mem.is_video or mem.target.suffix.lower() in VIDEO_EXT:
                    compose_video_overlay(mem.target, mem.overlay_source, _tmp(out),
                                          args.overlay_crf)
                else:
                    out = out.with_suffix(".jpg")
                    compose_image_overlay(mem.target, mem.overlay_source, _tmp(out),
                                          args.overlay_quality)
                os.replace(_tmp(out), out)
                mem.overlay_target = out
                mem.overlay_status = "ok"
            except Exception as exc:                    # noqa: BLE001
                mem.overlay_status = "fehler"
                mem.note = (mem.note + " | overlay: " + str(exc))[:250]
            return mem

        n = 0
        with cf.ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as pool:
            for _ in pool.map(burn, todo):
                n += 1
                if n % 50 == 0 or n == len(todo):
                    print(f"  {n}/{len(todo)}")

    # --- Multi-Snaps zusammenfuegen -----------------------------------------
    groups: list[list[Memory]] = []
    if args.join_multisnap:
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            print("\nHinweis: ffmpeg/ffprobe fehlt - Multi-Snaps werden nicht verbunden.")
        else:
            vids = [m for m in memories if m.status == "ok" and m.target
                    and m.target.suffix.lower() in VIDEO_EXT]
            print(f"\nAnalysiere {len(vids)} Videos auf Multi-Snaps ...")

            def probe(mem: Memory) -> Memory:
                mem.duration, mem.fingerprint = probe_video(mem.target)
                return mem

            with cf.ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as pool:
                list(pool.map(probe, vids))

            groups = group_multisnaps(memories, args.join_tolerance,
                                      args.join_min_segment)
            total = sum(len(g) for g in groups)
            print(f"  {len(groups)} Sessions aus {total} Segmenten erkannt")

            for gid, group in enumerate(groups, start=1):
                for pos, mem in enumerate(group, start=1):
                    mem.group_id, mem.group_pos, mem.group_size = gid, pos, len(group)
                lead = group[0]
                out = lead.target.with_name(lead.target.stem + "_joined.mp4")
                if out.exists():
                    lead.joined_target, lead.join_status = out, "ok"
                    for mem in group[1:]:
                        mem.join_status = "teil"
                    continue
                try:
                    join_group(group, _tmp(out))
                    os.replace(_tmp(out), out)
                    lead.joined_target, lead.join_status = out, "ok"
                    for mem in group[1:]:
                        mem.join_status = "teil"
                except Exception as exc:                # noqa: BLE001
                    for mem in group:
                        mem.join_status = "fehler"
                    lead.note = (lead.note + " | join: " + str(exc))[:250]

    ok = [m for m in memories if m.status == "ok"]
    failed = [m for m in memories if m.status != "ok"]
    ov_ok = [m for m in memories if m.overlay_status == "ok"]
    ov_fail = [m for m in memories if m.overlay_status == "fehler"]
    jn_ok = [m for m in memories if m.join_status == "ok"]
    jn_fail = [m for m in memories if m.join_status == "fehler"]

    n_files = sum(1 for m in ok if m.overlay_source) + len(ok) + len(ov_ok) + len(jn_ok)
    print(f"\nSchreibe Metadaten fuer {n_files} Dateien ...")
    try:
        run_exiftool(build_exiftool_args(memories, args.tz))
    except Exception as exc:                            # noqa: BLE001
        print(f"  Metadaten-Schritt fehlgeschlagen: {exc}")

    for m in ok:
        for p in (m.target, m.overlay_source, m.overlay_target, m.joined_target, *m.extras):
            if p:
                set_filesystem_time(p, m.taken_utc)

    write_report(memories, out_dir, args.hash, args.tz)
    print(f"\nFertig: {len(ok)} Originale ok, {len(failed)} fehlgeschlagen.")
    print(f"Overlays: {len(ov_ok)} erzeugt, {len(ov_fail)} fehlgeschlagen.")
    if args.join_multisnap:
        print(f"Multi-Snaps: {len(jn_ok)} Sessions verbunden, "
              f"{len(set(m.group_id for m in jn_fail))} fehlgeschlagen.")
    if failed or ov_fail:
        print("Details im Report. Skript erneut starten - bereits gesicherte")
        print("Dateien werden uebersprungen.")
        print("ACHTUNG: erst in Snapchat loeschen, wenn 0 Fehler uebrig sind.")


if __name__ == "__main__":
    main()
