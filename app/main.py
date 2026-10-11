#!/usr/bin/env python3
"""sc-extractor - save a Snapchat Memories export losslessly.

Reads memories_history.json from the "My Data" export, collects every media
file (locally from memories/ or via download link), writes the original
capture date and GPS into it, and produces a verification report.

Originals are never re-encoded: exiftool swaps only the EXIF block (JPEG) or
the container atoms (MP4). Where a memory has an overlay, an additional
"<name>_overlay.<ext>" is rendered; the transparent source is kept as
"<name>_overlay_src.png" so that variant can always be regenerated.

Output names carry the media ID, so a re-run recognises already-saved files
even when local time or folder changed, and renames instead of duplicating.

Two export formats: older ones carry a media ID and a download link per entry.
Newer ones ship the media inside the ZIPs and carry neither, but every file
keeps the timestamp of its JSON entry - which is what matching uses instead.

Usage: see README.md, or
    python app/main.py <export-dir> --out <target> [--verify-only] ...
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import io
import json
import math
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
    sys.exit("Missing: pip install requests")

try:
    from PIL import Image
except ImportError:
    Image = None  # only needed for overlays

# --- Configuration -----------------------------------------------------------

DEFAULT_TZ = "Europe/Vienna"

# Day-precision matches get GPS only when all candidates of that day lie
# within this radius - otherwise the coordinate could be the wrong place.
GEO_BUCKET_KM = 1.0

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus"}
IGNORE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
GEO_RE = re.compile(r"(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)")
ZIP_MAGIC = b"PK\x03\x04"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Output name: <date>_<time>_<id>[_overlay|_overlay_src|_joined].<ext>
OUT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_([0-9a-z]+(?:-\d+)?)"
    r"(_overlay_src|_overlay|_joined|_extra\d+)?(\.[0-9a-z]+)$", re.I)

# Half-written files carry this prefix and are renamed only when complete,
# so an abort never leaves a file that looks finished.
TMP_PREFIX = "~tmp_"


# --- Data model --------------------------------------------------------------

@dataclass
class Memory:
    index: int
    taken_utc: datetime
    media_type: str                       # "image" | "video"
    download_url: str | None = None
    media_id: str | None = None           # from the download link
    key: str = ""                         # short ID used in filenames
    lat: float | None = None
    lon: float | None = None
    tz_name: str | None = None            # resolved capture zone
    tz_source: str = "default"            # gps | neighbour | default
    source: Path | None = None            # original file in the export
    match: str = ""                       # id | timestamp | date | bucket | ""
    precision: str = "exact"              # exact | day
    geo_ok: bool = True                   # GPS safe enough to write?
    geo_spread: float | None = None       # km spread within the bucket
    local_overlay: Path | None = None     # overlay PNG in the export
    local_extras: list[Path] = field(default_factory=list)   # e.g. audio in the export
    extras: list[Path] = field(default_factory=list)         # saved _extraN files
    overlay_source: Path | None = None    # saved _overlay_src
    target: Path | None = None            # written original
    overlay_target: Path | None = None    # rendered _overlay variant
    joined_target: Path | None = None     # joined multi-snap
    status: str = "pending"
    overlay_status: str = "-"
    join_status: str = "-"                # ok | error | part | -
    group_id: int | None = None           # multi-snap group
    group_pos: int = 0                    # position 1..n in the group
    group_size: int = 0
    duration: float | None = None         # seconds, via ffprobe
    fingerprint: tuple | None = None      # stream parameters for comparison
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
        """Capture time in the local time of the place of capture."""
        return self.taken_utc.astimezone(self.tz(fallback))

    def dest_parts(self, out_dir: Path, fallback: str) -> tuple[Path, str]:
        """(target folder, filename stem) from the local capture time."""
        local = self.local(fallback)
        base_dir = out_dir / local.strftime("%Y") / local.strftime("%Y-%m")
        return base_dir, f"{local.strftime('%Y-%m-%d_%H-%M-%S')}_{self.key}"


# --- 1) Parse the JSON -------------------------------------------------------

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
        raise SystemExit(f"No memories list found in {path}.")

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

    assign_keys(out)
    return out


def assign_keys(memories: list[Memory]) -> None:
    """Set the short ID used in filenames; duplicates get -2, -3, ...

    Prefers the UUID of the matched export file, else the media ID, else the
    JSON row number. The file UUID is the stable choice: without it a re-run
    would stop recognising saved files as soon as the JSON order changes.
    Call again after matching.
        """
    seen: dict[str, int] = {}
    for mem in memories:
        uid = _file_uuid(mem.source.stem) if mem.source is not None else None
        base = re.sub(r"[^0-9a-z]", "", uid or mem.media_id or "")[:12] or f"n{mem.index:05d}"
        seen[base] = seen.get(base, 0) + 1
        mem.key = base if seen[base] == 1 else f"{base}-{seen[base]}"


def _mid_from_link(link: str | None) -> str | None:
    """The media ID sits in the download link: ...memories?uid=..&mid=<UUID>&..."""
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
    """Determine the time zone of each memory's location.

    1. From the GPS coordinates (offline, via timezonefinder).
    2. Without GPS: from the nearest memory WITH GPS inside window_hours.
    3. Otherwise the default zone.
        """
    finder = None
    if enabled:
        try:
            from timezonefinder import TimezoneFinder
            finder = TimezoneFinder(in_memory=True)
        except ImportError:
            print("Note: timezonefinder is missing (pip install timezonefinder) - "
                  f"falling back to {default_tz} everywhere.")

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
                mem.tz_name, mem.tz_source = best.tz_name, "neighbour"

    for mem in memories:
        if not mem.tz_name:
            mem.tz_name, mem.tz_source = default_tz, "default"

    stats: dict[str, int] = {}
    zones: dict[str, int] = {}
    for mem in memories:
        stats[mem.tz_source] = stats.get(mem.tz_source, 0) + 1
        zones[mem.tz_name or default_tz] = zones.get(mem.tz_name or default_tz, 0) + 1
    print("Time zones: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    top = sorted(zones.items(), key=lambda kv: -kv[1])[:6]
    print("  most common: " + ", ".join(f"{k} ({v})" for k, v in top))


# --- 2) Index the export and match files to memories -------------------------

@dataclass
class LocalIndex:
    mains_by_uuid: dict[str, Path]
    # The same UUID can appear more than once (a memory saved twice),
    # hence a list.
    overlays_by_uuid: dict[str, list[Path]]
    overlays_by_key: dict[str, Path]
    by_date: dict[str, list[Path]]
    mains: list[Path] = field(default_factory=list)   # all originals, flat
    total_mains: int = 0
    total_overlays: int = 0
    # Neither image nor video (e.g. audio). With a UUID in the name they are
    # matched to a memory and saved as _extraN.
    extras_by_uuid: dict[str, list[Path]] = field(default_factory=dict)
    unknown: list[Path] = field(default_factory=list)          # all of them
    unassigned_extras: list[Path] = field(default_factory=list)


def _norm_key(stem: str) -> str:
    """Normalise filenames so original and overlay match up.

    "2021-08-14_abc123-main"     -> "20210814abc123"
    "2021-08-14_abc123-overlay"  -> "20210814abc123"
        """
    s = stem.lower()
    for token in ("overlay", "media", "main"):
        s = s.replace(token, "")
    return re.sub(r"[^a-z0-9]", "", s)


def _file_uuid(stem: str) -> str | None:
    m = UUID_RE.search(stem)
    return m.group(0).lower() if m else None


def index_local_files(root: Path) -> LocalIndex:
    """Index every folder named "memories" below root."""
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
                idx.overlays_by_uuid.setdefault(uid, []).append(p)
            idx.overlays_by_key.setdefault(_norm_key(p.stem), p)
            continue
        idx.total_mains += 1
        idx.mains.append(p)
        if uid:
            idx.mains_by_uuid.setdefault(uid, p)
        m = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
        if m:
            idx.by_date.setdefault(m.group(1), []).append(p)
    return idx


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Rough distance between two coordinates in km."""
    dlat = (b[0] - a[0]) * 111.0
    dlon = (b[1] - a[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dlat, dlon)


def _spread_km(mems: list[Memory]) -> float | None:
    """Largest distance within the group; None if any memory lacks GPS.

    A missing position means the matched file may not have had one either, so
    no coordinate is written.
        """
    pts = [(m.lat, m.lon) for m in mems]
    if any(p[0] is None for p in pts):
        return None
    return max((_km(a, b) for a in pts for b in pts), default=0.0)


def _mtime_utc(p: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(round(p.stat().st_mtime), tz=timezone.utc)
    except OSError:
        return None


def _forced_pairs(memories: list[Memory], idx: LocalIndex,
                  used: set[Path], claimed: set) -> list[tuple[Memory, Path]]:
    """Pairs that are certain without any assumption: exactly one open
    memory and one free file of that type on that day.    """
    pending: dict[tuple[str, bool], list[Memory]] = {}
    for mem in memories:
        if mem.source is None:
            pending.setdefault((mem.taken_utc.strftime("%Y-%m-%d"), mem.is_video), []).append(mem)
    out = []
    for (day, is_video), mems in pending.items():
        cands = [p for p in idx.by_date.get(day, [])
                 if p not in used
                 and (p.suffix.lower() in VIDEO_EXT) == is_video
                 and _file_uuid(p.stem) not in claimed]
        if len(mems) == 1 and len(cands) == 1:
            out.append((mems[0], cands[0]))
    return out


def mtime_usable(memories: list[Memory], idx: LocalIndex,
                 used: set[Path], claimed: set) -> tuple[bool, str]:
    """Does the file mtime carry the JSON timestamp?

    The export stamps each file with exactly its JSON time. Extraction keeps
    it, but it can be lost or shifted - both would cause wrong matches, so
    check against pairs that are certain anyway.
        """
    pairs = _forced_pairs(memories, idx, used, claimed)
    if len(pairs) < 20:
        return False, f"only {len(pairs)} unique pairs to check - too few"
    hit = sum(1 for mem, p in pairs if _mtime_utc(p) == mem.taken_utc)
    quote = hit / len(pairs)
    return quote >= 0.95, (f"{hit}/{len(pairs)} unique pairs agree "
                           f"({100 * quote:.1f} %)")


def assign_by_mtime(memories: list[Memory], idx: LocalIndex, used: set[Path]) -> None:
    """Match on (file mtime, media type) - exact, no guessing.

    If several entries share a second and type, their order is open, but the
    time is identical for all of them and therefore right either way. Only
    the position stays uncertain, so it is checked as for buckets.
        """
    pending: dict[tuple[datetime, bool], list[Memory]] = {}
    for mem in memories:
        if mem.source is None:
            pending.setdefault((mem.taken_utc, mem.is_video), []).append(mem)

    free: dict[tuple[datetime, bool], list[Path]] = {}
    for p in idx.mains:
        if p in used:
            continue
        mt = _mtime_utc(p)
        if mt is not None:
            free.setdefault((mt, p.suffix.lower() in VIDEO_EXT), []).append(p)

    for key, cands in free.items():
        mems = pending.get(key)
        if not mems:
            continue
        cands.sort()
        mems.sort(key=lambda m: m.index)
        spread = _spread_km(mems) if len(mems) > 1 else None
        for mem, p in zip(mems, cands):
            mem.source, mem.match, mem.precision = p, "timestamp", "exact"
            mem.geo_spread = spread
            mem.geo_ok = len(mems) == 1 or (spread is not None
                                            and spread <= GEO_BUCKET_KM)
            used.add(p)


def assign_local(memories: list[Memory], idx: LocalIndex,
                 use_mtime: bool = True) -> None:
    """Give each memory at most one local file, each file at most once.

    1. By media ID (older exports only).
    2. By file mtime, once mtime_usable() has confirmed it.
    3. By (date, type) bucket: free files of a day alphabetically against open
       memories chronologically. One pair means the match is unique
       (match="date"); several mean only the day is certain (match="bucket",
       precision="day") - the filenames carry a random UUID and the media
       itself holds no capture time.
        """
    claimed = {m.media_id for m in memories if m.media_id}
    used: set[Path] = set()

    for mem in memories:
        p = idx.mains_by_uuid.get(mem.media_id) if mem.media_id else None
        if p is not None and p not in used:
            mem.source, mem.match = p, "id"
            used.add(p)

    if use_mtime and any(m.source is None for m in memories):
        ok, detail = mtime_usable(memories, idx, used, claimed)
        print(f"File timestamps: {'usable' if ok else 'NOT usable'} - {detail}")
        if ok:
            assign_by_mtime(memories, idx, used)

    pending: dict[tuple[str, bool], list[Memory]] = {}
    for mem in memories:
        if mem.source is None:
            pending.setdefault((mem.taken_utc.strftime("%Y-%m-%d"), mem.is_video), []).append(mem)
    for (day, is_video), mems in sorted(pending.items()):
        cands = sorted(p for p in idx.by_date.get(day, [])
                       if p not in used
                       and (p.suffix.lower() in VIDEO_EXT) == is_video
                       and _file_uuid(p.stem) not in claimed)
        if not cands:
            continue
        mems.sort(key=lambda m: m.taken_utc)
        exact = len(mems) == 1 and len(cands) == 1
        if not exact:
            # Ambiguous: only guess for memories that cannot be downloaded.
            # With a link, fetching the right file beats guessing a day-mate.
            mems = [m for m in mems if not m.download_url]
            if not mems:
                continue
        spread = None if exact else _spread_km(mems)
        for mem, p in zip(mems, cands):
            mem.source = p
            mem.match = "date" if exact else "bucket"
            mem.precision = "exact" if exact else "day"
            mem.geo_spread = spread
            mem.geo_ok = exact or (spread is not None and spread <= GEO_BUCKET_KM)
            used.add(p)

    for mem in memories:
        if mem.source is None:
            continue
        uid = mem.media_id if mem.match == "id" else _file_uuid(mem.source.stem)
        cands = idx.overlays_by_uuid.get(uid, []) if uid else []
        if len(cands) > 1:
            # Several overlays with the same UUID: take the one whose name carries
            # the same date, otherwise both memories get the same file.
            same_day = [p for p in cands if p.stem[:10] == mem.source.stem[:10]]
            cands = same_day or cands
        mem.local_overlay = (cands[0] if cands
                             else idx.overlays_by_key.get(_norm_key(mem.source.stem)))

    # Unknown types (e.g. audio) with a matching UUID come along.
    taken: set[Path] = set()
    for mem in memories:
        uid = _file_uuid(mem.source.stem) if mem.source is not None else mem.media_id
        if uid and uid in idx.extras_by_uuid:
            mem.local_extras = idx.extras_by_uuid[uid]
            taken.update(mem.local_extras)
    idx.unassigned_extras = [p for p in idx.unknown if p not in taken]


def print_unknown(memories: list[Memory], idx: LocalIndex) -> None:
    """Warn about files in the export that are neither image nor video."""
    if not idx.unknown:
        return
    assigned = [p for m in memories for p in m.local_extras]
    print(f"\nWARNING: {len(idx.unknown)} files in the export are neither image nor video:")
    print(f"  {describe_unknown(idx.unknown)}")
    if assigned:
        print(f"  {len(assigned)} of them belong to a memory by ID and are saved "
              "as _extraN alongside it.")
    if idx.unassigned_extras:
        print(f"  {len(idx.unassigned_extras)} unmatched - these are NOT saved:")
        print(f"  {describe_unknown(idx.unassigned_extras)}")


def describe_unknown(paths: list[Path], limit: int = 5) -> str:
    """'3x .m4a, 1x .txt  (e.g. a.m4a, b.txt)'"""
    counts: dict[str, int] = {}
    for p in paths:
        counts[p.suffix.lower() or "(ohne)"] = counts.get(p.suffix.lower() or "(ohne)", 0) + 1
    kinds = ", ".join(f"{n}x {ext}" for ext, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return f"{kinds}  (e.g. {', '.join(p.name for p in paths[:limit])})"


# --- 2b) Target files: write atomically, find existing ones ------------------

def _tmp(path: Path) -> Path:
    return path.with_name(TMP_PREFIX + path.name)


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = _tmp(dst)
    shutil.copy2(src, tmp)                      # byte for byte
    os.replace(tmp, dst)


def _atomic_write(dst: Path, data: bytes) -> None:
    tmp = _tmp(dst)
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _find_output(base_dir: Path, stem: str, variant: str = "") -> Path | None:
    """Finished target file "<stem><variant>.<ext>", or None."""
    for p in sorted(base_dir.glob(f"{stem}{variant}.*")):
        if not p.name.startswith(TMP_PREFIX):
            return p
    return None


def clean_temp_files(out_dir: Path) -> int:
    """Remove leftovers of aborted runs (ours and exiftool's)."""
    n = 0
    for p in list(out_dir.rglob(TMP_PREFIX + "*")) + list(out_dir.rglob("*_exiftool_tmp")):
        if p.is_file():
            p.unlink(missing_ok=True)
            n += 1
    return n


def relocate_existing(memories: list[Memory], out_dir: Path, fallback_tz: str) -> int:
    """Rename saved files whose target name changed.

    The name depends on local time. If the zone changes (other --tz /
    --tz-window), the media ID in the name finds the old file and moves it
    instead of creating a duplicate.
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

    if moved:   # drop folders that became empty
        for d in sorted((d for d in out_dir.rglob("*") if d.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
    return moved


# --- 3) Download: POST -> signed URL -> GET. The answer may be a ZIP. --------

def resolve_and_download(mem: Memory, session: requests.Session, dest: Path,
                         retries: int = 4) -> tuple[Path, Path | None]:
    """Return (original, overlay_or_None).

    Memories with an overlay arrive as a ZIP with two members.
        """
    url = mem.download_url
    if not url:
        raise RuntimeError("neither a local file nor a download link")

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            if "app.snapchat.com" in url or "/dmd/" in url:
                r = session.post(url, timeout=60)
                r.raise_for_status()
                real = r.text.strip()
                if not real.startswith("http"):
                    raise RuntimeError(f"unexpected response: {real[:120]!r}")
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
    raise RuntimeError(f"Download failed: {last_err}")


def _sniff_ext(head: bytes) -> str | None:
    """File extension from the leading bytes; None if unknown."""
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
    """Unpack a download ZIP: original, overlay, and anything else.

    The original is the image/video with "main" in its name (else the first).
    Everything else is saved as <name>_extraN.<ext> rather than overwriting it.
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
        raise RuntimeError("ZIP contained no media file: "
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


# --- 4) Burn in the overlay (creates a SECOND file; the original stays) ------

def _no_window() -> dict:
    """Suppress the console window per ffmpeg call on Windows."""
    if os.name == "nt":
        return {"creationflags": 0x08000000}   # CREATE_NO_WINDOW
    return {}


def compose_image_overlay(base: Path, overlay: Path, out: Path, quality: int) -> None:
    if Image is None:
        raise RuntimeError("Pillow is missing: pip install pillow")
    with Image.open(base) as bg_raw, Image.open(overlay) as ov_raw:
        bg = bg_raw.convert("RGBA")
        ov = ov_raw.convert("RGBA")
        if ov.size != bg.size:
            ov = ov.resize(bg.size, Image.LANCZOS)
        merged = Image.alpha_composite(bg, ov).convert("RGB")
        merged.save(out, "JPEG", quality=quality, subsampling=0, optimize=True)


def compose_video_overlay(base: Path, overlay: Path, out: Path, crf: int) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is missing")
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
        raise RuntimeError((proc.stderr or "ffmpeg error").strip()[:200])


# --- 5) Write metadata (one exiftool process for all files) ------------------


# --- 4b) Detect and losslessly join multi-snaps ------------------------------

def probe_video(path: Path) -> tuple[float | None, tuple | None]:
    """Return (duration, stream fingerprint) via ffprobe."""
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
    """Chain video snaps into recording sessions.

    Two consecutive clips belong together when the gap matches the first
    clip's duration, the first is a full segment (Snapchat cuts at ~10 s), and
    both share stream parameters.

    Clips matched only to a day (precision="day") are left out: their order
    within the day is unknown, and joining them wrongly would scramble the
    recording.
        """
    clips = sorted(
        (m for m in memories
         if m.status == "ok" and m.target and m.target.suffix.lower() in VIDEO_EXT
         and m.duration and m.fingerprint and m.precision == "exact"),
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
            and prev.duration >= min_segment          # full clip, not the tail
            and abs(gap - prev.duration) <= tolerance
        )
        if chains:
            current.append(nxt)
        else:
            groups.append(current)
            current = []
    return [g for g in groups if len(g) > 1]


def join_group(group: list[Memory], out: Path) -> None:
    """Join the segments via concat demuxer, without re-encoding."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is missing")
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
            raise RuntimeError((proc.stderr or "ffmpeg error").strip()[:200])
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
        # QuickTime stores UTC. With QuickTimeUTC=1 exiftool reads a time without
        # a zone as the PC's local time and converts it - wrong on most machines.
        # So pass the zone explicitly.
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
        if mem.lat is not None and mem.geo_ok:
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
        if mem.lat is not None and mem.geo_ok:
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
        lines.pop()          # else exiftool runs an empty pass
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


# --- 6) Verification ---------------------------------------------------------

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
        w.writerow(["index", "id", "date_utc", "date_local", "timezone", "tz_source",
                    "type", "match", "precision", "status", "file", "bytes",
                    "sha256", "overlay_status", "overlay_file", "extras",
                    "group", "join_status", "joined_file", "duration_s",
                    "gps", "gps_written", "gps_spread_km", "note"])
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
                # Precision and GPS describe the written file - without one they
                # say nothing.
                m.precision if m.status == "ok" else "-",
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
                ("yes" if m.lat is not None and m.geo_ok else "no")
                if m.status == "ok" else "-",
                f"{m.geo_spread:.2f}" if m.geo_spread is not None else "",
                m.note,
            ])
    print(f"\nReport: {report}")


# --- Main flow ---------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mydata", type=Path,
                    help="export folder (contains json/ and memories/)")
    ap.add_argument("--out", type=Path, default=Path("./memories_fixed"))
    ap.add_argument("--tz", default=DEFAULT_TZ,
                    help=f"fallback time zone when there are no coordinates (default {DEFAULT_TZ})")
    ap.add_argument("--no-geo-tz", action="store_true",
                    help="do not derive the time zone from coordinates")
    ap.add_argument("--no-mtime-match", action="store_true",
                    help="do not match files by their timestamp, "
                         "even when it matches the JSON")
    ap.add_argument("--tz-window", type=float, default=12.0,
                    help="hours within which a snap without GPS inherits the zone of "
                         "the nearest snap that has GPS (0 = off)")
    ap.add_argument("--workers", type=int, default=6, help="parallel downloads")
    ap.add_argument("--hash", action="store_true", help="compute SHA-256 per original")
    ap.add_argument("--verify-only", action="store_true",
                    help="write nothing, only compare JSON against files")
    ap.add_argument("--no-overlays", action="store_true",
                    help="do not create _overlay variants")
    ap.add_argument("--overlay-quality", type=int, default=95,
                    help="JPEG quality of _overlay images (default 95)")
    ap.add_argument("--overlay-crf", type=int, default=18,
                    help="x264 CRF of _overlay videos (default 18)")
    ap.add_argument("--join-multisnap", action="store_true",
                    help="also stitch consecutive video segments into one "
                         "_joined.mp4 (the segments are kept)")
    ap.add_argument("--join-tolerance", type=float, default=2.5,
                    help="allowed difference in seconds between clip duration and "
                         "timestamp gap (default 2.5)")
    ap.add_argument("--join-min-segment", type=float, default=8.0,
                    help="minimum length for a clip to count as a full Snapchat "
                         "segment (default 8.0)")
    args = ap.parse_args()

    root: Path = args.mydata.expanduser().resolve()
    json_paths = sorted(root.rglob("memories_history.json"))
    if not json_paths:
        sys.exit(f"memories_history.json not found under {root}")
    json_path = json_paths[0]
    if len(json_paths) > 1:
        print(f"Note: found {len(json_paths)} memories_history.json files, using {json_path}")

    memories = parse_json(json_path)
    with_id = sum(1 for m in memories if m.media_id)
    print(f"{len(memories)} entries in {json_path.name} ({with_id} with media ID)")
    resolve_timezones(memories, args.tz, args.tz_window, not args.no_geo_tz)

    idx = index_local_files(root)
    print(f"{idx.total_mains} originals and {idx.total_overlays} overlays found locally")
    assign_local(memories, idx, use_mtime=not args.no_mtime_match)
    assign_keys(memories)                  # filenames from the file UUID
    by_id = sum(1 for m in memories if m.match == "id")
    by_time = sum(1 for m in memories if m.match == "timestamp")
    by_day = sum(1 for m in memories if m.match == "date")
    by_bucket = sum(1 for m in memories if m.match == "bucket")
    assigned = by_id + by_time + by_day + by_bucket
    with_ov = sum(1 for m in memories if m.local_overlay)
    unused = idx.total_mains - assigned
    no_media = [m for m in memories if m.source is None and not m.download_url]

    print(f"Matched: {assigned} / {len(memories)} (by ID {by_id}, "
          f"by timestamp {by_time}, by date {by_day}, by day+type {by_bucket})")
    if by_bucket:
        geo_off = sum(1 for m in memories if m.match == "bucket" and not m.geo_ok)
        print(f"  {by_bucket} of them day-precision only: the date is right, the time "
              "within the day may be swapped")
        print(f"  GPS therefore not written for {geo_off} entries "
              f"(spread > {GEO_BUCKET_KM:g} km or incomplete)")
    print(f"With overlay: {with_ov}")
    if no_media:
        print(f"No media in export and no download link: {len(no_media)} "
              "- cannot be saved")
    to_download = len(memories) - assigned - len(no_media)
    if to_download:
        print(f"To fetch via download link: {to_download}")
    if unused > 0:
        print(f"Local originals with no match: {unused}")

    if args.verify_only:
        print_unknown(memories, idx)
        return

    do_overlays = not args.no_overlays
    if do_overlays and Image is None:
        sys.exit("Pillow is missing: pip install pillow  (or --no-overlays)")
    if not shutil.which("exiftool"):
        sys.exit("exiftool not found - run 1_setup.cmd first.")
    if do_overlays and not shutil.which("ffmpeg"):
        print("Note: ffmpeg is missing - video overlays will be skipped.")

    out_dir: Path = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print_unknown(memories, idx)
    n = clean_temp_files(out_dir)
    if n:
        print(f"removed {n} unfinished files from an aborted run")
    n = relocate_existing(memories, out_dir, args.tz)
    if n:
        print(f"renamed {n} already-saved files (local time changed)")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    def collect(mem: Memory) -> Memory:
        base_dir, stem = mem.dest_parts(out_dir, args.tz)
        dest = base_dir / stem
        try:
            existing = _find_output(base_dir, stem)
            if existing is None and mem.source is None and not mem.download_url:
                # Newer exports have no download links. An entry left without a file
                # means the media is missing from the export itself - not a runtime
                # error, and nothing here can fix it. Return before mkdir, so no
                # empty year/month folder is left behind.
                mem.status = "missing"
                mem.note = "no media in export and no download link"
                return mem
            base_dir.mkdir(parents=True, exist_ok=True)
            if existing is not None:
                mem.target, mem.note = existing, "existing"
            elif mem.source is not None:
                mem.target = dest.with_suffix(mem.source.suffix.lower())
                _atomic_copy(mem.source, mem.target)
                mem.note = "local"
            else:
                mem.target, _ = resolve_and_download(mem, session, dest)
                mem.note = "download"
            mem.status = "ok"

            # Keep the overlay source next to the original, also when the original
            # already exists, so missing or failed overlays can be redone.
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

            # Save further files of the memory (e.g. audio) unchanged
            for n, x in enumerate(mem.local_extras, start=1):
                dst = base_dir / f"{stem}_extra{n}{x.suffix.lower()}"
                if not dst.exists():
                    _atomic_copy(x, dst)
            mem.extras = [p for p in sorted(base_dir.glob(f"{stem}_extra*"))
                          if not p.name.startswith(TMP_PREFIX)]
            if mem.extras:
                mem.note += " | extras: " + ", ".join(p.suffix for p in mem.extras)
        except Exception as exc:                        # noqa: BLE001
            mem.status = "error"
            mem.note = str(exc)[:200]
        return mem

    print(f"\nCollecting files into {out_dir} ...")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for _ in pool.map(collect, memories):
            done += 1
            if done % 100 == 0 or done == len(memories):
                print(f"  {done}/{len(memories)}")

    # --- Burn in overlays ----------------------------------------------------
    todo = [m for m in memories if do_overlays and m.overlay_status == "pending"]
    if todo:
        print(f"\nBurning in {len(todo)} overlays (originals are kept) ...")

        def burn(mem: Memory) -> Memory:
            assert mem.target and mem.overlay_source
            out = mem.target.with_name(mem.target.stem + "_overlay" + mem.target.suffix)
            try:
                # The saved file decides, not the JSON media type: a row
                # labelled video can still have been matched to an image.
                if mem.target.suffix.lower() in VIDEO_EXT:
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
                mem.overlay_status = "error"
                mem.note = (mem.note + " | overlay: " + str(exc))[:250]
            return mem

        n = 0
        with cf.ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as pool:
            for _ in pool.map(burn, todo):
                n += 1
                if n % 50 == 0 or n == len(todo):
                    print(f"  {n}/{len(todo)}")

    # --- Join multi-snaps ----------------------------------------------------
    groups: list[list[Memory]] = []
    if args.join_multisnap:
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            print("\nNote: ffmpeg/ffprobe missing - multi-snaps will not be joined.")
        else:
            vids = [m for m in memories if m.status == "ok" and m.target
                    and m.target.suffix.lower() in VIDEO_EXT]
            print(f"\nAnalysing {len(vids)} videos for multi-snaps ...")

            def probe(mem: Memory) -> Memory:
                mem.duration, mem.fingerprint = probe_video(mem.target)
                return mem

            with cf.ThreadPoolExecutor(max_workers=max(2, os.cpu_count() or 2)) as pool:
                list(pool.map(probe, vids))

            groups = group_multisnaps(memories, args.join_tolerance,
                                      args.join_min_segment)
            total = sum(len(g) for g in groups)
            print(f"  {len(groups)} sessions detected across {total} segments")

            for gid, group in enumerate(groups, start=1):
                for pos, mem in enumerate(group, start=1):
                    mem.group_id, mem.group_pos, mem.group_size = gid, pos, len(group)
                lead = group[0]
                out = lead.target.with_name(lead.target.stem + "_joined.mp4")
                if out.exists():
                    lead.joined_target, lead.join_status = out, "ok"
                    for mem in group[1:]:
                        mem.join_status = "part"
                    continue
                try:
                    join_group(group, _tmp(out))
                    os.replace(_tmp(out), out)
                    lead.joined_target, lead.join_status = out, "ok"
                    for mem in group[1:]:
                        mem.join_status = "part"
                except Exception as exc:                # noqa: BLE001
                    for mem in group:
                        mem.join_status = "error"
                    lead.note = (lead.note + " | join: " + str(exc))[:250]

    ok = [m for m in memories if m.status == "ok"]
    failed = [m for m in memories if m.status == "error"]
    missing = [m for m in memories if m.status == "missing"]
    ov_ok = [m for m in memories if m.overlay_status == "ok"]
    ov_fail = [m for m in memories if m.overlay_status == "error"]
    jn_ok = [m for m in memories if m.join_status == "ok"]
    jn_fail = [m for m in memories if m.join_status == "error"]

    n_files = sum(1 for m in ok if m.overlay_source) + len(ok) + len(ov_ok) + len(jn_ok)
    print(f"\nWriting metadata for {n_files} files ...")
    try:
        run_exiftool(build_exiftool_args(memories, args.tz))
    except Exception as exc:                            # noqa: BLE001
        print(f"  metadata step failed: {exc}")

    for m in ok:
        for p in (m.target, m.overlay_source, m.overlay_target, m.joined_target, *m.extras):
            if p:
                set_filesystem_time(p, m.taken_utc)

    write_report(memories, out_dir, args.hash, args.tz)
    print(f"\nDone: {len(ok)} originals ok, {len(failed)} failed.")
    approx = [m for m in ok if m.precision == "day"]
    if approx:
        print(f"{len(approx)} of them day-precision only (column precision=day): "
              "the date is right, the time within the day may be swapped.")
    if missing:
        print(f"{len(missing)} entries have no media in the export - cannot be saved "
              "(column status=missing).")
    print(f"Overlays: {len(ov_ok)} created, {len(ov_fail)} failed.")
    if args.join_multisnap:
        print(f"Multi-snaps: {len(jn_ok)} sessions joined, "
              f"{len(set(m.group_id for m in jn_fail))} failed.")
        skipped = sum(1 for m in ok if m.precision == "day" and m.target
                      and m.target.suffix.lower() in VIDEO_EXT)
        if skipped:
            print(f"  {skipped} videos skipped: matched day-precision only, "
                  "the segment order is unknown.")
    if failed or ov_fail:
        print("Details are in the report. Run the script again - files that are")
        print("already saved will be skipped.")
    if failed or ov_fail or missing:
        # "missing" entries cannot be retried, but they are still memories that
        # were not saved - the warning has to cover them too.
        print("WARNING: only delete in Snapchat once every entry is accounted for.")


if __name__ == "__main__":
    main()
