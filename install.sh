#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Harsh Gandhi (harshgandhi.com) / Buho Smart Tools (buho.co.in)
#
# LocalYTManager is EXPERIMENTAL and provided "as is" with no warranty.
# See LICENSE for the full notice, including the no-authentication warning.
#
# LocalYTManager one-click installer.
#
# Design rules, all of them deliberate:
#   1. IDEMPOTENT. Safe to run any number of times. Re-running never changes the
#      port, never overwrites config/lytm.conf, never re-downloads yt-dlp.
#   2. REUSE FIRST. If yt-dlp, ffmpeg or python3 already exist, use them. We
#      never install a second copy of something that already works.
#   3. NON-DESTRUCTIVE TO OTHERS. This tool never touches /opt/rumblingpodcast,
#      never touches Pi-hole, and only ever writes inside /opt/localytmanager
#      plus its own systemd units.
#   4. EXPLICIT ABOUT COLLISIONS. Ports 80/443/53 (Pi-hole), 8088
#      (RumblingPodcast) are refused before anything is written.
#   5. HONEST ABOUT READ-ONLY FILESYSTEMS. Checks before writing, explains what
#      to do if the rootfs is mounted ro.

set -uo pipefail

APP="LocalYTManager"
INSTALL_DIR="${LYTM_INSTALL_DIR:-/opt/localytmanager}"
SERVICE_USER="${LYTM_USER:-$(id -un)}"
DEFAULT_PORT=8090
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYN=$'\033[36m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$OFF" "$*"; }
die()  { printf '  %sx%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }
step() { printf '\n%s==>%s %s%s%s\n' "$CYN" "$OFF" "$CYN" "$*" "$OFF"; }

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"
[ -z "$SUDO" ] || true
if ! [ "$(id -u)" -eq 0 ] && ! command -v sudo >/dev/null 2>&1; then
  die "needs root or sudo"
fi

# ---------------------------------------------------------------------------
# 0. preflight: never let a read-only rootfs turn into a confusing error later
# ---------------------------------------------------------------------------
step "Preflight"

ROOT_OPTS="$(findmnt -no OPTIONS / 2>/dev/null || echo '')"
case "$ROOT_OPTS" in
  *ro*) die "the root filesystem is mounted read-only ($(findmnt -no SOURCE /)).
     Reboot and check the boot partition, then re-run. Nothing was changed." ;;
esac

if [ -n "$ROOT_OPTS" ]; then
  ok "root filesystem is writable ($ROOT_OPTS)"
fi

# A 30MB /tmp breaks the PyInstaller yt-dlp build (it self-extracts ~40MB).
TMP_FREE_KB="$(df -Pk /tmp 2>/dev/null | awk 'NR==2{print $4}')"
if [ -n "${TMP_FREE_KB:-}" ] && [ "$TMP_FREE_KB" -lt 262144 ] 2>/dev/null; then
  warn "/tmp has only $((TMP_FREE_KB/1024))MB -- this tool sets its own TMPDIR instead"
else
  ok "/tmp has adequate space"
fi

# ---------------------------------------------------------------------------
# 1. dependencies: reuse whatever is already installed
# ---------------------------------------------------------------------------
step "Checking dependencies"

need_sudo() { [ "$(id -u)" -eq 0 ] && printf '' || printf 'sudo'; }

# The PyInstaller yt-dlp build self-extracts ~40MB into TMPDIR on every run.
# /tmp is a 30MB tmpfs on this Pi, so probe the version with a TMPDIR that has
# room, or the probe returns an empty string and looks like a broken install.
probe_ytdlp_version() {
  local probe_tmp="$INSTALL_DIR/run/tmp"
  mkdir -p "$probe_tmp" 2>/dev/null
  TMPDIR="$probe_tmp" TMP="$probe_tmp" TEMP="$probe_tmp" \
    "$1" --version 2>/dev/null | head -1
}

# python3
PYTHON=""
for cand in /usr/bin/python3 python3.11 python3.12 python3.13 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PYTHON="$(command -v "$cand")"; break; fi
done
if [ -n "$PYTHON" ]; then
  PYVER="$("$PYTHON" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  if "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' 2>/dev/null; then
    ok "python3 $PYVER at $PYTHON (reused)"
  else
    die "python3 at $PYTHON is too old ($PYVER); need 3.8+"
  fi
