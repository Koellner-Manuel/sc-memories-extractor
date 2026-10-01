# sc-extractor

Ziel: alle Memories (>8 GB) verlustfrei vom Snapchat-Server auf eigene Hardware
holen, mit korrekten Aufnahmedaten und Geokoordinaten, bevor Snapchat alles über
5 GB löscht.

**System:** Windows, portabel von einer externen SSD. Alle benötigten Programme
(Python, exiftool, ffmpeg) liegen im Ordner selbst – auf dem PC wird nichts
installiert und kein PATH dauerhaft verändert. Der Laufwerksbuchstabe der SSD
ist egal.

---

## Wichtig vorab

Snapchat hat den Gratisspeicher auf 5 GB begrenzt und für alles darüber eine
zeitlich befristete Aufbewahrung zugesagt. Der Zeitpunkt der Löschung wird pro
Account ausgerollt, es gibt kein verlässliches öffentliches Datum.

Dazu kommen zwei Fristen im Prozess selbst:

| Was | Dauer |
|---|---|
| Snapchat erstellt den Export | bis zu 7 Tage |
| Download-Links im Export sind gültig | 7 Tage |

**Schritt 1 also heute erledigen.** Alles Weitere kann in Ruhe folgen, solange
der Lauf innerhalb der Linkgültigkeit passiert.

---

## Ordnerstruktur

```
E:\sc-extractor\             ← Laufwerksbuchstabe beliebig
├─ 0_selftest.cmd            prüft, ob alle Tools auf diesem PC laufen
├─ 1_setup.cmd               einmalig, braucht Internet: lädt alle Tools nach tools\
├─ 2_check.cmd               Trockenlauf, schreibt nichts
├─ 3_run.cmd                 Hauptlauf, sichert nach output\
├─ shell.cmd                 PowerShell-Fenster mit exiftool/ffmpeg/python im PATH
├─ app\                      main.py, requirements.txt
├─ scripts\                  PowerShell-Skripte hinter den .cmd-Dateien
├─ tools\                    portable Programme (~480 MB, von 1_setup.cmd erzeugt)
├─ export\                   ← hier den Snapchat-Export entpacken
└─ output\                   Ergebnis + _verification.csv
```

Alle `.cmd`-Dateien per Doppelklick starten. Zusätzliche Parameter lassen sich
anhängen, z.B. in einer Konsole: `3_run.cmd --join-multisnap`.

---

## Schritt 1 – Export bei Snapchat anfordern

1. Im Browser `accounts.snapchat.com` öffnen und anmelden.
2. **My Data** aufrufen.
3. Folgende Optionen aktivieren:
   - `Export your Memories`
   - `Export JSON Files` ← **nicht optional**, hier stehen die Originaldaten
4. Datumsfilter **nicht** setzen (= gesamter Zeitraum).
5. E-Mail-Adresse bestätigen, **Submit**.

> Nicht die In-App-Funktion verwenden. Die exportiert maximal 100 Memories pro
> Durchgang in die Kamerarolle und schreibt jede Datei mit dem *heutigen* Datum –
> die gesamte Chronologie wäre zerstört.

Snapchat meldet sich per E-Mail, sobald der Export bereitsteht.

---

## Schritt 2 – SSD vorbereiten

Parallel zur Wartezeit erledigen.

### 2.1 SSD formatieren und Ordner kopieren

- SSD als **NTFS** formatieren (nicht exFAT). NTFS ist robuster, falls die SSD
  während eines Schreibvorgangs abgezogen wird.
- Rund **30 GB frei** einplanen: ZIPs (~8 GB) + entpackt (~8 GB) + Ausgabe
  (~8 GB) + Overlay- und Joined-Varianten.
- Den ganzen Ordner `sc-extractor` **direkt in die Wurzel** der SSD kopieren
  (z.B. `E:\sc-extractor`) – wegen des Windows-Pfadlimits von 260 Zeichen.

### 2.2 Tools einmalig herunterladen

