#!/usr/bin/env python3
"""Unit test for find_output(): picking the finished file and its thumbnail.

find_output has to make decisions that depend on the whole directory being
classified first (os.listdir order is arbitrary), and it deletes leftovers. Both
behaviours are easy to get subtly wrong, so they are tested directly against a
temp directory rather than on the live Pi.

Run:  python3 scripts/test_find_output.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "lib"))

import lytm_common as C
import lytm_worker as W

ok = True


def check(name, cond, detail=""):
    global ok
    ok = bool(cond) or ok
    print("  %-62s %s" % (name, "PASS" if cond else "FAIL  " + str(detail)))


def make_job(kind="audio", video_id="VID123"):
    return {"kind": kind, "video_id": video_id}


def setup(files):
    """Build a fake media dir, point C.install_dir() at it, return cfg."""
    tmp = tempfile.mkdtemp(prefix="lytmtest.")
    media = os.path.join(tmp, "data", "media")
    os.makedirs(media)
    for name in files:
        path = os.path.join(media, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * 32)
    cfg = {"media_dir": "data/media"}
    return tmp, media, cfg


def run_case(files, kind="audio", video_id="VID123"):
    tmp, media, cfg = setup(files)
    old_home, old_id = C.install_dir, C.load_config
    C.install_dir = lambda: tmp
    try:
        job = make_job(kind, video_id)
        got_media, got_thumb = W.find_output(job, cfg)
        after = sorted(os.listdir(media))
    finally:
        C.install_dir = old_home
        C.load_config = old_id
        shutil.rmtree(tmp, ignore_errors=True)
    return got_media, got_thumb, after


print("case 1: clean audio download (m4a + converted jpg)")
m, t, after = run_case(["VID123.m4a", "VID123.jpg"])
check("finds the m4a", m and m.endswith(".m4a"), m)
check("finds the jpg", t and t.endswith(".jpg"), t)
check("leaves both files alone", after == ["VID123.jpg", "VID123.m4a"], after)

print("case 2: stray .part and .ytdl removed once media is finished")
m, t, after = run_case(["VID123.m4a", "VID123.jpg", "VID123.m4a.part", "VID123.m4a.ytdl"])
check("still finds the m4a", m and m.endswith(".m4a"), m)
check("debris deleted", after == ["VID123.jpg", "VID123.m4a"], after)

print("case 3: debris PRESERVED when no media file exists (resume possible)")
m, t, after = run_case(["VID123.m4a.part", "VID123.m4a.ytdl", "VID123.jpg"])
check("no media reported", m is None, m)
check("debris kept for resume", "VID123.m4a.part" in after, after)
check("thumb still found", t and t.endswith(".jpg"), t)

print("case 4: unconverted webp thumbnail used as fallback")
m, t, after = run_case(["VID123.m4a", "VID123.webp"])
check("falls back to the webp", t and t.endswith(".webp"), t)
check("webp NOT deleted (only copy)", "VID123.webp" in after, after)

print("case 5: jpg preferred over webp, webp dropped")
m, t, after = run_case(["VID123.webp", "VID123.jpg", "VID123.m4a"])
check("prefers the jpg", t and t.endswith(".jpg"), t)
check("redundant webp removed", "VID123.webp" not in after, after)
check("jpg survives", "VID123.jpg" in after, after)

print("case 6: fragment temporaries dropped after success")
m, t, after = run_case(["VID123.mp4", "VID123.jpg", "VID123.mp4-Frag17"])
check("fragment debris removed", "VID123.mp4-Frag17" not in after, after)
check("mp4 kept", "VID123.mp4" in after, after)

print("case 7: video job picks mp4, ignores a sibling m4a")
m, t, after = run_case(["VID123.mp4", "VID123.jpg", "VID123.m4a"], kind="video")
check("prefers mp4 for a video job", m and m.endswith(".mp4"), m)

print("case 8: prefix matching is scoped to this video id")
m, t, after = run_case(["VID123.m4a", "VID123.jpg", "OTHER999.m4a", "OTHER999.jpg"])
check("ignores another video's files", m and "OTHER999" not in m, m)
check("thumbnail is ours too", t and "OTHER999" not in t, t)

print("case 9: falls back to another container if the expected one is absent")
m, t, after = run_case(["VID123.webm", "VID123.jpg"], kind="video")
check("accepts webm when no mp4", m and m.endswith(".webm"), m)

print("case 10: empty directory")
m, t, after = run_case([])
check("returns (None, None)", (m, t) == (None, None), (m, t))

print()
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)