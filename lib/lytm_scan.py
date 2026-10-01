"""Scan a URL with yt-dlp and return the option tree the UI renders.

Produces, for a single video:
  { kind: "video", title, video_id, thumbnail, duration, uploader,
    audio: [ {format_id, label, ext, bitrate, size} ],
    video: [ {format_id, label, ext, height, fps, size, needs_audio} ] }

And for a playlist:
  { kind: "playlist", title, count, entries: [ {video_id,title,duration,url} ] }

Format selection policy
-----------------------
Audio: prefer real audio-only streams (vcodec == "none"). Dedupe by
(abr rounded, ext) keeping the highest-quality tbr per slot. The last entry is
always a "best available" fallback.

Video: group by height, pick the best file per height. Video-only formats
(acodec == "none") are marked needs_audio so the downloader merges bestaudio.
Heights are capped at whatever the source actually offers -- no upscaling.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Harsh Gandhi (harshgandhi.com) / Buho Smart Tools (buho.co.in)
#
# LocalYTManager is EXPERIMENTAL and provided "as is" with no warranty.
# See LICENSE for the full notice, including the no-authentication warning.

import json
import sys

import lytm_common as C

# Keep memory bounded on a 415MB Pi: this is a parse-and-reduce, nothing more.
MAX_PROBE_BYTES = 32 * 1024 * 1024


def _fmt_rows(info):
    return info.get("formats") or []


def _filesize(f):
    for key in ("filesize", "filesize_approx"):
        val = f.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


def _tbr(f):
    val = f.get("tbr") or f.get("abr") or f.get("vbr")
    return float(val) if isinstance(val, (int, float)) else 0.0


def _abr(f):
    val = f.get("abr")
    if not isinstance(val, (int, float)):
        val = _tbr(f)
    return float(val)


def build_audio(info):
    """Audio-only options, best first, deduped by bitrate bucket."""
    rows = []
    for f in _fmt_rows(info):
        if f.get("vcodec") not in (None, "none"):
            continue
        if f.get("acodec") in (None, "none"):
            continue
        ext = f.get("ext") or "m4a"
        if ext in ("mhtml", "storyboard"):
            continue
        rows.append(
            {
                "format_id": f.get("format_id"),
                "ext": ext,
                "abr": _abr(f),
                "tbr": _tbr(f),
                "size": _filesize(f),
                "acodec": f.get("acodec") or "",
            }
        )
    if not rows:
        return []

    # Bucket by ~32kbps so a source with 20 near-identical streams shows a short
    # list, then keep the highest tbr inside each bucket.
    buckets = {}
    for r in rows:
        key = (r["ext"], int(round(r["abr"] / 32.0)))
        prev = buckets.get(key)
        if prev is None or r["tbr"] > prev["tbr"]:
            buckets[key] = r

    out = sorted(buckets.values(), key=lambda r: (r["abr"], r["tbr"]), reverse=True)
    for r in out:
        kbps = int(round(r["abr"]))
        r["bitrate_label"] = ("%d kbps" % kbps) if kbps > 0 else "auto"
        r["label"] = "Audio %s (%s, %s)" % (
            r["bitrate_label"],
            r["ext"],
            C.human_bytes(r["size"]) if r["size"] else "size unknown",
        )
    return out


def build_video(info):
    """One option per available height, best file at that height."""
    # Best audio-only stream size, used to estimate the merged output total for
    # video formats that carry no audio. The progress bar needs a denominator
    # that does not move, so it has to be known before the transfer starts.
    audio_only = [
        _filesize(f) for f in _fmt_rows(info)
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
        and f.get("ext") not in ("mhtml", "storyboard")
        and _filesize(f)
    ]
    best_audio_bytes = max(audio_only) if audio_only else 0

    by_height = {}
    for f in _fmt_rows(info):
        if f.get("vcodec") in (None, "none"):
            continue
        ext = f.get("ext") or "mp4"
        if ext in ("mhtml", "storyboard"):
            continue
        h = f.get("height")
        if not isinstance(h, int) or h <= 0:
            continue
        fps = int(f.get("fps") or 0) or 30
        # Key on height + rough framerate class so 30 and 60fps stay separate.
        bucket = "hi" if fps >= 50 else "std"
        key = (h, bucket)
        cand = {
            "format_id": f.get("format_id"),
            "ext": ext,
            "height": h,
            "fps": fps,
            "tbr": _tbr(f),
            "size": _filesize(f),
            "vcodec": f.get("vcodec") or "",
            "acodec": f.get("acodec") or "",
            "needs_audio": f.get("acodec") in (None, "none"),
        }
        prev = by_height.get(key)
        if prev is None:
            by_height[key] = cand
            continue
        # Prefer a self-contained stream, then the heavier bitrate.
        prev_pen = (0 if not prev["needs_audio"] else 1, -prev["tbr"])
        cand_pen = (0 if not cand["needs_audio"] else 1, -cand["tbr"])
        if cand_pen < prev_pen:
            by_height[key] = cand

    out = sorted(by_height.values(), key=lambda r: (r["height"], r["fps"]), reverse=True)
    for r in out:
        parts = ["Video %dp" % r["height"]]
        if r["fps"] >= 50:
            parts.append("%dfps" % r["fps"])
        parts.append(r["ext"])
        if r["needs_audio"]:
            parts.append("+audio")
        r["label"] = " · ".join(parts + [
            C.human_bytes(r["size"]) if r["size"] else "size unknown"])

        # Expected total for the whole job, so the progress bar has a stable
        # denominator from the first tick instead of one that jumps when the
        # second (audio) fragment reports its own size.
        #
        # Only trust this when the video stream's own size is known. Adding an
        # audio size to an unknown video size produces a plausible-looking but
        # badly wrong total (observed: 9.8 MiB for a file that ends up 17.2 MiB),
        # which shows a full bar while bytes are still arriving. When the video
        # size is missing, report None and let the UI use its indeterminate bar.
        if r["size"]:
            expected = r["size"] + (best_audio_bytes if r["needs_audio"] else 0)
        else:
            expected = 0
        r["expected_bytes"] = expected or None
        r["expected_human"] = C.human_bytes(expected) if expected else "size unknown"
        r["quality_label"] = "%dp%s" % (
            r["height"], "%dfps" % r["fps"] if r["fps"] >= 50 else "")
    return out


def _base_entry(title, video_id, duration=None, url=None):
    return {
        "title": title,
        "video_id": video_id,
        "duration": int(duration) if isinstance(duration, (int, float)) and duration else None,
        "thumbnail": C.thumb_url(video_id) if video_id else None,
        "url": url,
    }


def scan(url, cfg=None, want_playlist=False):
    """Entry point. Returns (result_dict, error_string)."""
    cfg = cfg or C.load_config()
    url = (url or "").strip()
    if not url:
        return None, "Enter a URL first."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    kind = C.guess_kind(url)
    if want_playlist or kind == "playlist":
        res = _scan_playlist(url, cfg)
        if res[0] is not None:
            return res
        # A "playlist" URL that turns out to hold one video: fall through.
    return _scan_single(url, cfg)


def _scan_playlist(url, cfg):
    rc, out, err = C.run_ytdlp(
        ["--no-warnings", "--flat-playlist", "-J", url], cfg=cfg, timeout=240
    )
    if rc != 0 or not out.strip():
        return None, _clean_error(err) or "Could not read that playlist."
    try:
        info = json.loads(out)
    except ValueError as exc:
        return None, "Unreadable playlist metadata: %s" % exc

    entries = []
    for e in info.get("entries") or []:
        if not e:
            continue
        vid = e.get("id") or ""
        title = e.get("title") or vid or "Untitled"
        entry = _base_entry(title, vid, e.get("duration"), e.get("url") or url)
        if not vid and entry["url"]:
            entry["video_id"] = e.get("display_id") or ""
        entries.append(entry)
    if not entries:
        return None, "That playlist has no entries."
    return (
        {
            "kind": "playlist",
            "title": info.get("title") or "Playlist",
            "count": len(entries),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "entries": entries,
        },
        None,
    )


def _scan_single(url, cfg):
    rc, out, err = C.run_ytdlp(
        ["--no-warnings", "--no-playlist", "-J", url], cfg=cfg, timeout=240
    )
    if rc != 0 or not out.strip():
        return None, _clean_error(err) or "Could not read that video."
    if len(out) > MAX_PROBE_BYTES:
        out = out[:MAX_PROBE_BYTES]
    try:
        info = json.loads(out)
    except ValueError as exc:
        return None, "Unreadable video metadata: %s" % exc

    vid = info.get("id") or ""
    base = _base_entry(
        info.get("title") or vid or "Untitled", vid, info.get("duration"), url
    )
    base.update(
        {
            "kind": "video",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "audio": build_audio(info),
            "video": build_video(info),
            "is_live": bool(info.get("is_live")),
        }
    )
    thumb = info.get("thumbnail")
    if thumb and not thumb.endswith(".webp"):
        base["thumbnail"] = thumb
    if not base["audio"] and not base["video"]:
        return None, "No downloadable formats were offered for this video."
    return base, None


def _clean_error(err):
    """Pull the useful line out of yt-dlp stderr."""
    if not err:
        return ""
    lines = [ln.strip() for ln in err.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.lower().startswith("error"):
            return ln.split(":", 1)[-1].strip() if ":" in ln else ln
    return lines[-1] if lines else ""


if __name__ == "__main__":
    res, error = scan(sys.argv[1] if len(sys.argv) > 1 else "")
    if error:
        print(json.dumps({"error": error}))
        sys.exit(1)
    print(json.dumps(res))