else
  warn "python3 missing -- attempting apt install"
  apt-get install -y python3 >&2 || die "could not install python3"
  PYTHON="$(command -v python3)"
  ok "python3 installed"
fi

# ffmpeg -- only needed for merging video+audio and thumbnail conversion
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1) (reused)"
  HAVE_FFMPEG=1
else
  HAVE_FFMPEG=0
  if apt-get install -y ffmpeg >&2; then
    ok "ffmpeg installed"
    HAVE_FFMPEG=1
  else
    warn "ffmpeg unavailable -- video merging and jpg thumbnails will fail"
  fi
fi

# yt-dlp -- reuse in priority order, download only as a last resort
YTDLP=""
YTDLP_SOURCE=""
for cand in "$INSTALL_DIR/bin/yt-dlp" /usr/local/bin/yt-dlp /usr/bin/yt-dlp; do
  if [ -x "$cand" ]; then YTDLP="$cand"; YTDLP_SOURCE="existing"; break; fi
done
# RumblingPodcast already ships a working yt-dlp. Symlink to it rather than
# downloading a second 40MB binary -- and never write into its directory.
if [ -z "$YTDLP" ] && [ -x /opt/rumblingpodcast/bin/yt-dlp ]; then
  YTDLP="/opt/rumblingpodcast/bin/yt-dlp"; YTDLP_SOURCE="rumblingpodcast"
fi
if [ -z "$YTDLP" ] && command -v yt-dlp >/dev/null 2>&1; then
  YTDLP="$(command -v yt-dlp)"; YTDLP_SOURCE="path"
fi
if [ -z "$YTDLP" ]; then
  warn "yt-dlp not found -- downloading a standalone build"
  mkdir -p "$INSTALL_DIR/bin" 2>/dev/null
  TMPD="$(mktemp -d)"
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64) YT_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_aarch64" ;;
    armv7l)  YT_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux_armv7l" ;;
    *)       die "unsupported architecture: $ARCH" ;;
  esac
  if curl -fSL --retry 3 -o "$TMPD/yt-dlp" "$YT_URL"; then
    mkdir -p "$INSTALL_DIR/bin"
    install -m 0755 "$TMPD/yt-dlp" "$INSTALL_DIR/bin/yt-dlp"
    YTDLP="$INSTALL_DIR/bin/yt-dlp"; YTDLP_SOURCE="downloaded"
  else
    rm -rf "$TMPD"
    die "could not download yt-dlp"
  fi
  rm -rf "$TMPD"
fi
ok "yt-dlp $(probe_ytdlp_version "$YTDLP") via $YTDLP_SOURCE"
[ "$YTDLP_SOURCE" = "downloaded" ] || ok "yt-dlp reused, nothing overwritten"

# ---------------------------------------------------------------------------
# 2. port: chosen once, recorded, never changed again
# ---------------------------------------------------------------------------
step "Reserving a port"

CONFIG_FILE="$INSTALL_DIR/config/lytm.conf"
mkdir -p "$INSTALL_DIR/config" 2>/dev/null

