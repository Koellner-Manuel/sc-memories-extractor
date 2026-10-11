# sc-extractor

Rescue a Snapchat **Memories** export to your own storage — with the original
capture dates and GPS coordinates written into every file.

Runs portable from a USB drive on Windows. Python, exiftool and ffmpeg are
downloaded into `tools\`; nothing is installed on the PC and `PATH` is only
changed for the running session. The drive letter does not matter.

**Do not use Snapchat's in-app export.** It saves at most 100 memories per run
and stamps every file with *today's* date, destroying the chronology. Request
the official "My Data" export instead, as described below.

---

## Requirements

- Windows, ~30 GB free (ZIPs + extracted + output)
- NTFS, not exFAT — survives an unplanned disconnect far better
- Put the folder in the **root** of the drive (`E:\sc-extractor`) to stay
  clear of the 260-character path limit

---

## Folder layout

```
E:\sc-extractor\
├─ 0_selftest.cmd     check that the portable tools run on this PC
├─ 1_setup.cmd        one-time, needs internet: downloads tools\ (~480 MB)
├─ 2_check.cmd        dry run, writes nothing
├─ 3_run.cmd          main run, saves into output\
├─ 4_e2etest.cmd      proves the metadata really lands in the files
├─ shell.cmd          PowerShell with the portable tools on PATH
├─ app\               main.py, e2etest.py, requirements.txt
├─ scripts\           PowerShell scripts behind the .cmd files
├─ tools\             portable binaries (created by 1_setup.cmd)
├─ export\            ← extract the Snapchat export here
└─ output\            result + _verification.csv
```

Double-click any `.cmd`. Extra arguments are passed through, e.g.
`3_run.cmd --join-multisnap`.

---

## Quick start

### 1. Request the export (do this first — it can take up to 7 days)

At `accounts.snapchat.com` → **My Data**, enable:

- `Export your Memories`
- `Export JSON Files` — **required**, this is where the real dates live

Set no date filter. Submit, then wait for the email.

### 2. Download the tools

Run `1_setup.cmd` once on a PC with internet. It must end with **"Ready."**
On any other PC, `0_selftest.cmd` is enough — no setup, no internet.

Optional: add the folder to your antivirus exclusions, otherwise every
gigabyte gets scanned as it is written.

### 3. Extract the export

Download **all** ZIP parts and extract each one with **7-Zip** into its own
subfolder of `export\`. Explorer is slow on multi-GB archives and tends to
abort.

```
export\
├─ mydata~<accountid>\
│  ├─ json\memories_history.json     ← contains ALL entries
│  └─ memories\
├─ mydata~<accountid>-2\
│  └─ memories\
└─ mydata~<accountid>-3\
   └─ memories\
