#!/usr/bin/env python3
"""LocalYTManager HTTP server: JSON API + static UI + media streaming.

Standard library only -- the Pi has no pip, so this deliberately avoids any web
framework. ThreadingHTTPServer keeps a slow download POST from blocking the
progress polling that the browser runs in parallel.

Nothing here requires authentication: the tool binds on the LAN and is meant to
be reached only from a trusted network, exactly as asked.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Harsh Gandhi (harshgandhi.com) / Buho Smart Tools (buho.co.in)
#
# LocalYTManager is EXPERIMENTAL and provided "as is" with no warranty.
# See LICENSE for the full notice, including the no-authentication warning.

import json
import mimetypes
import os
import posixpath
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Imports live in ../lib relative to this file, which is stable whether the
# tool runs from the repo (local testing) or /opt/localytmanager (deployed).
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "lib")
sys.path.insert(0, os.path.abspath(_LIB))

import lytm_common as C          # noqa: E402
import lytm_scan                 # noqa: E402
import lytm_worker                # noqa: E402

WEB_DIR = os.path.join(C.install_dir(), "web")
MAX_BODY = 64 * 1024

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalYTManager/%s" % C.VERSION
    protocol_version = "HTTP/1.1"

    # ---------------- plumbing ----------------

    def log_message(self, fmt, *args):
        # Keep access logs out of stderr noise; errors still surface.
        if not str(args[1] if len(args) > 1 else "").startswith(("2", "3")):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _fail(self, code, msg):
        self._json({"ok": False, "error": msg}, code)

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------------- routing ----------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path, query)
            if path.startswith("/media/"):
                return self._serve_media(path)
            if path.startswith("/thumb/"):
                return self._serve_thumb(path)
            return self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never kill the server thread
            sys.stderr.write("GET %s failed: %r\n" % (path, exc))
            try:
                self._fail(500, "Server error: %s" % exc)
            except Exception:
                pass

    do_HEAD = do_GET

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            if path == "/api/scan":
                return self._api_scan()
            if path == "/api/download":
                return self._api_download()
            if path == "/api/cancel":
                return self._api_cancel()
            if path == "/api/delete":
                return self._api_delete()
            if path == "/api/clean":
                return self._api_clean()
            return self._fail(404, "Unknown endpoint")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("POST %s failed: %r\n" % (path, exc))
            try:
                self._fail(500, "Server error: %s" % exc)
            except Exception:
                pass

    # ---------------- API: GET ----------------

    def _api_get(self, path, query):
        cfg = C.load_config()
        if path == "/api/state":
            return self._json(self._state(cfg))
        if path == "/api/jobs":
            return self._json(self._jobs())
        if path == "/api/library":
            return self._json(self._library(cfg))
        if path == "/api/status":
            return self._json(self._status(cfg))
        if path == "/api/scan":
            url = (query.get("url") or [""])[0]
            want_playlist = (query.get("playlist") or ["0"])[0] in ("1", "true")
            if not url:
                return self._fail(400, "Missing url")
            res, err = lytm_scan.scan(url, cfg=cfg, want_playlist=want_playlist)
            if err:
                return self._fail(400, err)
            return self._json({"ok": True, "result": res})
        return self._fail(404, "Unknown endpoint")

    def _status(self, cfg):
        exe = C.find_ytdlp(cfg)
        return {
            "ok": True,
            "app": C.APP,
            "version": C.VERSION,
            "port": cfg["port"],
            "retention_days": cfg["retention_days"],
            # Cached: probing this costs ~14s on the Pi's CPU.
            "yt_dlp": C.ytdlp_version(cfg),
            "ytdlp_path": exe or "",
            "install_dir": C.install_dir(),
            "media_dir": os.path.join(C.install_dir(), cfg["media_dir"]),
            "free_disk": shutil_free(cfg),
            "tmpdir": C.tmp_dir(),
        }

    def _state(self, cfg):
        """Single call the UI makes on load: library + jobs + status."""
        return {
            "ok": True,
            "status": self._status(cfg),
            "jobs": self._jobs(),
            "library": self._library(cfg)["items"],
        }

    def _jobs(self):
        conn = C.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 60"
            ).fetchall()
            out = []
            for r in rows:
                item = dict(r)
                item["downloaded_human"] = C.human_bytes(item.get("downloaded_bytes"))
                item["total_human"] = C.human_bytes(item.get("total_bytes"))
                item["eta_human"] = C.human_eta(item.get("eta"))
                out.append(item)
            return out
        finally:
            conn.close()

    def _library(self, cfg):
        conn = C.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM media ORDER BY downloaded_at DESC"
            ).fetchall()
            ts = C.now()
            items = []
            stale = []
            for r in rows:
                item = dict(r)
                item["bytes_human"] = C.human_bytes(item.get("bytes"))
                item["expires_in_days"] = (
                    max(0, round((item["expires_at"] - ts) / 86400.0))
                    if item.get("expires_at") else None
                )
                exists = False
                if item.get("rel_path"):
                    try:
                        exists = os.path.isfile(C.media_path(item["rel_path"]))
                    except ValueError:
                        exists = False
                item["exists"] = exists
                # A row whose file vanished (deleted outside the UI, wiped by a
                # cleanup script, lost to a partial disk) would otherwise render
                # as a card that 404s on click. Drop the row so the library
                # always reflects what is actually on disk.
                if not exists:
                    stale.append(item["id"])
                    continue
                items.append(item)
            if stale:
                conn.execute(
                    "DELETE FROM media WHERE id IN (%s)"
                    % ",".join("?" * len(stale)), stale)
                conn.commit()
            return {"ok": True, "items": items, "pruned": len(stale)}
        finally:
            conn.close()

    # ---------------- API: POST ----------------

    def _api_scan(self):
        body = self._body_json()
        url = (body.get("url") or "").strip()
        if not url:
            return self._fail(400, "Enter a YouTube URL first.")
        res, err = lytm_scan.scan(
            url, cfg=C.load_config(), want_playlist=bool(body.get("playlist"))
        )
        if err:
            return self._fail(400, err)
        return self._json({"ok": True, "result": res})

    def _api_download(self):
        body = self._body_json()
        url = (body.get("url") or "").strip()
        if not url:
            return self._fail(400, "Missing url")

        cfg = C.load_config()
        kind = body.get("kind")
        quality = body.get("quality") or ""
        fmt = body.get("format_id") or ""

        if kind == "playlist":
            entries = body.get("entries") or []
            if not entries:
                return self._fail(400, "Playlist has no entries selected.")
            if len(entries) > 200:
                return self._fail(400, "Playlist is too large (%d entries); "
                                     "select fewer than 200." % len(entries))
            job_ids = []
            conn = C.connect()
            try:
                for idx, entry in enumerate(entries):
                    vurl = entry.get("url") or ""
                    if not vurl:
                        continue
                    job_ids.append(lytm_worker.enqueue(
                        entry.get("video_id") or "",
                        entry.get("title") or entry.get("video_id") or "Untitled",
                        vurl, "audio" if body.get("force_audio") else kind,
                        quality or "playlist default", fmt, cfg,
                    ))
                conn.commit()
            finally:
                conn.close()
            return self._json({"ok": True, "queued": len(job_ids), "job_ids": job_ids})

        if kind not in ("audio", "video"):
            return self._fail(400, "Choose audio or video.")
        if not fmt:
            return self._fail(400, "Choose a quality.")

        job_id = lytm_worker.enqueue(
            body.get("video_id") or "", body.get("title") or "", url,
            kind, quality, fmt, cfg,
            expected_bytes=body.get("expected_bytes"),
        )
        return self._json({"ok": True, "queued": 1, "job_ids": [job_id]})

    def _api_cancel(self):
        body = self._body_json()
        job_id = body.get("job_id")
        if not job_id:
            return self._fail(400, "Missing job_id")
        lytm_worker.update_job(
            job_id, status="cancelled", phase="Cancelled",
            message="Cancelled by user. Re-queue it if you still want it.",
        )
        return self._json({"ok": True})

    def _api_delete(self):
        body = self._body_json()
        media_id = body.get("id")
        if not media_id:
            return self._fail(400, "Missing id")
        conn = C.connect()
        try:
            row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
            if not row:
                return self._fail(404, "Not found")
            for rel in (row["rel_path"], row["thumb"]):
                if not rel:
                    continue
                try:
                    full = C.media_path(rel)
                    if os.path.isfile(full):
                        os.remove(full)
                except (ValueError, OSError):
                    pass
            conn.execute("DELETE FROM media WHERE id=?", (media_id,))
            conn.commit()
        finally:
            conn.close()
        return self._json({"ok": True})

    def _api_clean(self):
        """Remove expired files, or everything if force=1."""
        cfg = C.load_config()
        body = self._body_json()
        force = bool(body.get("force"))
        if force:
            conn = C.connect()
            try:
                rows = conn.execute("SELECT * FROM media").fetchall()
                for row in rows:
                    for rel in (row["rel_path"], row["thumb"]):
                        if not rel:
                            continue
                        try:
                            full = C.media_path(rel)
                            if os.path.isfile(full):
                                os.remove(full)
                        except (ValueError, OSError):
                            pass
                conn.execute("DELETE FROM media")
                conn.commit()
                return self._json({"ok": True, "removed": len(rows)})
            finally:
                conn.close()
        removed, expired = lytm_worker.enforce_retention(cfg)
        return self._json({"ok": True, "removed": removed, "expired": expired})

    # ---------------- media delivery ----------------

    def _parse_range(self, header, size):
        """Return (start, end) inclusive, or None."""
        if not header or not header.startswith("bytes="):
            return None
        spec = header[6:].split(",")[0].strip()
        if "-" not in spec:
            return None
        first, _, last = spec.partition("-")
        try:
            if not first:
                length = int(last)
                if length <= 0:
                    return None
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(first)
                end = int(last) if last else size - 1
        except ValueError:
            return None
        if start >= size or start < 0 or end < start:
            return None
        return start, min(end, size - 1)

    def _serve_file(self, full, download_name=None, ctype=None):
        if not os.path.isfile(full):
            return self._fail(404, "File not found")
        size = os.path.getsize(full)
        ctype = ctype or (mimetypes.guess_type(full)[0] or "application/octet-stream")

        range_hdr = self.headers.get("Range")
        span = self._parse_range(range_hdr, size)
        headers = {"Accept-Ranges": "bytes"}
        if download_name:
            safe = download_name.replace('"', "")
            headers["Content-Disposition"] = 'attachment; filename="%s"' % safe

        if span is None:
            headers["Content-Length"] = str(size)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(full, "rb") as fh:
                _copy(fh, self.wfile)
            return

        start, end = span
        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(full, "rb") as fh:
            fh.seek(start)
            _copy(fh, self.wfile, length)

    def _serve_media(self, path):
        # /media/<relpath>            -> inline stream (browser can seek)
        # /media/<relpath>?dl=1       -> attachment download
        rel = path[len("/media/"):]
        if not rel:
            return self._fail(404, "Missing file")
        try:
            full = C.media_path(rel)
        except ValueError:
            return self._fail(400, "Bad path")
        download_name = None
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if (query.get("dl") or ["0"])[0] in ("1", "true"):
            download_name = os.path.basename(full)
        self._serve_file(full, download_name=download_name)

    def _serve_thumb(self, path):
        rel = path[len("/thumb/"):].split("?")[0]
        if not rel:
            return self._fail(404, "Missing thumbnail")
        try:
            full = C.media_path(rel)
        except ValueError:
            return self._fail(400, "Bad path")
        self._serve_file(full, ctype="image/jpeg")

    # ---------------- static UI ----------------

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        clean = posixpath.normpath(path).lstrip("/")
        if clean.startswith("..") or os.path.isabs(clean):
            return self._fail(400, "Bad path")
        full = os.path.realpath(os.path.join(WEB_DIR, clean))
        base = os.path.realpath(WEB_DIR)
        if not full.startswith(base + os.sep) and full != base:
            return self._fail(400, "Bad path")
        if not os.path.isfile(full):
            return self._fail(404, "Not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _copy(fh, wfile, limit=None):
    chunk = 64 * 1024
    remaining = limit
    while True:
        if limit is not None:
            if remaining <= 0:
                break
            want = min(chunk, remaining)
        else:
            want = chunk
        data = fh.read(want)
        if not data:
            break
        wfile.write(data)
        if limit is not None:
            remaining -= len(data)


def shutil_free(cfg):
    path = os.path.join(C.install_dir(), cfg["media_dir"])
    try:
        st = os.statvfs(path if os.path.isdir(path) else C.install_dir())
        return {"total": st.f_blocks * st.f_frsize,
                "free": st.f_bavail * st.f_frsize}
    except OSError:
        return None


def main():
    cfg = C.load_config()
    C.init_db()
    os.makedirs(C.tmp_dir(), exist_ok=True)
    os.makedirs(os.path.join(C.install_dir(), cfg["media_dir"]), exist_ok=True)
    os.makedirs(os.path.join(C.install_dir(), cfg["thumb_dir"]), exist_ok=True)

    port = int(cfg["port"])
    bind = cfg["bind"]
    httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.daemon_threads = True
    sys.stderr.write(
        "%s %s listening on %s:%d (retention %sd)\n"
        % (C.APP, C.VERSION, bind, port, cfg["retention_days"])
    )
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()