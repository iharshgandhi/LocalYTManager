"""Shared helpers for LocalYTManager: config, paths, database, job queue.

LocalYTManager by Harsh Gandhi (harshgandhi.com) / Buho Smart Tools
(buho.co.in). Standard library only. The Pi Zero 2 W has no pip, so nothing here
may import a third-party package.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Harsh Gandhi (harshgandhi.com) / Buho Smart Tools (buho.co.in)
#
# LocalYTManager is EXPERIMENTAL and provided "as is" with no warranty.
# See LICENSE for the full notice, including the no-authentication warning.

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.parse

APP = "LocalYTManager"
VERSION = "1.0.0"


def install_dir():
    return os.environ.get("LYTM_HOME") or "/opt/localytmanager"


def conf_path():
    return os.path.join(install_dir(), "config", "lytm.conf")


def run_dir():
    return os.path.join(install_dir(), "run")


def tmp_dir():
    return os.path.join(run_dir(), "tmp")


def log_dir():
    return os.path.join(install_dir(), "logs")


def state_dir():
    return os.path.join(install_dir(), "data")


def db_path():
    return os.path.join(state_dir(), "lytm.db")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

_DEFAULTS = {
    "port": 8090,
    "bind": "0.0.0.0",
    "retention_days": 7,
    "max_parallel_downloads": 1,
    "media_dir": "data/media",
    "thumb_dir": "data/thumbs",
    "ytdlp_path": "",
    "ytdlp_extra_args": "",
    "worker_nice": 19,
}


def load_config():
    """Read lytm.conf. Missing keys fall back to defaults; never rewrites."""
    cfg = dict(_DEFAULTS)
    try:
        with open(conf_path(), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if key not in cfg:
                    continue
                cfg[key] = _coerce(cfg[key], val)
    except FileNotFoundError:
        pass
    return cfg


def _coerce(current, raw):
    if isinstance(current, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            return current
    return raw


# --------------------------------------------------------------------------
# yt-dlp discovery — reuse whatever is already installed
# --------------------------------------------------------------------------

_YTDLP_CANDIDATES = (
    "{home}/bin/yt-dlp",
    "/opt/rumblingpodcast/bin/yt-dlp",
    "/usr/local/bin/yt-dlp",
    "/usr/bin/yt-dlp",
)


def find_ytdlp(cfg=None):
    """Resolve a usable yt-dlp.

    Order: explicit config value, our own bin copy, then $PATH.

    bin/yt-dlp is a SYMLINK to /opt/rumblingpodcast/bin/yt-dlp. We do not own
    that file: RumblingPodcast installed it and refreshes it with `rp update`,
    which swaps the binary via an atomic rename. The symlink picks up the new
    version automatically, so there is nothing to do here and nothing to
    restart -- but do not write a different binary over that path, because this
    tool would silently start using it.

    Returns an absolute path or None.
    """
    cfg = cfg or load_config()
    explicit = (cfg.get("ytdlp_path") or "").strip()
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit

    for tmpl in _YTDLP_CANDIDATES:
        cand = tmpl.format(home=install_dir())
        if os.path.isfile(cand):
            if not os.access(cand, os.X_OK):
                try:
                    os.chmod(cand, 0o755)
                except OSError:
                    continue
            return cand

    which = shutil.which("yt-dlp")
    return which


def ytdlp_env(cfg=None):
    """Environment for yt-dlp children.

    /tmp on this Pi is now disk-backed: /etc/fstab binds /opt/shared/tmp onto
    /tmp, so anything on the box gets a normal-sized temp dir. It used to be a
    30MB tmpfs, which was too small for yt-dlp -- the PyInstaller build
    self-extracts ~40MB on every run and dies with "decompression resulted in
    return code -1" without room.

    Keep setting TMPDIR anyway. It costs nothing, it keeps this tool working if
    /tmp ever reverts to a tmpfs, and it avoids writing large extraction
    payloads into a directory other services share.
    """
    env = dict(os.environ)
    tmp = tmp_dir()
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError:
        pass
    env["TMPDIR"] = tmp
    env["TMP"] = tmp
    env["TEMP"] = tmp
    return env


def run_ytdlp(args, cfg=None, timeout=180, check=False):
    """Run yt-dlp and return (returncode, stdout, stderr) with text output."""
    cfg = cfg or load_config()
    exe = find_ytdlp(cfg)
    if not exe:
        return 127, "", "yt-dlp binary not found"
    try:
        proc = subprocess.run(
            [exe] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout,
            env=ytdlp_env(cfg),
        )
    except subprocess.TimeoutExpired:
        return 124, "", "yt-dlp timed out after %ss" % timeout
    if check and proc.returncode != 0:
        return proc.returncode, proc.stdout, proc.stderr
    return proc.returncode, proc.stdout, proc.stderr


def ytdlp_version(cfg=None, max_age=86400):
    """yt-dlp's version string, cached on disk.

    Probing the version costs ~14s on this Pi: the PyInstaller build
    self-extracts ~40MB before printing. The status endpoint is polled every few
    seconds, so it must never shell out on the request path.
    """
    cfg = cfg or load_config()
    exe = find_ytdlp(cfg)
    if not exe:
        return "not found"

    cache = os.path.join(run_dir(), "ytdlp-version")
    try:
        age = time.time() - os.path.getmtime(cache)
        if age < max_age:
            with open(cache, "r", encoding="utf-8") as fh:
                cached = fh.read().strip()
            if cached:
                return cached
    except OSError:
        pass

    rc, out, _ = run_ytdlp(["--version"], cfg=cfg, timeout=180)
    ver = out.strip() if rc == 0 and out.strip() else "error"
    if ver not in ("error", "not found"):
        try:
            os.makedirs(run_dir(), exist_ok=True)
            tmp = cache + ".new"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(ver)
            os.replace(tmp, cache)
        except OSError:
            pass
    return ver


def nice_prefix(cfg=None):
    """argv prefix that runs a child at the configured priority.

    Unused in normal operation: the worker is a systemd unit carrying Nice=19 and
    IOSchedulingClass=idle, and its yt-dlp children inherit both. Kept for
    one-off manual runs where the unit is not in play.
    """
    cfg = cfg or load_config()
    nice = int(cfg.get("worker_nice", 19))
    if os.geteuid() == 0:
        return []
    return ["nice", "-n", str(nice)]


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     TEXT UNIQUE,
    title        TEXT,
    kind         TEXT,             -- audio | video
    quality      TEXT,
    ext          TEXT,
    filename     TEXT,
    rel_path     TEXT,
    bytes        INTEGER,
    duration     INTEGER,
    source_url   TEXT,
    thumb        TEXT,
    created_at   INTEGER,
    expires_at   INTEGER,
    downloaded_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_expiry ON media(expires_at);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    video_id     TEXT,
    title        TEXT,
    source_url   TEXT,
    kind         TEXT,
    quality      TEXT,
    format_sel   TEXT,
    expected_bytes INTEGER,
    status       TEXT,             -- queued | running | done | error | cancelled
    phase        TEXT,
    percent      REAL,
    downloaded_bytes INTEGER,
    total_bytes  INTEGER,
    speed        REAL,
    eta          INTEGER,
    message      TEXT,
    result_path  TEXT,
    queue_pos    INTEGER,
    created_at   INTEGER,
    updated_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def connect():
    os.makedirs(state_dir(), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db():
    conn = connect()
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS silently leaves an existing table alone, so a
    reinstall against an old DB would then fail every INSERT with "no such
    column". Adding the column conditionally keeps upgrades in place.
    """
    additions = (
        ("jobs", "expected_bytes", "INTEGER"),
        ("media", "expires_at", "INTEGER"),
        ("media", "downloaded_at", "INTEGER"),
        ("media", "thumb", "TEXT"),
    )
    for table, column, decl in additions:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


