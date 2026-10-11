#!/usr/bin/env python3
"""End-to-end test for the full pipeline.

Builds a mini export from the real one in a temp folder, runs main.py on it
and verifies the written metadata with exiftool and ffprobe - against values
computed straight from the JSON, not with main.py's own logic.

Covers timestamps and offsets, GPS exactly where the report claims it,
unchanged image and video data, overlay variants, a second run recognising
everything, renaming on a zone change, and the media-ID path with multi-snap.

The real export and output/ are only read. Start with 4_e2etest.cmd, or
    python app/e2etest.py --export export
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main as sc                                     # noqa: E402

TZ = "Europe/Vienna"
MAIN = Path(__file__).resolve().parent / "main.py"

_results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((bool(ok), label, detail))
    print(f"  [{'ok' if ok else 'FAIL':^6}] {label}" + (f"  {detail}" if detail else ""))
    return bool(ok)


def run_main(export: Path, out: Path, *extra: str) -> str:
    cmd = [sys.executable, str(MAIN), str(export), "--out", str(out),
           "--no-geo-tz", "--tz", TZ, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit(f"main.py brach ab (exit {proc.returncode})")
    return proc.stdout


def report_rows(out: Path) -> list[dict]:
    with (out / "_verification.csv").open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- Build the mini export ---------------------------------------------------

def load_entries(json_path: Path) -> list:
    """Raw entries of a memories_history.json, keyed as parse_json accepts them."""
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    for key in ("Saved Media", "Memories", "saved_media"):
        if isinstance(raw.get(key), list):
            return raw[key]
    for value in raw.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    raise SystemExit(f"No memories list in {json_path}")


def pick_memories(export: Path) -> tuple[list, dict]:
    """Pick a small but representative selection from the real export.

    Only memories that are alone on their day: their match is then unique even
    without timestamps, and mtime_usable() needs at least 20 such pairs.
        """
    json_path = sorted(export.rglob("memories_history.json"))[0]
    entries = load_entries(json_path)

    mems = sc.parse_json(json_path)
    idx = sc.index_local_files(export)
    sc.assign_local(mems, idx)
    sc.assign_keys(mems)

    # Only memories alone on their day: unique even without timestamps, and
    # mtime_usable() needs at least 20 such pairs before it trusts them.
    per_day: dict[str, list] = {}
    for m in mems:
        per_day.setdefault(m.taken_utc.strftime("%Y-%m-%d"), []).append(m)
    singles = [v[0] for v in per_day.values() if len(v) == 1 and v[0].source]
    singles.sort(key=lambda m: m.taken_utc)

    picked: set[int] = set()
    sel = []

    def take(pred, n):
        got = 0
        for m in singles:
            if got >= n:
                break
            if m.index not in picked and pred(m):
                picked.add(m.index)
                sel.append(m)
                got += 1

    take(lambda m: m.local_overlay and not m.is_video, 2)
    take(lambda m: m.local_overlay and m.is_video, 2)
    take(lambda m: m.lat is None, 1)
    take(lambda m: m.is_video and not m.local_overlay, 2)
    take(lambda m: not m.is_video and not m.local_overlay, 2)
    take(lambda m: True, 26 - len(sel))             # fill past the 20-pair bar
    for m in mems:                                   # one entry with no file at all
        if m.source is None:
            sel.append(m)
            break
    return sel, entries


def pick_ambiguous(export: Path) -> tuple[list, dict]:
    """Memories of one day, same type, far apart.

    Exercises the (date, type) fallback that applies once file timestamps are
    gone.
        """
    json_path = sorted(export.rglob("memories_history.json"))[0]
    entries = load_entries(json_path)

    mems = sc.parse_json(json_path)
    idx = sc.index_local_files(export)
    sc.assign_local(mems, idx, use_mtime=False)      # Buckets sichtbar machen

    groups: dict[tuple, list] = {}
    for m in mems:
        if m.source is not None and m.lat is not None:
            groups.setdefault(
                (m.taken_utc.strftime("%Y-%m-%d"), m.is_video), []).append(m)
    best = None
    for grp in groups.values():
        if not 2 <= len(grp) <= 4:
            continue
        sp = sc._spread_km(grp)
        if sp is not None and sp > sc.GEO_BUCKET_KM and (best is None or sp > best[0]):
            best = (sp, grp)
    return (best[1] if best else []), entries


def build_export(dest: Path, mems: list, entries: list) -> None:
    """Write a mini export in the format of the real one."""
    mem_dir = dest / "mydata" / "memories"
    json_dir = dest / "mydata" / "json"
    mem_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    for m in mems:
        for src in filter(None, [m.source, m.local_overlay, *m.local_extras]):
            shutil.copy2(src, mem_dir / src.name)
    payload = {"Saved Media": [entries[m.index] for m in mems]}
    (json_dir / "memories_history.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def build_legacy_export(dest: Path, export: Path) -> list[str]:
    """Mini export in the OLD format, with a media ID in the link.

    Two videos with identical stream parameters get timestamps spaced by the
    first one's duration, so multi-snap detection triggers.
        """
    mem_dir = dest / "mydata" / "memories"
    json_dir = dest / "mydata" / "json"
    mem_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    by_fp: dict[tuple, list[tuple[Path, float]]] = {}
    for p in sorted(export.rglob("*-main.mp4"))[:150]:
        dur, fp = sc.probe_video(p)
        if dur and fp and dur >= 8.0:
            by_fp.setdefault(fp, []).append((p, dur))
            if len(by_fp[fp]) >= 3:
                break
    pair = next((v for v in by_fp.values() if len(v) >= 2), None)
    if pair is None:
        return []

    start = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    rows, names = [], []
    t = start
    for p, dur in pair[:3]:
        shutil.copy2(p, mem_dir / p.name)
        uid = sc._file_uuid(p.stem)
        names.append(p.name)
        rows.append({
            "Date": t.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Media Type": "Video",
            "Location": "Latitude, Longitude: 48.20000, 16.37000",
            "Download Link": f"https://app.snapchat.com/dmd/memories?uid=u&mid={uid}",
            "Media Download Url": "",
        })
        t = t + timedelta(seconds=round(dur))
    (json_dir / "memories_history.json").write_text(
        json.dumps({"Saved Media": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return names


# --- Expected values, computed from the JSON independently -------------------

def expected(entry: dict) -> dict:
    utc = datetime.strptime(str(entry["Date"])[:19], "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=timezone.utc)
    local = utc.astimezone(ZoneInfo(TZ))
    off = local.strftime("%z")
    return {
        "utc": utc.strftime("%Y:%m:%d %H:%M:%S"),
        "local": local.strftime("%Y:%m:%d %H:%M:%S"),
        "offset": f"{off[:3]}:{off[3:]}",
        "video": "video" in str(entry["Media Type"]).lower(),
    }


def exif_dump(paths: list[Path]) -> dict[str, dict]:
    if not paths:
        return {}
    cmd = ["exiftool", "-j", "-G0:1", "-charset", "filename=UTF8", *map(str, paths)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        raise SystemExit(f"exiftool output unreadable:\n{proc.stdout[:400]}\n{proc.stderr[:400]}")
    return {Path(d["SourceFile"]).name: d for d in data}


def tag(d: dict, *suffixes: str) -> str | None:
    for k, v in d.items():
        if k.split(":")[-1] in suffixes:
            return str(v)
    return None


def stream_hash(path: Path) -> str | None:
    """Hash of the raw stream data, independent of the container."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0", "-c", "copy",
         "-f", "streamhash", "-hash", "sha256", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout.strip() or None