```

Merging is not necessary — every folder named `memories` below `export\` is
searched. **Keep the ZIPs** until the report shows no errors.

> **Do not touch the file timestamps.** Each file in the ZIP carries the exact
> capture time of its JSON entry, and newer exports rely on that to match files
> to entries. 7-Zip and Explorer both preserve it on extraction; some sync and
> cloud tools overwrite it. Step 4 checks and reports this.

### 4. Dry run

`2_check.cmd` writes nothing and reports what can be matched:

```
4000 entries in memories_history.json (0 with media ID)
Time zones: default=300, gps=3600, neighbour=100
3950 originals and 900 overlays found locally
File timestamps: usable - 900/900 unique pairs agree (100.0 %)
Matched: 3950 / 4000 (by ID 0, by timestamp 3950, by date 0, by day+type 0)
With overlay: 900
No media in export and no download link: 50 - cannot be saved
```

Check that the entry count is plausible and that nothing unexpected appears
under `Local originals with no match`. See *Matching* below for the rest.

### 5. Main run

`3_run.cmd` (equivalent to `--hash`). Add `--join-multisnap` to also stitch
split recordings together.

Takes roughly half an hour for ~4000 memories; burning video overlays
dominates. It is resumable — closing the window costs nothing, already-saved
files are skipped on the next run.

### 6. Verify

`4_e2etest.cmd` copies a handful of files to a temp folder, runs the full
pipeline on them and checks the result with exiftool and ffprobe against
values computed straight from the JSON. `export\` and `output\` are untouched.
It must end with **"End-to-end test passed."**

Then open `output\_verification.csv` and confirm:

- row count matches the entry count from step 4
- `status` contains no `error`
- `overlay_status` contains no `error`
- `precision` is `exact` throughout (see *Matching*)
- spot-check a trip abroad — does the local time look right?
- spot-check a video in Explorer → Details → "Media created"

### 7. Back up, then clean up

Three copies, two media, one off-site. Only once that is done should you
delete memories in the Snapchat app to get under the quota.

> There is no undo. When in doubt, wait a week and review the backup first.

---

## Matching

Each JSON entry has to be paired with its media file. Four strategies, in
order:

| Strategy | `match` | `precision` | When |
|---|---|---|---|
| Media ID | `id` | `exact` | older exports carry `mid=` in the download link |
| File timestamp | `timestamp` | `exact` | newer exports: the file's mtime equals its JSON timestamp, to the second |
| Unique day | `date` | `exact` | exactly one entry and one file of that type that day |
| Day + type | `bucket` | `day` | several that day — only the **date** is certain |

Timestamp matching is only used after it has been validated: on all pairs that
are forced anyway (one entry, one file that day) the timestamps must agree at
least 95 %, over at least 20 such pairs. If the output says
`File timestamps: NOT usable`, they were altered during extraction —
re-extract with 7-Zip. `--no-mtime-match` disables the strategy entirely.

Without usable timestamps, days holding several memories of the same type fall
back to `day` precision: files are paired alphabetically against entries in
chronological order. Nothing better is possible — the filename holds only a
date and a random UUID, and the media itself carries no capture time. Then:

- **GPS** is only written when all candidates for that day lie within 1 km
  (`GEO_BUCKET_KM` in `app\main.py`), so travel days get no coordinates rather
  than wrong ones
- **`--join-multisnap`** skips those videos — joined in the wrong order they
  would produce a scrambled recording
- the `precision` and `gps_spread_km` columns flag every affected row

Entries whose media is missing from the export and that have no download link
are reported as `status = missing`. A re-run cannot fix them.

---

## Output

```
output\
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d.jpg             original
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d_overlay.jpg     with overlay
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d_overlay_src.png overlay source
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d_extra1.m4a      extra file
├─ 2022\2022-07\...
└─ _verification.csv
```

The name is the **local time at the place of capture** plus the first 12
characters of the media ID. An evening-UTC snap taken in Tokyo therefore files
under the next day — that is correct.

**Originals are never re-encoded.** exiftool swaps only the EXIF block (JPEG)
or the container atoms (MP4); the image and stream data stay bit-identical.

**Overlays** (captions, drawings, stickers) produce an *additional*
`_overlay` file — necessarily re-encoded — while the original stays untouched
beside it. The transparent source is kept as `_overlay_src.png` so the variant
can always be regenerated.

**Audio is never altered.** `_overlay.mp4` copies all audio streams unchanged.

### Importing into a photo library

The written EXIF/QuickTime timestamps and coordinates are read directly by
photo managers such as Immich or PhotoPrism, which build the timeline and map
from them. Exclude `**/*_overlay_src.*`, otherwise the transparent overlay
sources appear as separate images — they carry the same date and GPS as the
photo they belong to.

---

## Verification report

`output\_verification.csv`, one row per JSON entry.

| Column | Meaning |
|---|---|
| `id` | short ID used in the filename |
| `date_utc` / `date_local` | timestamp as in the JSON / converted local time |
| `timezone` / `tz_source` | zone and its source: `gps`, `neighbour`, `default` |
| `match` / `precision` | matching strategy and how exact it is |
| `status` | `ok`, `error`, or `missing` (no media in the export) |
| `file` / `bytes` / `sha256` | saved file, size, checksum (with `--hash`) |
| `overlay_status` / `overlay_file` | `ok`, `error`, `pending`, `-` |
| `extras` | additional files saved as `_extraN`, e.g. audio |
| `group` / `join_status` / `joined_file` | multi-snap grouping |
| `gps` / `gps_written` / `gps_spread_km` | coordinates, whether written, spread |
| `note` | `local`, `download`, `existing`, or an error message |

---

## Options

Append to `2_check.cmd` / `3_run.cmd`. Export and output folders are set
automatically.

| Option | Default | Effect |
|---|---|---|
| `--verify-only` | – | check only, write nothing (= `2_check.cmd`) |
| `--hash` | off | SHA-256 per original in the report (on in `3_run.cmd`) |
| `--workers` | 6 | parallel downloads |
| `--tz` | `Europe/Vienna` | fallback zone when there are no coordinates |
| `--no-geo-tz` | – | do not derive the time zone from coordinates |
| `--tz-window` | 12 | hours within which a snap without GPS inherits a neighbour's zone |
| `--no-mtime-match` | – | do not match files by their timestamp |
| `--no-overlays` | – | do not create `_overlay` variants |
| `--overlay-quality` | 95 | JPEG quality of overlay images |
| `--overlay-crf` | 18 | x264 CRF of overlay videos |
| `--join-multisnap` | off | stitch split recordings into `_joined.mp4` |
| `--join-tolerance` | 2.5 | seconds of slack in segment detection |
| `--join-min-segment` | 8.0 | minimum length counting as a full segment |

Time zones come from the coordinates offline via `timezonefinder`. A snap
without GPS inherits the zone of the nearest snap with GPS within `--tz-window`
hours; otherwise `--tz` applies. Changing `--tz` or `--tz-window` on a re-run
**renames and moves** already-saved files rather than duplicating them.

### Multi-snap

Longer recordings are split by Snapchat into ~10-second segments.
`--join-multisnap` additionally produces one continuous file per session. A
clip is only appended when the timestamp gap matches the previous clip's
duration (±`--join-tolerance`), the previous clip was a full segment, and
codec, resolution, pixel format, frame rate and audio parameters are identical.
Joining is a stream copy — no re-encoding. All individual segments are kept.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Portable Python is missing` | `1_setup.cmd` has not been run |
| Self-test shows `[FAIL]` | run `1_setup.cmd -Force` on a PC with internet |
| `.cmd` window closes instantly | start it from a console to read the message |
| `No memories_history.json` | not extracted into `export\`, or requested without "Export JSON Files" |
| `File timestamps: NOT usable` | timestamps altered during extraction → re-extract with 7-Zip |
| Many rows with `precision = day` | same cause; without usable timestamps only the date is certain |
| Many rows with `status = missing` | media not included in the export, no download link — not fixable |
| `gps_written = no` despite coordinates | day-precision match with widely scattered candidates; threshold `GEO_BUCKET_KM` |
| `--join-multisnap` joins nothing | all candidates matched day-precision only |
| Many errors with HTTP 429 | too many parallel downloads → `3_run.cmd --workers 3` |
| All downloads fail | links older than 7 days → request a new export |
| Video overlays skipped | ffmpeg missing → run the self-test |
| exiftool errors on umlauts in the path | use a folder name without special characters |
| Path too long | put the folder in the root of the drive |
| `WARNING: … neither image nor video` | files matched by ID are saved as `_extraN`; check the rest yourself |

---

## License

MIT - see [LICENSE](LICENSE).

`1_setup.cmd` downloads Python, exiftool and ffmpeg at runtime; they are not
redistributed here and carry their own licences.