Auf einem PC mit Internet `1_setup.cmd` doppelklicken. Das lädt nach `tools\`:

| Tool | Quelle |
|---|---|
| Python 3.13 (embeddable) + requests, Pillow, timezonefinder, tzdata | python.org, PyPI |
| exiftool (aktuelle Version) | exiftool.org / SourceForge |
| ffmpeg + ffprobe | gyan.dev (release essentials) |

Am Ende läuft automatisch ein Selbsttest, der mit **„Alles bereit.“** enden
muss. Bereits vorhandene Tools werden bei erneutem Aufruf übersprungen;
`1_setup.cmd -Force` installiert alles neu.

### 2.3 Auf jedem weiteren PC

Nur `0_selftest.cmd` ausführen. Kein Setup, keine Installation, kein Internet
nötig.

### 2.4 Defender-Ausnahme (optional, spart Zeit)

Windows-Sicherheit → Viren- & Bedrohungsschutz → Einstellungen verwalten →
Ausschlüsse → Ordner `E:\sc-extractor` hinzufügen. Sonst wird jede der ~8 GB
beim Schreiben gescannt. Gilt pro PC.

---

## Schritt 3 – Export herunterladen und entpacken

1. Link aus der Snapchat-Mail öffnen, **alle** ZIP-Teile herunterladen.
   Bei 8 GB splittet Snapchat in mehrere Dateien.
2. Jedes ZIP mit **7-Zip** in einen eigenen Unterordner von `export\`
   entpacken – nicht mit dem Explorer, der ist bei mehreren GB langsam und
   bricht gerne ab.

Zusammenkopieren ist **nicht** nötig: das Tool durchsucht alle Ordner namens
`memories` unterhalb von `export\`.

```
E:\sc-extractor\export\
├─ mydata\
│  ├─ json\
│  │  └─ memories_history.json     ← enthält ALLE Einträge, auch aus Folge-ZIPs
│  └─ memories\
├─ mydata-1\
│  └─ memories\
└─ mydata-2\
   └─ memories\
```

**ZIPs aufheben**, bis der Report am Ende 0 Fehler zeigt.

---

## Schritt 4 – Trockenlauf

`2_check.cmd` doppelklicken. Schreibt nichts, prüft nur, wie viel sich lokal
zuordnen lässt:

```
4231 Eintraege in memories_history.json (4231 mit Media-ID)
Zeitzonen: default=312, gps=3705, nachbar=214
  haeufigste: Europe/Vienna (3102), Asia/Tokyo (486), ...
4180 Originale und 622 Overlays lokal gefunden
Zuordenbar: 4180 / 4231 (per ID 4180, per Datum 0)
Davon mit Overlay: 622
Per Download-Link nachzuladen: 51
```

Zu prüfen:

- Ist die Zahl der Einträge plausibel?
- Wird der Großteil **per ID** zugeordnet? Die Media-ID steht im Download-Link
  der JSON (`mid=…`) und als UUID im Dateinamen
  (`2024-05-01_<UUID>-main.jpg`). Falls **sehr viele** nachgeladen werden
  müssten, weicht das Namensschema des Exports ab – dann erst die
  Zuordnungslogik anpassen, statt 8 GB unnötig neu herunterzuladen.
- Erscheint `Lokale Originale ohne Zuordnung`, liegen Dateien im Export, die
  keinem JSON-Eintrag zugeordnet werden konnten – ebenfalls ein Hinweis auf ein
  abweichendes Namensschema.

Die Zuordnung per Datum greift nur, wenn an einem Tag genau ein Memory und genau
eine freie Datei übrig sind. Im Zweifel wird lieber heruntergeladen als das
falsche Foto zugeordnet.

**Dateien, die weder Bild noch Video sind** (z.B. eine separate Musikdatei),
meldet der Trockenlauf mit `ACHTUNG: …`:

```
ACHTUNG: 12 Dateien im Export sind weder Bild noch Video:
  11x .m4a, 1x .txt  (z.B. 2024-08-01_<UUID>-music.m4a, notiz.txt)
  11 davon gehoeren per ID zu einem Memory und werden als _extraN mitgesichert.
  1 ohne Zuordnung - werden NICHT gesichert:
  1x .txt  (z.B. notiz.txt)