def pixel_hash(path: Path) -> str | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    import hashlib
    with Image.open(path) as im:
        return hashlib.sha256(im.convert("RGB").tobytes()).hexdigest()


# --- Checks ------------------------------------------------------------------

def verify_new_format(export: Path, work: Path) -> None:
    print("\n--- New format (no media ID) ---")
    mems, entries = pick_memories(export)
    mini, out = work / "neu_export", work / "neu_out"
    build_export(mini, mems, entries)
    sources = {p.name: p for p in (mini / "mydata/memories").iterdir()}
    print(f"  Mini export: {len(mems)} entries, {len(sources)} files")

    run_main(mini, out, "--hash")
    rows = report_rows(out)
    check(len(rows) == len(mems), "report has one row per entry",
          f"{len(rows)} von {len(mems)}")

    files = {p.name: p for p in out.rglob("*") if p.is_file() and p.name != "_verification.csv"}
    dumps = exif_dump(list(files.values()))

    ok_rows = [r for r in rows if r["status"] == "ok"]
    check(all(r["file"] in files for r in ok_rows),
          "every ok row has its file in the target")

    # Expected values come from the mini JSON: the report numbers its rows
    # from 0, not those of the real export.
    mini_entries = load_entries(mini / "mydata/json/memories_history.json")

    # timestamp and GPS per file
    time_bad, gps_bad, off_bad = [], [], []
    for r in ok_rows:
        exp = expected(mini_entries[int(r["index"])])
        d = dumps.get(r["file"], {})
        if exp["video"]:
            got = (tag(d, "CreateDate") or "")[:19]
            want = exp["utc"]
            creation = tag(d, "CreationDate") or ""
            if exp["offset"] not in creation:
                off_bad.append((r["file"], exp["offset"], creation))
        else:
            got = (tag(d, "DateTimeOriginal") or "")[:19]
            want = exp["local"]
            if (tag(d, "OffsetTimeOriginal") or "") != exp["offset"]:
                off_bad.append((r["file"], exp["offset"], tag(d, "OffsetTimeOriginal")))
        if got != want:
            time_bad.append((r["file"], want, got))

        has_gps = any("GPS" in k and "Ref" not in k for k in d)
        if (r["gps_written"] == "yes") != has_gps:
            gps_bad.append((r["file"], r["gps_written"], has_gps))

    check(not time_bad, "capture time is correct in every file",
          "" if not time_bad else f"{len(time_bad)} wrong, e.g. {time_bad[0]}")
    check(not off_bad, "time zone offset is correct",
          "" if not off_bad else f"{len(off_bad)} wrong, e.g. {off_bad[0]}")
    check(not gps_bad, "GPS present exactly where the report says",
          "" if not gps_bad else f"{len(gps_bad)} differ, e.g. {gps_bad[0]}")

    check(all(r["match"] == "timestamp" for r in ok_rows),
          "matching runs via the file timestamp",
          ", ".join(sorted({r["match"] for r in ok_rows})))
    check(all(r["precision"] == "exact" for r in ok_rows),
          "all matches are exact")
    with_geo = [r for r in ok_rows if r["gps"]]
    check(all(r["gps_written"] == "yes" for r in with_geo),
          "GPS is always written for exact matches",
          f"{len(with_geo)} with coordinates")

    # Losslessness: find the source via the short ID in the target name.
    def source_for(row: dict) -> Path | None:
        for p in sources.values():
            if "overlay" in p.stem.lower():
                continue
            uid = (sc._file_uuid(p.stem) or "").replace("-", "")
            if uid[:12] == row["id"].lower():
                return p
        return None

    img_bad, vid_bad, compared = [], [], 0
    for r in ok_rows:
        src, dst = source_for(r), files.get(r["file"])
        if src is None or dst is None:
            continue
        compared += 1
        # An unreadable hash counts as a failure: comparing None with None
        # would pass without having compared anything.
        if dst.suffix.lower() in sc.VIDEO_EXT:
            a, b = stream_hash(src), stream_hash(dst)
            if a is None or a != b:
                vid_bad.append(dst.name)
        else:
            a, b = pixel_hash(src), pixel_hash(dst)
            if a is None or a != b:
                img_bad.append(dst.name)
    check(compared == len(ok_rows), "every saved file mapped back to its source",
          f"{compared} of {len(ok_rows)}")
    check(not img_bad, "image data unchanged (only EXIF swapped)",
          "" if not img_bad else str(img_bad[:2]))
    check(not vid_bad, "video streams unchanged (only atoms swapped)",
          "" if not vid_bad else str(vid_bad[:2]))

    # Overlays
    ov_rows = [r for r in rows if r["overlay_status"] == "ok"]
    ov_bad = []
    for r in ov_rows:
        base, ov = files.get(r["file"]), files.get(r["overlay_file"])
        if not ov:
            ov_bad.append(r["overlay_file"])
            continue
        if ov.suffix.lower() in sc.VIDEO_EXT:
            d1, _ = sc.probe_video(base)
            d2, _ = sc.probe_video(ov)
            if not (d1 and d2 and abs(d1 - d2) < 0.5):
                ov_bad.append(f"{ov.name} Dauer {d1} vs {d2}")
        else:
            from PIL import Image
            with Image.open(base) as a, Image.open(ov) as b:
                if a.size != b.size:
                    ov_bad.append(f"{ov.name} {a.size} vs {b.size}")
    check(bool(ov_rows), "overlay variants were created (test coverage)",
          f"{len(ov_rows)}")
    check(not ov_bad, "overlay variants match the original",
          "" if not ov_bad else str(ov_bad[:2]))
    # Every overlay variant needs its source saved, else it could not be
    # regenerated later.
    src_missing = [r["file"] for r in ov_rows
                   if not list(out.rglob(Path(r["file"]).stem + "_overlay_src.*"))]
    check(not src_missing, "_overlay_src saved for every overlay variant",
          f"{sum(1 for n in files if '_overlay_src' in n)} files"
          if not src_missing else str(src_missing[:2]))

    # fehlende Medien
    missing = [r for r in rows if r["status"] == "missing"]
    if missing:
        check(all(r["precision"] == "-" and r["gps_written"] == "-"
                  for r in missing),
              "entries without media correctly marked missing", f"{len(missing)}x")

    # Second run: recognise everything, duplicate nothing
    before = {p.name: p.stat().st_size for p in out.rglob("*") if p.is_file()}
    hashes = {r["file"]: r["sha256"] for r in ok_rows}
    run_main(mini, out, "--hash")
    rows2 = report_rows(out)
    after = {p.name: p.stat().st_size for p in out.rglob("*") if p.is_file()}
    check(before.keys() == after.keys(), "second run creates no new files",
          f"{len(before)} -> {len(after)}")
    again = [r for r in rows2 if r["status"] == "ok" and "existing" not in r["note"]]
    check(not again, "second run reports everything as existing",
          "" if not again else f"{len(again)} written again")
    changed = [r["file"] for r in rows2
               if r["status"] == "ok" and hashes.get(r["file"], r["sha256"]) != r["sha256"]]
    check(not changed, "checksums unchanged after the second run",
          "" if not changed else str(changed[:2]))

    # Zeitzone wechseln: umbenennen statt duplizieren
    n_before = len([p for p in out.rglob("*") if p.is_file()])
    run_main(mini, out, "--tz", "Asia/Tokyo")
    n_after = len([p for p in out.rglob("*") if p.is_file()])
    check(n_before == n_after, "a different time zone renames instead of duplicating",
          f"{n_before} -> {n_after} files")
    tokyo = report_rows(out)
    moved = sum(1 for a, b in zip(sorted(r["file"] for r in rows2 if r["status"] == "ok"),
                                  sorted(r["file"] for r in tokyo if r["status"] == "ok"))
                if a != b)
    check(moved > 0, "filenames follow the new local time", f"{moved} renamed")
    check(not list(out.rglob(sc.TMP_PREFIX + "*")), "no ~tmp_ leftovers")