def media_path(rel):
    """Resolve a stored relative media path to an absolute path, refusing escapes."""
    base = os.path.realpath(state_dir())
    full = os.path.realpath(os.path.join(install_dir(), rel))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("path escapes install dir: %r" % rel)
    return full


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def guess_kind(url):
    low = (url or "").lower()
    if "list=" in low:
        return "playlist"
    if any(d in low for d in ("/playlist", "/@", "/channel/", "/c/", "/user/")):
        return "playlist"
    return "video"


def thumb_url(video_id):
    return "https://i.ytimg.com/vi/%s/mqdefault.jpg" % video_id


def safe_name(title, fallback="media", maxlen=80):
    """Filesystem-safe name that still reads like the video title."""
    if not title:
        title = fallback
    title = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "", title)
    title = re.sub(r"\s+", " ", title).strip().strip(".")
    if not title:
        title = fallback
    if len(title) > maxlen:
        title = title[:maxlen].rstrip()
    return title


def now():
    return int(time.time())


def human_bytes(n):
    if not n:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    val = float(n)
    for unit in units:
        if val < 1024 or unit == units[-1]:
            return ("%.1f %s" % (val, unit)).replace(".0 ", " ")
        val /= 1024
    return "%d B" % n


def human_eta(seconds):
    if seconds is None or seconds < 0:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)