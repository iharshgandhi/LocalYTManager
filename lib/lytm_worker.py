"""Background download worker.

Runs as its own systemd unit, entirely independent of any browser session. The
UI only ever writes rows into the jobs table; this process claims them, runs
yt-dlp, and records progress. Closing the tab therefore cannot stop a download,
and a browser refresh re-attaches to whatever is already in flight.

Progress reporting uses yt-dlp's --progress-template with %(progress)j, which
emits one JSON object per progress tick containing the real byte counts, so the
bar reflects actual transferred bytes rather than a timer-based guess.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Harsh Gandhi (harshgandhi.com) / Buho Smart Tools (buho.co.in)
#
# LocalYTManager is EXPERIMENTAL and provided "as is" with no warranty.
# See LICENSE for the full notice, including the no-authentication warning.

import json
import os
import signal
import subprocess
import sys
import time
import uuid

import lytm_common as C

PROGRESS_PREFIX = "LYTM_PROGRESS "

_running = True


def _log(msg):
    line = "%s [%s] %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "worker",
        msg,
    )
    sys.stderr.write(line)
    sys.stderr.flush()


def _handle_signal(signum, frame):
    global _running
    _running = False
    _log("signal %s received, finishing current job then exiting" % signum)


# --------------------------------------------------------------------------
# queue helpers
# --------------------------------------------------------------------------


def enqueue(video_id, title, source_url, kind, quality, format_sel, cfg=None,
            expected_bytes=None):
    """Insert a queued job. Returns the job id.

    expected_bytes comes from the scan (where yt-dlp already reported format
    sizes) and becomes a fixed progress denominator, so the bar never jumps when
    a later fragment reports a different total of its own.
    """
    cfg = cfg or C.load_config()
    job_id = uuid.uuid4().hex[:12]
    ts = C.now()
    conn = C.connect()
    try:
        pos = conn.execute(
            "SELECT COALESCE(MAX(queue_pos), 0) + 1 AS p FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()["p"]
        conn.execute(
            """INSERT INTO jobs
               (id, video_id, title, source_url, kind, quality, format_sel,
                expected_bytes, status,
                phase, percent, message, queue_pos, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,'queued','Queued',0,?,?,?,?)""",
            (job_id, video_id, title or video_id, source_url, kind, quality,
             format_sel, int(expected_bytes) if expected_bytes else None,
             "Waiting in queue (position %d)" % pos, pos, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return job_id


def _claim_next(conn):
    """Atomically take the oldest queued job."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    cur = conn.execute(
        "UPDATE jobs SET status='running', phase='Starting', updated_at=? WHERE id=? AND status='queued'",
        (C.now(), row["id"]),
    )
    conn.commit()
    return row if cur.rowcount == 1 else None


def update_job(job_id, **fields):
    if not fields:
        return
    fields["updated_at"] = C.now()
    cols = ", ".join("%s=?" % k for k in fields)
    conn = C.connect()
    try:
        conn.execute("UPDATE jobs SET %s WHERE id=?" % cols,
                     tuple(fields.values()) + (job_id,))
        conn.commit()
    finally:
        conn.close()