```

Dateien mit der UUID eines Memorys im Namen werden unverändert als
`<name>_extra1.<ext>` neben dem Original gesichert. Dateien ohne Zuordnung
bitte selbst ansehen, bevor der Export gelöscht wird.

---

## Schritt 5 – Hauptlauf

`3_run.cmd` doppelklicken (entspricht `--hash`).

Was dabei passiert:

1. **Zeitzonen bestimmen** – aus den Geokoordinaten per `timezonefinder`
   (offline). Snaps ohne GPS erben die Zeitzone des zeitlich nächsten Snaps mit
   GPS, sofern dieser innerhalb von 12 Stunden liegt. Sonst greift `--tz`
   (Standard `Europe/Vienna`).
2. **Dateien sammeln** – lokal vorhandene werden byteweise kopiert, der Rest
   über den Download-Link nachgeladen. Kommt statt einer Datei ein ZIP zurück,
   enthält es Original + Overlay. Weitere Dateien im ZIP (z.B. Audio) werden
   als `_extraN` gesichert und überschreiben nie das Original.
3. **Overlays einbrennen** – zu jedem Memory mit Overlay entsteht zusätzlich
   eine `_overlay`-Variante. Das Original bleibt unberührt, die Overlay-Quelle
   wird als `_overlay_src.png` mitgesichert.
4. **Metadaten schreiben** – in einem einzigen exiftool-Prozess. Bilder bekommen
   `DateTimeOriginal`, `OffsetTimeOriginal` und GPS, Videos die
   QuickTime-Äquivalente (in UTC, unabhängig von der Zeitzone des PCs). Danach
   wird die Datei-mtime gesetzt.
5. **Report schreiben** – `output\_verification.csv`.

Ergebnisstruktur:

```
output\
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d.jpg              Original
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d_overlay.jpg      mit Overlay
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d_overlay_src.png  Overlay-Quelle
├─ 2021\2021-08\2021-08-14_21-32-11_3f2a9c1be04d_extra1.m4a       weitere Datei (falls vorhanden)
├─ 2022\2022-07\...
└─ _verification.csv
```

Der Name besteht aus der **Ortszeit der Aufnahme** und den ersten 12 Zeichen der
Media-ID. Ein Snap vom 3. Juli abends UTC in Tokio landet deshalb unter
`2022-07-04` – dort war es bereits der 4.

### Ton und Musik

Die Tonspur wird nie verändert: das Original ist eine 1:1-Kopie, und die
`_overlay.mp4` übernimmt **alle** Tonspuren unverändert (Stream-Copy). Ob mit
Snapchat-„Sounds“ hinzugefügte Musik im Export enthalten ist – in der Tonspur
oder als eigene Datei – hängt vom Export ab. Zum Prüfen ein Memory mit
bekannter Musik in `export\` abspielen und im Trockenlauf auf `ACHTUNG`
achten.

### Bei Abbruch / auf einem anderen PC weitermachen

Einfach `3_run.cmd` erneut starten – auch auf einem anderen PC. Bereits
gesicherte Dateien werden übersprungen, nur Fehlendes wird nachgeholt:

- Halbfertige Dateien eines Abbruchs (`~tmp_…`) werden beim Start entfernt und
  neu erzeugt. Eine Datei bekommt ihren endgültigen Namen erst, wenn sie
  vollständig geschrieben ist.
- Fehlende oder fehlgeschlagene `_overlay`-Varianten werden aus der gesicherten
  `_overlay_src.png` neu erzeugt.

### Bei vielen Einträgen ohne GPS im selben Urlaub

```powershell
.\3_run.cmd --tz-window 72
```

Erhöht das Fenster, in dem ein Snap ohne Koordinaten die Zeitzone eines
benachbarten Snaps mit Koordinaten übernimmt, von 12 auf 72 Stunden.

Bereits gesicherte Dateien, deren Ortszeit sich dadurch ändert, werden anhand
der Media-ID wiedererkannt und **umbenannt bzw. verschoben** – es entstehen
keine Duplikate.

---

## Schritt 6 – Multi-Snaps verbinden

Längere Aufnahmen hat Snapchat in ~10-Sekunden-Segmente zerlegt. Dieser Lauf
erzeugt zusätzlich eine durchgehende Datei je Aufnahmesession:

```powershell
.\3_run.cmd --join-multisnap
```

Da die Originale bereits liegen, wird nur analysiert und verbunden.

Erkennungslogik – ein Clip wird nur angehängt, wenn **alle** Bedingungen passen:

| Bedingung | Standardwert |
|---|---|
| Abstand der Zeitstempel ≈ Dauer des vorigen Clips | ±2,5 s (`--join-tolerance`) |
| voriger Clip ist ein volles Segment | ≥ 8 s (`--join-min-segment`) |
| identischer Video-Codec, Auflösung, Pixelformat, Framerate | – |
| identischer Audio-Codec, Samplerate, Kanalzahl | – |

Das Verbinden läuft per ffmpeg concat mit Stream-Copy, also **ohne
Neukodierung**. Die `_joined.mp4` erhält Zeitstempel und Geodaten des ersten
Segments. Alle Einzelsegmente bleiben erhalten.

---

## Schritt 7 – Verifizieren

`output\_verification.csv` in Excel öffnen. Spalten:

| Spalte | Bedeutung |
|---|---|
| `id` | Kurz-ID im Dateinamen (aus der Media-ID) |
| `datum_utc` | Zeitstempel wie in der Snapchat-JSON |
| `datum_lokal` | daraus berechnete Ortszeit inkl. Offset |
| `zeitzone` / `tz_quelle` | ermittelte Zone, Quelle: `gps` / `nachbar` / `default` |
| `zuordnung` | `id`, `datum` oder `download` |
| `status` | `ok` oder `fehler` |
| `sha256` | Prüfsumme der gesicherten Datei (nach dem Schreiben der Metadaten) |
| `overlay_status` | `ok`, `fehler`, `pending` oder `-` |
| `extras` | zusätzlich gesicherte Dateien (`_extraN`), z.B. Audio |
| `gruppe` / `join_status` | Multi-Snap-Zuordnung, z.B. `G0001 2/3` / `teil` |
| `hinweis` | `lokal`, `download`, `vorhanden` oder Fehlertext |

Checkliste:

- [ ] Anzahl Zeilen = Anzahl Einträge aus Schritt 4
- [ ] Spalte `status` enthält **kein** `fehler`
- [ ] Spalte `overlay_status` enthält kein `fehler`
- [ ] Einträge mit `zuordnung = datum` stichprobenartig öffnen – passt das Foto?
- [ ] Stichprobe: eine Auslandsaufnahme öffnen, Uhrzeit gegen die Erinnerung prüfen
- [ ] Stichprobe: ein Video in Windows-Eigenschaften → Details prüfen
      („Medium erstellt“ muss die Ortszeit zeigen)
- [ ] Stichprobe: eine `_joined.mp4` abspielen, Länge plausibel
- [ ] Einträge mit `tz_quelle = default` durchsehen – dort wurde die
      Fallback-Zeitzone verwendet

Bei Fehlern: `3_run.cmd` erneut ausführen. Erst wenn hier alles sauber ist,
weitergehen.

---

## Schritt 8 – Ablegen und sichern

Nach der 3-2-1-Regel: drei Kopien, zwei Medien, eine außer Haus.

1. Arbeitskopie auf der SSD (`output\`)
2. Kopie auf ein zweites Laufwerk
3. Später auf den geplanten Heimserver (ThinkCentre)

Für den Heimserver bietet sich **Immich** im Docker-Container an: es liest die
geschriebenen EXIF-Zeitstempel und Geokoordinaten direkt aus und baut daraus
Timeline und Karte. Der Zugriff vom iPhone läuft über die Immich-App, auf die
Dateien selbst kommst du per SMB/WebDAV über die Dateien-App.

Tipp: in Immich als Ausschlussmuster `**/*_overlay_src.*` setzen, sonst
erscheinen die transparenten Overlay-Quellen als eigene Bilder in der Timeline.

Prüfsummen für den späteren Abgleich (mit `--hash` stehen sie bereits im Report):

```powershell
Get-FileHash -Algorithm SHA256 .\output\2021\2021-08\*.jpg
```

---

## Schritt 9 – Erst jetzt in Snapchat aufräumen

**Nur wenn Schritt 7 komplett abgehakt ist und mindestens zwei Kopien
existieren.**

In der Snapchat-App unter Memories die gesicherten Inhalte löschen, bis der
belegte Speicher unter 5 GB liegt.

> Ab hier gibt es kein Zurück. Im Zweifel lieber eine Woche warten und die
> Sicherung vorher einmal stichprobenartig durchsehen.

---

## Parameter-Referenz

An `2_check.cmd` / `3_run.cmd` anhängen. Export- und Zielordner (`export\`,
`output\`) setzen die Skripte automatisch.

| Parameter | Standard | Wirkung |
|---|---|---|
| `--verify-only` | – | nur prüfen, nichts schreiben (= `2_check.cmd`) |
| `--hash` | aus | SHA-256 je Original in den Report (in `3_run.cmd` aktiv) |
| `--workers` | 6 | parallele Downloads |
| `--tz` | `Europe/Vienna` | Fallback-Zeitzone ohne Geodaten |
| `--no-geo-tz` | – | Zeitzone nicht aus Koordinaten bestimmen |
| `--tz-window` | 12 | Stunden für die Zeitzonen-Übernahme vom Nachbarsnap |
| `--no-overlays` | – | keine `_overlay`-Varianten erzeugen |
| `--overlay-quality` | 95 | JPEG-Qualität der Overlay-Bilder |
| `--overlay-crf` | 18 | x264-CRF der Overlay-Videos |
| `--join-multisnap` | aus | Multi-Snaps zu `_joined.mp4` verbinden |
| `--join-tolerance` | 2.5 | Sekunden Toleranz bei der Segmenterkennung |
| `--join-min-segment` | 8.0 | ab wann ein Clip als volles Segment gilt |

---

## Problembehandlung

| Symptom | Ursache / Lösung |
|---|---|
| `Portables Python fehlt` | `1_setup.cmd` noch nicht ausgeführt |
| Selbsttest zeigt `[FEHLER]` | `1_setup.cmd -Force` auf einem PC mit Internet |
| `.cmd` öffnet sich und schließt sofort | aus einer Konsole starten, um die Meldung zu sehen |
| `Keine memories_history.json` | Export nicht unter `export\` entpackt, oder ohne „Export JSON Files“ angefordert |
| Viele `fehler` mit HTTP 429 | zu viele parallele Downloads → `3_run.cmd --workers 3` |
| Downloads scheitern alle | Links älter als 7 Tage → neuen Export anfordern |
| Video-Overlays werden übersprungen | ffmpeg fehlt → Selbsttest prüfen |
| exiftool meldet Fehler bei Umlauten im Pfad | SSD-Ordner ohne Sonderzeichen benennen |
| Pfad zu lang | Ordner direkt in die Wurzel der SSD legen |
| `ACHTUNG: … weder Bild noch Video` | siehe Schritt 4 – zugeordnete werden mitgesichert, den Rest selbst prüfen |
| Musik fehlt im Video | im Export prüfen (Schritt 5, „Ton und Musik“) – das Tool entfernt keinen Ton |
| Sehr viele Snaps mit `tz_quelle = default` | Snapchat hat wenig GPS gespeichert → `--tz-window` erhöhen |
| Einzelsnaps wurden fälschlich verbunden | `--join-tolerance 1.5` |
| Zusammengehörige Segmente blieben getrennt | `--join-tolerance 4` |
