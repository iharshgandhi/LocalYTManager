# LocalYTManager

> **Experimental.** Provided "as is", with no warranty of any kind. It is
> deployed and tested on exactly one machine (a Raspberry Pi Zero 2 W) and is
> not tested anywhere else. Read [`LICENSE`](LICENSE) before using it — it
> carries a real warning about the deliberate lack of authentication.

A local, browser-based YouTube downloader for a single Raspberry Pi. Paste a URL,
scan it, pick **audio** or **video**, pick a quality, watch a real progress bar,
download the file. Finished downloads land in a library tab and are erased
automatically after a week.

Runs entirely on the Pi. No cloud service, no API keys, no accounts. Uses
`yt-dlp` and `ffmpeg`, both of which it reuses if they are already installed.

Currently deployed on the Pi Zero 2 W at `192.168.1.200`, serving on **port 8090**.

---

## Quick start

```bash
# from this repo, on the Pi or on your machine pointed at the Pi
./install.sh
```

The installer is idempotent and safe to re-run. It prints the URL when done.

Open **http://192.168.1.200:8090/**

### Control script

```bash
sudo lytmctl status     # services, port, disk, memory, library size
sudo lytmctl queue      # what is downloading right now
sudo lytmctl logs       # tail worker + web UI logs
sudo lytmctl sweep      # run the 7-day expiry sweep immediately
sudo lytmctl restart    # restart both services
sudo lytmctl uninstall  # remove units (--purge also deletes media + config)
```

---

## How it works

Three separate pieces, so that no single failure takes everything down:

| Unit | Role |
| --- | --- |
| `localytmanager.service` | Web UI + JSON API. Port 8090. |
| `localytmanager-worker.service` | The download engine. Owns all `yt-dlp` calls. |
| `localytmanager-retention.timer` | Hourly sweep that deletes expired files. |

**Downloads do not depend on your browser.** The page only ever writes a row
into a SQLite queue; the worker claims it and runs `yt-dlp` itself. You can
close the tab, refresh, open another device, or stop the web service entirely
and the transfer keeps going. This is the central design decision — verified by
stopping `localytmanager.service` mid-download and watching the worker finish.

Queue state lives in SQLite (`data/lytm.db`), so it also survives a reboot.

### Request flow

1. **Scan** — `yt-dlp -J` reads the URL, then the format list is reduced to a
   short list of real choices: audio streams grouped by bitrate, video grouped
   by height (with 30/60fps kept separate). Nothing is upscaled.
2. **Pick** — clicking a quality enables the download button.
3. **Queue** — one row per job, handled one at a time by default.
4. **Progress** — real transferred bytes, not a timer.
5. **Done** — file plus a converted jpg thumbnail, registered in the library.

---

## The progress bar

This got the most attention, because the obvious implementation is wrong.

`yt-dlp` reports progress **per fragment**. A merged video download is a video
stream *and* an audio stream, and each one restarts at 0% with its own total. Naively
forwarding those numbers made the bar jump **69% → 41%** mid-download, and the
denominator wobble between 15 and 19 MiB.

Two fixes, layered:

1. **A pinned denominator.** The scan already knows each format's size, so the
   expected total is stored on the job at enqueue time and used verbatim. The
   percentage is then monotonic by construction — a constant denominator with a
   numerator that only grows.
2. **A cross-fragment byte accumulator.** Bytes from a finished fragment are
   banked so the count does not reset at the boundary.

When the true size is unknown, the tool **refuses to show a percentage** and
switches to an indeterminate bar instead. YouTube currently reports no size for
most video formats, so video downloads are usually indeterminate — bytes, speed
and ETA still update. A fabricated percentage would be worse than no percentage.

On completion the pinned estimate is replaced by the real on-disk size, so the
final figure is exact even when the estimate was a little off.

Progress is parsed from `--progress-template 'download:...%(progress)j'`, which
emits one JSON object per tick with actual byte counts.

---

## Library and retention

The Library tab lists every finished download as a card with its thumbnail,
size, quality, duration and days remaining. Click a card to preview it in the
browser; each card has **Download** and **Delete**.

Files are deleted **7 days** after download (`retention_days` in the config).
The sweep runs hourly and also once at worker start. Expiry is per-file, so a
new download does not reset an older one.

If a file disappears from disk by some other means, its library row is pruned
automatically rather than rendering as a card that 404s.