def requeue_orphans():
    """On startup, mark jobs that were 'running' when we died as failed.

    A yt-dlp child does not survive a worker restart, so claiming those as
    running forever would hide them from the queue view.
    """
    conn = C.connect()
    try:
        cur = conn.execute(
            """UPDATE jobs SET status='error', phase='Interrupted',
               message='Worker restarted while this download was running. Re-queue it.',
               updated_at=? WHERE status='running'""",
            (C.now(),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# --------------------------------------------------------------------------
# progress parsing
# --------------------------------------------------------------------------


def parse_progress(line):
    """Extract the JSON payload from a yt-dlp progress line, or None."""
    idx = line.find(PROGRESS_PREFIX)
    if idx < 0:
        return None
    blob = line[idx + len(PROGRESS_PREFIX):].strip()
    if not blob.startswith("{"):
        return None
    try:
        return json.loads(blob)
    except ValueError:
        return None


def apply_progress(job_id, payload, acc):
    """Map yt-dlp's progress dict onto our job row.

    yt-dlp reports progress PER FRAGMENT: a merged video download is a video
    stream plus an audio stream, and each restarts at 0% with its own total. Used
    directly, the bar jumps backwards every time a fragment boundary is crossed.

    `acc` is a per-run accumulator and holds the fix together:

      expected   Total size for the whole job, taken from the scan (where
                 yt-dlp already reported the format sizes). When present it is
                 used verbatim as the denominator, so the bar cannot jump at all.
      done       Bytes banked from fragments that have finished.
      seen_max   Largest total reported for the fragment currently in flight.
                 Only used when no expected size was available.

    With `expected` set the percentage is monotonic by construction: the
    denominator is constant and the numerator only grows.
    """
    total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
    done = payload.get("downloaded_bytes") or 0
    speed = payload.get("speed")
    eta = payload.get("eta")
    status = payload.get("status")

    if status == "finished":
        # Bytes actually transferred are ground truth for a fragment. The size
        # yt-dlp reported along the way is only an estimate and it drifts both
        # ways, so trusting it makes the final total wrong (observed: an estimate
        # of 900 for a fragment that actually delivered 800).
        acc["done"] += int(done or 0)
        acc["fragment_done"] = 0
    else:
        acc["fragment_done"] = int(done or 0)

    grand_done = acc["done"] + acc["fragment_done"]
    if acc["expected"]:
        grand_total = int(acc["expected"])
        # The estimate can undershoot reality (yt-dlp sometimes reports no size
        # at all). Let the denominator grow in that case rather than pin at 100%
        # while bytes are still arriving -- but never let it shrink.
        if grand_done > grand_total:
            grand_total = grand_done
        pct = (float(grand_done) / float(grand_total) * 100.0) if grand_total else 0.0
    else:
        # No size was known before the transfer started, so a percentage is not
        # knowable. Fragment 1 would legitimately read 100% and fragment 2 would
        # then restart near 0%, which is a worse lie than no number at all.
        # Report bytes and speed only; the UI switches to an indeterminate bar
        # when total_bytes is 0 and pins a real 100% on completion.
        grand_total = 0
        pct = 0.0

    if grand_total and grand_done > grand_total:
        grand_done = grand_total

    fields = {
        "percent": max(0.0, min(100.0, float(pct))),
        "downloaded_bytes": int(grand_done),
        "total_bytes": int(grand_total),
        "speed": float(speed or 0.0),
        "eta": int(eta) if isinstance(eta, (int, float)) and eta >= 0 else None,
        "phase": "Downloading",
    }
    if grand_total:
        fields["message"] = "%s of %s at %s/s · ETA %s" % (
            C.human_bytes(grand_done), C.human_bytes(grand_total),
            C.human_bytes(speed or 0),
            C.human_eta(eta),
        )
    else:
        fields["message"] = "%s downloaded · %s/s%s" % (
            C.human_bytes(grand_done), C.human_bytes(speed or 0),
            (" · ETA " + C.human_eta(eta)) if eta is not None else "")
    update_job(job_id, **fields)


def new_accumulator(expected_bytes=None):
    """Fresh per-run accumulator.

    With an expected size the percentage is exact and monotonic by construction.
    Without one, `expected` stays 0 and the UI is told to show an indeterminate
    bar instead of a fabricated percentage.
    """
    return {
        "expected": int(expected_bytes) if expected_bytes else 0,
        "done": 0,
        "fragment_done": 0,
    }


# --------------------------------------------------------------------------
# the download itself
# --------------------------------------------------------------------------


def build_command(job, cfg):
    """Compose the yt-dlp argv for one job."""
    exe = C.find_ytdlp(cfg)
    kind = job["kind"]
    fmt = job["format_sel"]

    media_root = os.path.join(C.install_dir(), cfg["media_dir"])
    out_tmpl = os.path.join(media_root, "%(id)s.%(ext)s")

    cmd = [exe, "--no-warnings", "--newline", "--no-playlist",
           "--no-part", "--no-resize",
           "--progress-template", "download:" + PROGRESS_PREFIX + "%(progress)j",
           "-o", out_tmpl]

    if kind == "audio":
        # Extract to m4a so the browser gets a well-typed audio file regardless
        # of the source container.
        cmd += ["-x", "--audio-format", "m4a", "--audio-quality", "0"]
        cmd += ["-f", fmt]
    else:
        cmd += ["--merge-output-format", "mp4"]
        if fmt and fmt != "best":
            cmd += ["-f", fmt]
        else:
            cmd += ["-f", "bv*+ba/b"]

    # Thumbnail: convert to jpg so the library grid has a consistent image type.
    cmd += ["--write-thumbnail", "--convert-thumbnails", "jpg"]

    extra = (cfg.get("ytdlp_extra_args") or "").strip()
    if extra:
        cmd += extra.split()
    cmd += [job["source_url"]]
    return cmd


def find_output(job, cfg):
    """Locate the finished file and its thumbnail for a job.

    Two passes on purpose. os.listdir order is arbitrary, so every decision here
    (which thumbnail wins, whether transfer debris can be deleted) depends on
    having classified the whole directory first.
    """
    media_root = os.path.join(C.install_dir(), cfg["media_dir"])
    prefix = job["video_id"] or ""
    try:
        names = os.listdir(media_root)
    except OSError:
        return None, None
    want_ext = "m4a" if job["kind"] == "audio" else "mp4"

    media = None
    media_any = None
    jpg_thumb = None
    other_thumb = None
    debris = []

    for name in names:
        full = os.path.join(media_root, name)
        if not name.startswith(prefix) or not os.path.isfile(full):
            continue
        stem, ext = os.path.splitext(name)
        ext = ext.lower().lstrip(".")
        # Fragment temporaries look like "<title>.mp4-Frag17". splitext reports
        # the whole "-Frag17" as the extension, so match on the name instead of
        # relying on ext. Debris is only ever deleted after a real media file is
        # confirmed, so this cannot destroy a resumable transfer.
        is_debris = (
            ext in ("part", "ytdl", "temp")
            or "-frag" in name.lower()
            or name.lower().endswith((".part", ".ytdl", ".temp"))
        )
        if is_debris:
            debris.append(full)
            continue
        if ext == "jpg":
            if jpg_thumb is None:
                jpg_thumb = full
        elif ext in ("jpeg", "png", "webp"):
            # A source thumbnail whose conversion to jpg failed. Still a usable
            # image, so hold it as a fallback rather than dropping the only copy.
            if other_thumb is None:
                other_thumb = full
        elif ext == want_ext:
            if media is None:
                media = full
        elif ext in ("mp4", "m4a", "mkv", "webm", "opus"):
            if media_any is None:
                media_any = full

    if media is None:
        media = media_any

    thumb = jpg_thumb or other_thumb

    # Only clear leftovers once a real media file is confirmed present. If the
    # transfer did not finish, the .part/.ytdl files are the only copy of what
    # was fetched and must survive for a resume.
    if media is not None:
        drop = list(debris)
        if jpg_thumb and other_thumb:
            drop.append(other_thumb)   # converted jpg is the better copy
        for path in drop:
            try:
                os.remove(path)
            except OSError:
                pass

    return media, thumb


def register_media(job, media_file, thumb_file, cfg):
    """Record a completed download in the library table."""
    retention_days = int(cfg.get("retention_days", 7))
    ts = C.now()
    size = os.path.getsize(media_file) if os.path.isfile(media_file) else 0
    rel = os.path.relpath(media_file, C.install_dir())
    rel_thumb = (
        os.path.relpath(thumb_file, C.install_dir()) if thumb_file else None
    )
    conn = C.connect()
    try:
        conn.execute(
            """INSERT INTO media
               (video_id, title, kind, quality, ext, filename, rel_path, bytes,
                duration, source_url, thumb, created_at, expires_at, downloaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET
                 title=excluded.title, kind=excluded.kind, quality=excluded.quality,
                 ext=excluded.ext, filename=excluded.filename, rel_path=excluded.rel_path,
                 bytes=excluded.bytes, source_url=excluded.source_url, thumb=excluded.thumb,
                 expires_at=excluded.expires_at, downloaded_at=excluded.downloaded_at""",
            (job["video_id"], job["title"], job["kind"], job["quality"],
             os.path.splitext(media_file)[1].lstrip(".").lower(),
             os.path.basename(media_file), rel, size,
             _probe_duration(media_file), job["source_url"], rel_thumb,
             ts, ts + retention_days * 86400, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return rel


def _probe_duration(path):
    """ffprobe duration in seconds, or None. Never fatal."""
    if not path or not os.path.isfile(path):
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=60,
        )
        return int(float(proc.stdout.strip()))
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def run_job(job, cfg):
    """Execute one job to completion, streaming progress into the DB."""
    job_id = job["id"]
    update_job(job_id, phase="Preparing", percent=0, message="Starting yt-dlp",
               downloaded_bytes=0, total_bytes=0, speed=0, eta=None)

    cmd = build_command(job, cfg)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env=C.ytdlp_env(cfg),
            cwd=C.install_dir(),
        )
    except OSError as exc:
        update_job(job_id, status="error", phase="Failed",
                   message="Could not launch yt-dlp: %s" % exc)
        return

    postprocess_seen = False
    tail = []
    # Cross-fragment accumulator, pinned to the size the scan reported so the
    # percentage is monotonic across fragment boundaries.
    acc = new_accumulator(job["expected_bytes"]
                          if "expected_bytes" in job.keys() else None)
    try:
        for line in proc.stdout:
            if not _running:
                # Graceful stop: kill the child so no orphan keeps writing.
                proc.terminate()
                break
            payload = parse_progress(line)
            if payload is not None:
                if payload.get("status") == "finished":
                    postprocess_seen = True
                    apply_progress(job_id, payload, acc)
                else:
                    apply_progress(job_id, payload, acc)
                continue
            low = line.lower()
            if "[merge" in low or "merging formats" in low:
                postprocess_seen = True
                update_job(job_id, phase="Merging", message="Merging video + audio with ffmpeg")
            elif "[download] 100" in low:
                postprocess_seen = True
                update_job(job_id, phase="Finishing", message="Processing")
            elif low.startswith("error") or " error:" in low:
                tail.append(line.strip())
            elif tail and len(tail) < 6:
                tail.append(line.strip())
    finally:
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()

    rc = proc.returncode
    media_file, thumb_file = find_output(job, cfg)

    if media_file and os.path.getsize(media_file) > 0:
        rel = register_media(job, media_file, thumb_file, cfg)
        size = os.path.getsize(media_file)
        phase = "Merging" if (postprocess_seen and job["kind"] == "video") else "Complete"
        update_job(
            job_id, status="done", phase=phase, percent=100.0, speed=0, eta=0,
            downloaded_bytes=size, total_bytes=size,
            result_path=rel,
            message="Ready · %s · kept for %d days" % (C.human_bytes(size),
                                                      int(cfg.get("retention_days", 7))),
        )
        _log("done %s (%s) -> %s" % (job_id, job["kind"], os.path.basename(media_file)))
        return

    detail = " ".join(tail[-3:]).strip()
    if rc in (-2, -15) and not _running:
        update_job(job_id, status="cancelled", phase="Stopped",
                   message="Worker stopped before this finished. Re-queue it.")
        return
    msg = detail or ("yt-dlp exited with code %s" % rc if rc else "yt-dlp produced no file")
    update_job(job_id, status="error", phase="Failed", message=msg[:400])
    _log("failed %s: %s" % (job_id, msg[:200]))


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------


def enforce_retention(cfg=None, dry_run=False):
    """Delete library entries whose expiry has passed. Returns removed count."""
    cfg = cfg or C.load_config()
    ts = C.now()
    conn = C.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM media WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (ts,),
        ).fetchall()
        removed = 0
        for row in rows:
            targets = []
            if row["rel_path"]:
                targets.append(row["rel_path"])
            if row["thumb"]:
                targets.append(row["thumb"])
            if not dry_run:
                for rel in targets:
                    try:
                        full = C.media_path(rel)
                        if os.path.isfile(full):
                            os.remove(full)
                    except (ValueError, OSError):
                        pass
                conn.execute("DELETE FROM media WHERE id=?", (row["id"],))
                removed += 1
        if not dry_run:
            conn.commit()
        return removed, len(rows)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


def loop(cfg=None):
    cfg = cfg or C.load_config()
    C.init_db()
    orphans = requeue_orphans()
    if orphans:
        _log("re-queued %d interrupted job(s) as failed" % orphans)
    removed, expired = enforce_retention(cfg)
    if removed:
        _log("retention sweep removed %d expired file(s)" % removed)

    parallel = max(1, int(cfg.get("max_parallel_downloads", 1)))
    _log("worker up · parallel=%d · retention=%sd · tmp=%s"
         % (parallel, cfg.get("retention_days"), C.tmp_dir()))

    while _running:
        conn = C.connect()
        try:
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status='running'"
            ).fetchone()["n"]
            job = None
            if active < parallel:
                job = _claim_next(conn)
        finally:
            conn.close()

        if job is None:
            time.sleep(2)
            continue

        row = dict(job)
        # Drop the parallel ceiling for a moment so a playlist can run while
        # another stream finishes; simplicity beats a real pool here.
        run_job(row, cfg)

    _log("worker exiting")


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    cfg = C.load_config()
    # Standalone invocation: reap anything already past expiry.
    enforce_retention(cfg)
    loop(cfg)


if __name__ == "__main__":
    main()