port_is_free() {
  local p="$1"
  ! (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && return 0 || return 1
}

if [ -f "$CONFIG_FILE" ]; then
  # Config already exists -- this is a re-install. Read the port, do not touch it.
  EXISTING_PORT="$(sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' "$CONFIG_FILE" | head -1)"
  if [ -n "$EXISTING_PORT" ]; then
    # A re-install keeps the port it was given the first time. A live listener is
    # acceptable here because that is this tool's own previous run.
    PORT="$EXISTING_PORT"
    if port_is_free "$PORT" || systemctl is-active --quiet localytmanager.service 2>/dev/null; then
      ok "reusing port $PORT from existing config (never changes)"
    else
      warn "port $PORT is busy but is your configured port -- keeping it anyway"
    fi
  else
    PORT="$DEFAULT_PORT"
  fi
else
  PORT="$DEFAULT_PORT"
  # Find a free port near the default, but never a port we know Pi-hole or
  # RumblingPodcast owns.
  RESERVED="53 80 443 8088 8081 8082"
  CAND="$PORT"
  while :; do
    TAKEN=0
    for r in $RESERVED; do [ "$CAND" = "$r" ] && TAKEN=1; done
    if [ "$TAKEN" -eq 0 ] && port_is_free "$CAND"; then break; fi
    CAND=$((CAND+1))
  done
  PORT="$CAND"
fi

# Refuse loudly rather than colliding with critical infrastructure.
for guard in "53:Pi-hole DNS" "80:Pi-hole HTTP" "443:Pi-hole HTTPS" "8088:RumblingPodcast"; do
  p="${guard%%:*}"; who="${guard#*:}"
  if [ "$PORT" = "$p" ]; then
    die "configured port $p belongs to $who -- edit $CONFIG_FILE and pick another"
  fi
done
if ! port_is_free "$PORT"; then
  # Either our own service is already up (fine) or something else took it.
  if systemctl is-active --quiet localytmanager.service 2>/dev/null; then
    ok "port $PORT already served by LocalYTManager itself"
  else
    die "port $PORT is in use by something else. Edit $CONFIG_FILE to change it."
  fi
else
  ok "port $PORT is free"
fi
say "  ${DIM}web UI will be at http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT/${OFF}"

# ---------------------------------------------------------------------------
# 3. directory layout
# ---------------------------------------------------------------------------
step "Installing to $INSTALL_DIR"

$SUDO mkdir -p \
  "$INSTALL_DIR"/{bin,lib,config,web,systemd,logs,run/tmp} \
  "$INSTALL_DIR/data"/{media,thumbs}

# sudo creates directories as root, which would leave the service user unable to
# write. Hand ownership over immediately, before the writability check.
$SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" 2>/dev/null

if [ ! -w "$INSTALL_DIR" ]; then
  die "$INSTALL_DIR is not writable by $SERVICE_USER -- check ownership"
fi

# Code and web assets: always refreshed (they are our own files).
for f in bin/server.py bin/lytmctl lib/lytm_common.py lib/lytm_scan.py \
         lib/lytm_worker.py web/index.html web/app.js web/style.css; do
  if [ -f "$REPO_ROOT/$f" ]; then
    $SUDO install -m 0755 "$REPO_ROOT/$f" "$INSTALL_DIR/$f" 2>/dev/null \
      || { $SUDO cp "$REPO_ROOT/$f" "$INSTALL_DIR/$f"; $SUDO chmod 0755 "$INSTALL_DIR/$f"; }
    ok "installed $f"
  fi
done

if [ -f "$REPO_ROOT/VERSION" ]; then
  $SUDO cp "$REPO_ROOT/VERSION" "$INSTALL_DIR/VERSION" 2>/dev/null || true
fi

# The GPL requires the license and notice to travel with the program, so these
# are installed alongside the code rather than living only in the repo.
for doc in LICENSE README.md; do
  if [ -f "$REPO_ROOT/$doc" ]; then
    $SUDO cp "$REPO_ROOT/$doc" "$INSTALL_DIR/$doc" 2>/dev/null \
      && $SUDO chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/$doc" 2>/dev/null
    ok "installed $doc"
  fi
done

# Config: created once, then never touched again.
if [ -f "$CONFIG_FILE" ]; then
  ok "config preserved (port $PORT, your settings intact)"
else
  if [ -f "$REPO_ROOT/config/lytm.conf" ]; then
    $SUDO cp "$REPO_ROOT/config/lytm.conf" "$CONFIG_FILE"
    # Apply the port we actually resolved.
    $SUDO sed -i "s/^port = .*/port = $PORT/" "$CONFIG_FILE"
    ok "config created with port $PORT"
  else
    die "config/lytm.conf missing from the repo"
  fi
fi

# yt-dlp: link, never overwrite a working binary.
if [ "$YTDLP" != "$INSTALL_DIR/bin/yt-dlp" ]; then
  if [ ! -e "$INSTALL_DIR/bin/yt-dlp" ]; then
    $SUDO ln -sf "$YTDLP" "$INSTALL_DIR/bin/yt-dlp" && ok "linked bin/yt-dlp -> $YTDLP"
  else
    ok "bin/yt-dlp already present, left alone"
  fi
fi

# ---------------------------------------------------------------------------
# 4. database + directories
# ---------------------------------------------------------------------------
step "Initialising storage"
LYTM_HOME="$INSTALL_DIR" "$PYTHON" -c "
import sys; sys.path.insert(0, '$INSTALL_DIR/lib')
import lytm_common as C, os
C.init_db()
cfg = C.load_config()
for d in (C.run_dir(), C.tmp_dir(), C.log_dir(),
          os.path.join(C.install_dir(), cfg['media_dir']),
          os.path.join(C.install_dir(), cfg['thumb_dir'])):
    os.makedirs(d, exist_ok=True)
print('  database + directories ready')
" || die "storage initialisation failed"

# ---------------------------------------------------------------------------
# 5. systemd units
# ---------------------------------------------------------------------------
step "Installing systemd units"
for unit in localytmanager.service localytmanager-worker.service \
            localytmanager-retention.service localytmanager-retention.timer; do
  if [ -f "$REPO_ROOT/systemd/$unit" ]; then
    $SUDO cp "$REPO_ROOT/systemd/$unit" "/etc/systemd/system/$unit"
    ok "installed $unit"
  fi
done
$SUDO systemctl daemon-reload
$SUDO ln -sf "$INSTALL_DIR/bin/lytmctl" /usr/local/bin/lytmctl 2>/dev/null \
  && ok "linked /usr/local/bin/lytmctl"
ok "chown $INSTALL_DIR to $SERVICE_USER"
$SUDO chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 6. start
# ---------------------------------------------------------------------------
step "Starting services"
$SUDO systemctl enable localytmanager.service localytmanager-worker.service \
                      localytmanager-retention.timer >/dev/null 2>&1
# --now matters for the timer: `enable` alone leaves it inactive, so the hourly
# expiry sweep would silently never run.
$SUDO systemctl start localytmanager-retention.timer
$SUDO systemctl restart localytmanager-worker.service
$SUDO systemctl restart localytmanager.service
sleep 2

UI_STATE="$(systemctl is-active localytmanager.service 2>/dev/null)"
WK_STATE="$(systemctl is-active localytmanager-worker.service 2>/dev/null)"
TM_STATE="$(systemctl is-active localytmanager-retention.timer 2>/dev/null)"
[ "$UI_STATE" = "active" ] && ok "web UI active" || { $SUDO journalctl -u localytmanager.service -n 20 --no-pager; die "web UI failed to start"; }
[ "$WK_STATE" = "active" ] && ok "worker active" || { $SUDO journalctl -u localytmanager-worker.service -n 20 --no-pager; die "worker failed to start"; }
[ "$TM_STATE" = "active" ] && ok "retention timer active (hourly auto-delete)" \
  || warn "retention timer is NOT active -- auto-delete will not run.
     Fix: sudo systemctl enable --now localytmanager-retention.timer"

# Warm the yt-dlp version cache once, so the first page load is not the thing
# that pays the ~14s PyInstaller self-extract cost.
sudo -u "$SERVICE_USER" env LYTM_HOME="$INSTALL_DIR" "$PYTHON" -c "
import sys; sys.path.insert(0, '$INSTALL_DIR/lib')
import lytm_common as C
C.ytdlp_version()
" >/dev/null 2>&1 && ok "yt-dlp version cached" || warn "version cache warm failed (harmless)"

# Health check against the real socket.
if curl -fsS --max-time 20 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  ok "API responding on 127.0.0.1:$PORT"
else
  warn "API did not answer on port $PORT -- check: sudo journalctl -u localytmanager -f"
fi

# ---------------------------------------------------------------------------
# 7. verify we did not disturb anything else
# ---------------------------------------------------------------------------
step "Verifying other services are untouched"
if systemctl is-active --quiet rumblingpodcast-worker.service 2>/dev/null; then
  ok "RumblingPodcast worker still active"
else
  warn "RumblingPodcast worker is NOT active (was it stopped before this install?)"
fi
if command -v pihole >/dev/null 2>&1 && pihole status 2>/dev/null | grep -q "blocking is enabled"; then
  ok "Pi-hole still blocking"
else
  warn "Pi-hole status could not be confirmed"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

$(printf '%s%s installed.%s' "$GRN" "$APP" "$OFF")

  Web UI      http://$IP:$PORT/
  Control     sudo lytmctl status | sudo lytmctl logs | sudo lytmctl queue
  Retention   downloads auto-erased after $(sed -n 's/^[[:space:]]*retention_days[[:space:]]*=[[:space:]]*\([0-9]*\).*/\1/p' "$CONFIG_FILE" | head -1) days

  Re-running install.sh is safe: the port and config are preserved.

EOF