---

## Playlists

Paste a playlist URL and the UI lists every entry with title, thumbnail and
duration. Tick the ones you want and queue them, as audio or video — they
download sequentially in the background and each lands in the library
individually. **Options** on any row scans that single entry so you can pick a
specific quality for it instead of using the playlist default.

Flat playlist scans carry no format sizes, so playlist jobs show the
indeterminate bar.

---

## Configuration

`config/lytm.conf` on the Pi. **Created on first install, never overwritten
afterwards** — that is what keeps the port stable across upgrades.

```ini
port = 8090                  # chosen once, never changes
bind = 0.0.0.0
retention_days = 7
max_parallel_downloads = 1    # this Pi has ~150MB RAM; keep it at 1
ytdlp_extra_args =            # e.g. --extractor-args "youtube:player_client=android"
```

To change the port, edit this file and `sudo lytmctl restart`.

---

## Behaviour on this specific Pi

Constraints that shaped the implementation, all discovered the hard way:

- **No pip, no node.** Standard library only — no web framework, no build step.
- **`/tmp` is disk-backed via a bind mount.** `/etc/fstab` binds `/opt/shared/tmp`
  onto `/tmp`, so anything on the box gets a normal-sized temp dir. It used to be
  a 30MB tmpfs, which was too small for `yt-dlp` (the PyInstaller build
  self-extracts ~40MB per run and dies with
  `decompression resulted in return code -1`). Every child process still gets
  `TMPDIR=/opt/localytmanager/run/tmp` — it costs nothing, keeps this tool working
  if `/tmp` ever reverts to a tmpfs, and avoids dumping large extraction payloads
  into a directory other services share.
- **Probing the yt-dlp version costs ~14s** (that self-extract). It is cached on
  disk for 24h, and the installer warms the cache, so page loads stay at
  ~0.1s.
- **415MB RAM total, ~150MB available.** Both units run at `Nice=19` /
  `IOSchedulingClass=idle`, and the worker is capped with `MemoryMax`.
- **Never touches RumblingPodcast or Pi-hole.** Ports 80/443/53 (Pi-hole) and
  8088 (RumblingPodcast) are explicitly refused before anything is written. The
  installer's final step asserts both are still healthy.
- **yt-dlp is reused, not duplicated.** `bin/yt-dlp` is a **symlink** to
  `/opt/rumblingpodcast/bin/yt-dlp`. RumblingPodcast owns that binary and refreshes
  it with `rp update`, which swaps the file via an atomic rename — so the symlink
  picks up each new version on its own, with nothing to update and nothing to
  restart here. This tool never writes a different binary over that path. Set
  `ytdlp_path` in the config only if you want a completely separate copy.

---

## Tests

```bash
python3 scripts/test_progress.py    # 27 checks: progress monotonicity
python3 scripts/test_find_output.py # 24 checks: output/thumbnail/debris selection
```

Both run offline against synthetic data — no Pi and no network needed.

`scripts/e2e.sh` and `scripts/progress_test.sh` are live end-to-end probes
against the running service.

---

## Layout

```
install.sh              one-click installer (idempotent, reuse-first)
config/lytm.conf        port, retention, limits — created once, then preserved
bin/server.py           HTTP server: JSON API + static UI + Range streaming
bin/lytmctl             control script
lib/lytm_common.py      config, paths, SQLite schema, yt-dlp discovery
lib/lytm_scan.py        URL -> the audio/video option tree
lib/lytm_worker.py      the download queue and progress engine
web/                    index.html, app.js, style.css (no framework)
systemd/                two services + a retention timer
scripts/                tests and live probes
```

---

## Security note

There is **no authentication**, by design, because this is a LAN-only tool for a
trusted home network. Anyone who can reach the port can queue downloads and
retrieve every file the tool has kept. Do not port-forward it or expose it to
the internet without putting authentication in front of it.

---

## License

**GPL-3.0-or-later.** Copyright (C) 2026 Harsh Gandhi
(harshgandhi.com) / Buho Smart Tools (buho.co.in).

Experimental and educational, provided "as is" with no warranty. See
[`LICENSE`](LICENSE) for the full notice — including the copyright and
responsible-use terms, which are not boilerplate and are worth reading.

---

Built by Harsh Gandhi (harshgandhi.com) / Buho Smart Tools (buho.co.in).