def verify_bucket_fallback(export: Path, work: Path) -> None:
    """Destroy timestamps - must fall back to (date, type)."""
    print("\n--- Fallback without file timestamps ---")
    mems, entries = pick_ambiguous(export)
    if len(mems) < 2:
        check(False, "found a day with several memories and distant locations",
              "skipped")
        return
    mini, out = work / "bucket_export", work / "bucket_out"
    build_export(mini, mems, entries)
    for p in (mini / "mydata/memories").iterdir():
        os.utime(p, (1_760_000_000, 1_760_000_000))      # all identical -> unusable
    print(f"  Mini export: {len(mems)} memories on "
          f"{mems[0].taken_utc:%Y-%m-%d}, timestamps flattened")

    stdout = run_main(mini, out, "--hash")
    check("NOT usable" in stdout,
          "timestamps are detected as unusable",
          next((l.strip() for l in stdout.splitlines()
                if "File timestamps" in l), "")[:70])
    rows = [r for r in report_rows(out) if r["status"] == "ok"]
    check(all(r["match"] in ("date", "bucket") for r in rows),
          "falls back to date / day+type",
          ", ".join(sorted({r["match"] for r in rows})))
    tag_rows = [r for r in rows if r["precision"] == "day"]
    check(bool(tag_rows), "day-precision matches are produced", f"{len(tag_rows)}x")
    held = [r for r in tag_rows if r["gps"] and r["gps_written"] == "no"]
    check(bool(held), "GPS is withheld when locations are far apart",
          f"{len(held)}x, spread up to {max((r['gps_spread_km'] for r in held), default='?')} km")

    files = {p.name: p for p in out.rglob("*") if p.is_file()}
    dumps = exif_dump([files[r["file"]] for r in held if r["file"] in files])
    leaked = [n for n, d in dumps.items()
              if any("GPS" in k and "Ref" not in k for k in d)]
    check(not leaked, "withheld GPS is absent from the file too",
          "" if not leaked else str(leaked[:2]))

    vids = [r for r in tag_rows if (out / r["file"]).suffix.lower() in sc.VIDEO_EXT]
    if vids:
        run_main(mini, out, "--join-multisnap")
        check(not list(out.rglob("*_joined.mp4")),
              "day-precision videos are not joined")


