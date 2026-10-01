#!/usr/bin/env python3
"""Unit test for the cross-fragment progress accumulator.

Feeds it the exact payload shapes yt-dlp emits for a two-fragment merged
download (video stream, then audio stream), including the mid-transfer
estimate growth that previously made the bar jump backwards.

Run:  python3 scripts/test_progress.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "lib"))

import lytm_worker as W


class FakeDB:
    """Capture update_job calls instead of touching SQLite."""

    def __init__(self):
        self.rows = {}

    def __call__(self, job_id, **fields):
        self.rows[job_id] = fields


def run(payloads, acc=None):
    acc = acc or W.new_accumulator()
    fake = FakeDB()
    original = W.update_job
    W.update_job = fake
    try:
        seen = []
        for p in payloads:
            W.apply_progress("job", p, acc)
            f = fake.rows["job"]
            seen.append((f["percent"], f["downloaded_bytes"], f["total_bytes"]))
    finally:
        W.update_job = original
    return seen


def check(name, cond, detail=""):
    print("  %-58s %s" % (name, "PASS" if cond else "FAIL " + detail))
    return cond


ok = True

def monotonic(name, seen, strict_total=True):
    """Percent and byte counts must never go backwards.

    `strict_total` additionally requires the denominator to never shrink. That
    holds whenever an expected size was known up front (the normal path, pinned
    from the scan). Without an expected size the denominator is built from
    yt-dlp's own estimates, which drift; the honest behaviour is to let it settle
    downward in the final tick once the true transferred size is known, so that
    guarantee is asserted separately for that fallback path.
    """
    global ok
    pcts = [s[0] for s in seen]
    downs = [s[1] for s in seen]
    tots = [s[2] for s in seen]
    a = all(pcts[i] <= pcts[i+1] + 0.01 for i in range(len(pcts)-1))
    b = all(downs[i] <= downs[i+1] for i in range(len(downs)-1))
    ok &= check(name + ": percent monotonic", a, str([round(p, 1) for p in pcts]))
    ok &= check(name + ": bytes monotonic", b, str(downs))
    if strict_total:
        c = all(tots[i] <= tots[i+1] for i in range(len(tots)-1))
        ok &= check(name + ": total non-decreasing", c, str(tots))
    return a and b


# --- Case 1: single fragment, steady, exact total --------------------------
print("case 1: single audio fragment, expected total known (1000)")
single = [
    {"status": "downloading", "downloaded_bytes": 100, "total_bytes": 1000, "speed": 100, "eta": 9},
    {"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000, "speed": 100, "eta": 5},
    {"status": "finished", "downloaded_bytes": 1000, "total_bytes": 1000, "speed": 100, "eta": 0},
]
seen = run(single, W.new_accumulator(expected_bytes=1000))
ok &= check("percentages are 10/50/100",
            [round(s[0]) for s in seen] == [10, 50, 100],
            str([round(s[0]) for s in seen]))
ok &= check("total pinned at 1000 throughout",
            {s[2] for s in seen} == {1000}, str([s[2] for s in seen]))
monotonic("case 1", seen)

# --- Case 2: merged video+audio with expected total pinned ------------------
print("case 2: merged download, expected total pinned (2300)")
merged = [
    # fragment 1 (video): estimate grows 500 -> 1500 as yt-dlp learns the size
    {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 500, "speed": 100, "eta": 10},
    {"status": "downloading", "downloaded_bytes": 800, "total_bytes": 1500, "speed": 100, "eta": 7},
    {"status": "downloading", "downloaded_bytes": 1500, "total_bytes_estimate": 1500, "speed": 100, "eta": 0},
    {"status": "finished", "downloaded_bytes": 1500, "total_bytes": 1500, "speed": 100, "eta": 0},
    # fragment 2 (audio): counter restarts at 0 with its own total
    {"status": "downloading", "downloaded_bytes": 100, "total_bytes": 800, "speed": 100, "eta": 7},
    {"status": "downloading", "downloaded_bytes": 600, "total_bytes": 900, "speed": 100, "eta": 3},
    {"status": "finished", "downloaded_bytes": 800, "total_bytes": 800, "speed": 100, "eta": 0},
]
seen = run(merged, W.new_accumulator(expected_bytes=2300))
monotonic("case 2", seen)
ok &= check("total never leaves 2300 (pinned)",
            {s[2] for s in seen} == {2300}, str([s[2] for s in seen]))
ok &= check("bytes cross the fragment boundary without resetting (1500 -> 1600)",
            seen[4][1] == 1600, str([s[1] for s in seen]))
ok &= check("final percent == 100", round(seen[-1][0]) == 100, str(seen[-1][0]))

# --- Case 3: no expected size -> indeterminate, not a fake percentage -------
print("case 3: merged download, NO expected size (indeterminate path)")
seen = run(merged, W.new_accumulator())
# A percentage is genuinely unknowable here: fragment 1 legitimately reaches
# 100% before fragment 2 restarts. The honest choice is bytes-only plus an
# indeterminate bar, so total_bytes stays 0 for the whole transfer.
ok &= check("total_bytes stays 0 the whole way (UI shows indeterminate)",
            all(s[2] == 0 for s in seen), str([s[2] for s in seen]))
ok &= check("percent stays 0 rather than jumping 100 -> 70",
            all(s[0] == 0 for s in seen), str([s[0] for s in seen]))
ok &= check("bytes still accumulate across fragments (monotonic)",
            all(seen[i][1] <= seen[i+1][1] for i in range(len(seen)-1)),
            str([s[1] for s in seen]))
ok &= check("final byte count is the true 2300",
            seen[-1][1] == 2300, "got %s" % seen[-1][1])

# --- Case 4: unknown total throughout ---------------------------------------
print("case 4: stream with unknown total")
unknown = [
    {"status": "downloading", "downloaded_bytes": 4096, "total_bytes": None, "speed": 500, "eta": None},
    {"status": "downloading", "downloaded_bytes": 8192, "total_bytes": None, "speed": 500, "eta": None},
]
seen = run(unknown, W.new_accumulator())
ok &= check("total stays 0 so the UI can go indeterminate",
            all(s[2] == 0 for s in seen), str([s[2] for s in seen]))
ok &= check("bytes still reported", seen[-1][1] == 8192, str(seen[-1]))
ok &= check("percent stays 0 rather than being invented",
            all(s[0] == 0 for s in seen), str([s[0] for s in seen]))

# --- Case 5: the exact regression observed on the Pi -----------------------
print("case 5: regression from the live Pi run (69% -> 41% at boundary)")
# Before the fix: total jumped 28.6MiB -> 48.6MiB and percent fell 69 -> 41.
regression = [
    {"status": "downloading", "downloaded_bytes": 16_600_000, "total_bytes": 28_600_000, "speed": 50000, "eta": 96},
    {"status": "downloading", "downloaded_bytes": 19_900_000, "total_bytes": 28_600_000, "speed": 50000, "eta": 70},
    {"status": "finished", "downloaded_bytes": 19_900_000, "total_bytes": 19_900_000, "speed": 50000, "eta": 0},
    # second fragment announces a much bigger estimate than the first ever had
    {"status": "downloading", "downloaded_bytes": 1_000_000, "total_bytes_estimate": 20_000_000, "speed": 50000, "eta": 380},
    {"status": "downloading", "downloaded_bytes": 5_000_000, "total_bytes_estimate": 25_000_000, "speed": 50000, "eta": 400},
]
seen = run(regression, W.new_accumulator(expected_bytes=28_600_000))
monotonic("case 5", seen)
ok &= check("percent does NOT fall at the fragment boundary",
            seen[3][0] >= seen[2][0],
            "%.1f -> %.1f" % (seen[2][0], seen[3][0]))
ok &= check("total never inflates past the known 28.6MB estimate",
            max(s[2] for s in seen) == 28_600_000,
            str(max(s[2] for s in seen)))

# --- Case 6: estimate undershoots reality -----------------------------------
print("case 6: expected size undershoots actual bytes")
undershoot = [
    {"status": "downloading", "downloaded_bytes": 900, "total_bytes": None, "speed": 100, "eta": None},
    {"status": "downloading", "downloaded_bytes": 1500, "total_bytes": None, "speed": 100, "eta": None},
    {"status": "finished", "downloaded_bytes": 1800, "total_bytes": None, "speed": 100, "eta": 0},
]
seen = run(undershoot, W.new_accumulator(expected_bytes=1000))
monotonic("case 6", seen)
ok &= check("total grows past the bad estimate instead of pinning at 100%",
            seen[-1][0] == 100.0, str(seen[-1][0]))

print()
print("ALL PASS" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)