def verify_legacy_format(export: Path, work: Path) -> None:
    print("\n--- Old format (with media ID) + multi-snap ---")
    mini, out = work / "alt_export", work / "alt_out"
    names = build_legacy_export(mini, export)
    if len(names) < 2:
        check(False, "found two videos with identical stream parameters",
              "skipped - no pair in the export")
        return
    print(f"  Mini export: {len(names)} videos with a synthetic media ID")

    stdout = run_main(mini, out, "--hash", "--join-multisnap")
    rows = report_rows(out)
    check(all(r["match"] == "id" for r in rows),
          "matching runs via the media ID",
          ", ".join(sorted({r["match"] for r in rows})))
    check(all(r["precision"] == "exact" for r in rows),
          "media ID matching counts as exact")

    joined = [p for p in out.rglob("*_joined.mp4")]
    check(bool(joined), "multi-snap was joined",
          joined[0].name if joined else stdout.strip().splitlines()[-1][:80])
    if joined:
        # Files live in out/YEAR/YEAR-MONTH/, not directly in out.
        files = {p.name: p for p in out.rglob("*") if p.is_file()}
        segs = [r for r in rows if r["join_status"] in ("ok", "part")]
        check(all(r["file"] in files for r in segs),
              "individual segments are kept", f"{len(segs)} segments")
        total = sum(sc.probe_video(files[r["file"]])[0] or 0
                    for r in segs if r["file"] in files)
        dur, _ = sc.probe_video(joined[0])
        check(dur is not None and abs(dur - total) < 1.5,
              "_joined.mp4 length equals the sum of its segments",
              f"{dur:.2f}s vs {total:.2f}s" if dur is not None
              else f"duration unreadable, segments total {total:.2f}s")
        d = exif_dump(joined).get(joined[0].name, {})
        check(bool(tag(d, "CreateDate")), "_joined.mp4 received metadata",
              (tag(d, "CreateDate") or "")[:19])


def main_cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", type=Path, default=Path("export"),
                    help="the real Snapchat export (read only)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the temp folder for inspection")
    args = ap.parse_args()

    export = args.export.expanduser().resolve()
    if not sorted(export.rglob("memories_history.json")):
        raise SystemExit(f"No memories_history.json under {export}")
    for tool in ("exiftool", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found - run 1_setup.cmd first.")

    work = Path(tempfile.mkdtemp(prefix="sc_e2e_"))
    print(f"Working folder: {work}")
    try:
        verify_new_format(export, work)
        verify_bucket_fallback(export, work)
        verify_legacy_format(export, work)
    finally:
        if args.keep:
            print(f"\nTemp folder kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in _results if not r[0]]
    print(f"\n{len(_results) - len(failed)} of {len(_results)} checks passed.")
    if failed:
        print("\nFailed:")
        for _, label, detail in failed:
            print(f"  - {label}  {detail}")
        sys.exit(1)
    print("End-to-end test passed.")


if __name__ == "__main__":
    main_cli()
