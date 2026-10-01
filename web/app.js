/* LocalYTManager front end.
 *
 * No framework, no build step: the Pi serves these two static files as-is.
 *
 * State model: the Pi owns everything. This page never holds a download in
 * progress -- it POSTs a request, then polls /api/jobs. That is what lets the
 * user close the tab, refresh, or open another device entirely without
 * disturbing a transfer.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  status: null,
  jobs: [],
  library: [],
  scan: null,        // last scan result
  kind: null,        // active format tab: 'audio' | 'video'
  selection: null,   // selected format object for the active kind
  pollTimer: null,
};

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

function fmtDuration(sec) {
  if (!sec && sec !== 0) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

let toastTimer = null;
function toast(msg, isErr) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({
    headers: { "Content-Type": "application/json" },
  }, opts));
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok || (data && data.ok === false)) {
    const msg = (data && data.error) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

/* ------------------------------------------------------------------ */
/* tabs                                                               */
/* ------------------------------------------------------------------ */

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`#panel-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "library") loadLibrary();
    if (tab.dataset.tab === "activity") pollJobs(true);
  });
});

/* ------------------------------------------------------------------ */
/* status + polling                                                   */
/* ------------------------------------------------------------------ */

function renderStatus(st) {
  if (!st) return;
  state.status = st;
  $("#retention-hint").textContent = st.retention_days;
  const free = st.free_disk ? ` · ${fmtBytes(st.free_disk.free)} free` : "";
  $("#sub-status").textContent =
    `yt-dlp ${st.yt_dlp} · kept ${st.retention_days} days · port ${st.port}${free}`;
}

function fmtBytes(n) {
  if (!n && n !== 0) return "?";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = Number(n), i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

async function refreshAll() {
  try {
    const data = await api("/api/state");
    renderStatus(data.status);
    state.jobs = data.jobs || [];
    state.library = data.library || [];
    renderJobs();
    renderLibrary();
  } catch (e) {
    $("#sub-status").textContent = "cannot reach the Pi — is the service running?";
  }
}

async function loadLibrary() {
  try {
    const data = await api("/api/library");
    state.library = data.items || [];
    renderLibrary();
  } catch (e) { /* surfaced by poll */ }
}

async function pollJobs(force) {
  try {
    const data = await api("/api/state");
    renderStatus(data.status);
    state.jobs = data.jobs || [];
    state.library = data.library || [];
    renderJobs();
    renderLibrary();
  } catch (e) {
    if (force) toast("Cannot reach the server", true);
  }
  // 1.2s while something is moving, 4s when idle: the Pi has ~150MB RAM and is
  // also running Pi-hole plus RumblingPodcast, so do not poll needlessly.
  const active = state.jobs.some((j) => j.status === "running" || j.status === "queued");
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(() => pollJobs(false), active ? 1200 : 4000);
}

/* ------------------------------------------------------------------ */
/* scan                                                               */
/* ------------------------------------------------------------------ */

function setScanStatus(msg, cls) {
  const el = $("#scan-status");
  if (!msg) { el.classList.add("hidden"); return; }
  el.className = "scanstatus " + (cls || "");
  el.textContent = msg;
}

$("#btn-scan").addEventListener("click", doScan);
$("#url").addEventListener("keydown", (e) => { if (e.key === "Enter") doScan(); });

async function doScan() {
  const url = $("#url").value.trim();
  if (!url) { toast("Paste a URL first", true); return; }

  $("#btn-scan").disabled = true;
  setScanStatus("Scanning with yt-dlp… this can take a few seconds on this Pi.", "busy");
  $("#result").innerHTML = "";

  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ url, playlist: $("#force-playlist").checked }),
    });
    const res = data.result;
    state.scan = res;
    state.kind = res.kind === "video" ? null : "playlist";
    state.selection = null;
    setScanStatus("");
    if (res.kind === "playlist") renderPlaylist(res);
    else renderVideo(res);
  } catch (e) {
    setScanStatus(e.message, "err");
  } finally {
    $("#btn-scan").disabled = false;
  }
}

/* ---------------- single video ---------------- */

function renderVideo(res) {
  const audio = res.audio || [];
  const video = res.video || [];

  const facts = [
    res.uploader,
    fmtDuration(res.duration),
    `${audio.length} audio / ${video.length} video options`,
  ].filter(Boolean).map(esc).join(" · ");

  const tabs = `
    <div class="kindtabs">
      <button class="kindtab" data-kind="audio">
        🎧 Audio${audio.length ? ` (${audio.length})` : ""}
      </button>
      <button class="kindtab" data-kind="video">
        🎬 Video${video.length ? ` (${video.length})` : ""}
      </button>
    </div>
    <div id="fmt-area"></div>`;

  $("#result").innerHTML = `
    <div class="card">
      <div class="vhead">
        <img class="vthumb" src="${esc(res.thumbnail || "")}"
             alt="" onerror="this.style.visibility='hidden'">
        <div class="vmeta">
          <p class="vtitle">${esc(res.title)}</p>
          <div class="vfacts">${facts}</div>
        </div>
      </div>
      ${tabs}
    </div>`;

  $$(".kindtab").forEach((b) => b.addEventListener("click", () => {
    state.kind = b.dataset.kind;
    state.selection = null;
    renderFormats();
  }));

  if (audio.length || video.length) {
    state.kind = audio.length ? "audio" : "video";
  }
  renderFormats();
}

function renderFormats() {
  const res = state.scan;
  if (!res || res.kind !== "video") return;
  $$(".kindtab").forEach((b) =>
    b.classList.toggle("active", b.dataset.kind === state.kind));

  const list = state.kind === "audio" ? (res.audio || []) : (res.video || []);
  const area = $("#fmt-area");
  if (!area) return;

  if (!list.length) {
    area.innerHTML = `<p class="hint">No ${state.kind} options were offered
      for this video.</p>`;
    return;
  }

  const rows = list.map((f, i) => `
    <label class="qrow" data-idx="${i}">
      <input type="radio" name="quality" value="${i}"
             ${state.selection && state.selection.format_id === f.format_id ? "checked" : ""}>
      <span class="qlabel">${esc(f.label)}</span>
      <span class="qsize">${esc(state.kind === "audio"
        ? (f.size ? fmtBytes(f.size) : "—")
        : (f.quality_label || ""))}</span>
    </label>`).join("");

  area.innerHTML = `
    <div class="qlist">${rows}</div>
    <button class="btn primary" id="btn-download" disabled>
      Download ${esc(state.kind)}
    </button>`;

  area.querySelectorAll(".qrow").forEach((row) => {
    row.addEventListener("click", () => {
      state.selection = list[Number(row.dataset.idx)];
      area.querySelectorAll(".qrow").forEach((r) => r.classList.remove("sel"));
      row.classList.add("sel");
      row.querySelector("input").checked = true;
      $("#btn-download").disabled = false;
    });
  });

  const dl = $("#btn-download");
  if (dl) {
    dl.addEventListener("click", () => queueSelection(res, state.selection));
  }
}

async function queueSelection(res, fmt) {
  if (!fmt) { toast("Pick a quality first", true); return; }
  const btn = $("#btn-download");
  if (btn) { btn.disabled = true; btn.textContent = "Queueing…"; }
  try {
    const data = await api("/api/download", {
      method: "POST",
      body: JSON.stringify({
        url: res.url || $("#url").value.trim(),
        video_id: res.video_id,
        title: res.title,
        kind: state.kind,
        quality: fmt.quality_label || fmt.bitrate_label || fmt.label,
        format_id: fmt.format_id,
        // Pinned at enqueue time so the progress bar has a fixed denominator
        // and never jumps when yt-dlp starts a second stream for the merge.
        // Only trust a size the scan could actually establish; a guessed total
        // shows a full bar while bytes are still arriving.
        expected_bytes: fmt.expected_bytes
          || (state.kind === "audio" ? (fmt.size || null) : null),
      }),
    });
    toast(`Queued: ${res.title} (${data.queued})`);
    pollJobs(true);
  } catch (e) {
    toast(e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = `Download ${state.kind}`; }
  }
}

/* ---------------- playlist ---------------- */

function renderPlaylist(res) {
  const entries = res.entries || [];
  $("#result").innerHTML = `
    <div class="card">
      <div class="plhead">
        <div>
          <h2>${esc(res.title)}</h2>
          <div class="vfacts">${esc(res.uploader || "")} ·
            ${entries.length} video${entries.length === 1 ? "" : "s"}</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn" id="pl-none">Clear</button>
          <button class="btn" id="pl-all">Select all</button>
        </div>
      </div>
      <p class="hint" style="margin:0 0 12px">
        Pick the videos you want now — everything is added to the queue and
        downloads in the background. Or take just one at a time.
      </p>
      <div id="pl-list">
        ${entries.map((e, i) => playlistRow(e, i)).join("")}
      </div>
      <div style="margin-top:16px;display:flex;gap:9px;align-items:center;flex-wrap:wrap">
        <select id="pl-kind" style="flex:0 0 160px">
          <option value="audio">Audio only</option>
          <option value="video">Video</option>
        </select>
        <button class="btn primary" id="pl-queue" disabled>Queue selected</button>
        <span class="hint" id="pl-count" style="margin:0">0 selected</span>
      </div>
    </div>`;

  const boxes = $$("#pl-list input[type=checkbox]");
  const refresh = () => {
    const n = boxes.filter((b) => b.checked).length;
    $("#pl-count").textContent = `${n} selected`;
    $("#pl-queue").disabled = n === 0;
  };
  boxes.forEach((b) => b.addEventListener("change", refresh));

  $("#pl-all").addEventListener("click", () => {
    boxes.forEach((b) => { b.checked = true; });
    refresh();
  });
  $("#pl-none").addEventListener("click", () => {
    boxes.forEach((b) => { b.checked = false; });
    refresh();
  });

  $("#pl-queue").addEventListener("click", async () => {
    const picked = entries.filter((_, i) => boxes[i].checked);
    const kind = $("#pl-kind").value;
    const btn = $("#pl-queue");
    btn.disabled = true;
    btn.textContent = "Queueing…";
    try {
      const data = await api("/api/download", {
        method: "POST",
        body: JSON.stringify({
          kind: "playlist",
          url: $("#url").value.trim(),
          entries: picked,
          quality: kind === "audio" ? "audio (playlist)" : "best video (playlist)",
          format_id: kind === "audio"
            ? "worstaudio[acodec!=none]/bestaudio/best"
            : "bv*+ba/b",
          force_audio: kind === "audio",
        }),
      });
      toast(`Queued ${data.queued} download(s) from the playlist`);
      pollJobs(true);
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.textContent = "Queue selected";
      refresh();
    }
  });

  // Per-item buttons: scan one entry and offer its real format list.
  $$(".pl-scan").forEach((btn) => btn.addEventListener("click", () => {
    const i = Number(btn.dataset.i);
    const e = entries[i];
    $("#url").value = e.url || `https://www.youtube.com/watch?v=${e.video_id}`;
    $("#force-playlist").checked = false;
    doScan();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
}

function playlistRow(e, i) {
  return `
    <div class="plrow">
      <input type="checkbox" id="plcb-${i}">
      <img class="plthumb" src="${esc(e.thumbnail || "")}"
           alt="" onerror="this.style.visibility='hidden'">
      <div class="pltitle">
        ${esc(e.title)}
        <div style="color:var(--muted);font-size:12px">${esc(e.video_id)}</div>
      </div>
      <span class="pldur">${esc(fmtDuration(e.duration))}</span>
      <button class="btn sm pl-scan" data-i="${i}">Options</button>
    </div>`;
}

/* ------------------------------------------------------------------ */
/* jobs / activity                                                    */
/* ------------------------------------------------------------------ */

function renderJobs() {
  const jobs = state.jobs || [];
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued");
  $("#job-count").textContent = active.length;

  const host = $("#jobs-list");
  if (!host) return;
  if (!jobs.length) {
    host.innerHTML = `<div class="empty">Nothing queued yet.
      Scan a URL and pick a quality to get started.</div>`;
    return;
  }

  host.innerHTML = jobs.map(jobRow).join("");
}

function jobRow(j) {
  const st = j.status;
  const pct = Number(j.percent || 0);
  const isLive = st === "running" || st === "queued";
  const noTotal = !j.total_bytes;

  let cls = "";
  if (st === "done") cls = "done";
  else if (st === "error" || st === "cancelled") cls = "err";

  // Live jobs with an unknown total get a travelling bar instead of a fake
  // percentage — an invented number would be worse than an honest indeterminate.
  const barCls = isLive && noTotal ? "indet" : cls;
  const width = (isLive && noTotal) ? "" : `${st === "done" ? 100 : pct}%`;

  let foot = "";
  if (st === "done") {
    const name = (j.result_path || "").split("/").pop();
    foot = `
      <a class="btn sm" href="/media/${encodeURI(j.result_path)}?dl=1">Download file</a>
      <button class="btn sm" data-dismiss="${esc(j.id)}">Clear</button>`;
  } else if (st === "queued") {
    foot = `<button class="btn sm" data-cancel="${esc(j.id)}">Cancel</button>`;
  } else if (isLive) {
    foot = `<button class="btn sm" data-cancel="${esc(j.id)}">Cancel</button>`;
  }

  const bytes = j.total_bytes
    ? `${esc(j.downloaded_human)} / ${esc(j.total_human)}`
    : esc(j.downloaded_human || "");

  const speed = j.speed ? `${fmtBytes(j.speed)}/s` : "";
  const eta = (st === "running" && j.eta_human && j.eta_human !== "--")
    ? `ETA ${esc(j.eta_human)}` : "";

  return `
    <div class="jrow">
      <img class="jthumb" src="https://i.ytimg.com/vi/${esc(j.video_id || "")}/mqdefault.jpg"
           alt="" onerror="this.style.visibility='hidden'">
      <div class="jbody">
        <p class="jtitle">${esc(j.title || j.video_id || "download")}</p>
        <p class="jmsg">${esc(j.message || j.phase || "")}</p>
        <div class="bar"><div class="barfill ${barCls}" style="width:${width}"></div></div>
        <div class="jfoot">
          <span>
            <span class="badge ${esc(j.kind)}">${esc(j.kind)}</span>
            ${esc(j.quality || "")} · ${bytes}
            ${speed ? " · " + speed : ""} ${eta ? " · " + eta : ""}
            ${(!isLive && st === "done") ? "" :
              (isLive && noTotal ? "" : " · " + pct.toFixed(1) + "%")}
          </span>
          <span>${foot}</span>
        </div>
      </div>
    </div>`;
}

document.addEventListener("click", async (e) => {
  const cancel = e.target.closest("[data-cancel]");
  if (cancel) {
    try {
      await api("/api/cancel", {
        method: "POST", body: JSON.stringify({ job_id: cancel.dataset.cancel }),
      });
      toast("Cancelled");
      pollJobs(true);
    } catch (err) { toast(err.message, true); }
    return;
  }
  const dismiss = e.target.closest("[data-dismiss]");
  if (dismiss) {
    dismiss.closest(".jrow").remove();
  }
});

/* ------------------------------------------------------------------ */
/* library                                                            */
/* ------------------------------------------------------------------ */

function renderLibrary() {
  const grid = $("#library-grid");
  if (!grid) return;
  const q = ($("#lib-search").value || "").toLowerCase();
  const filter = $("#lib-filter").value;
  const items = (state.library || []).filter((m) => {
    if (filter !== "all" && m.kind !== filter) return false;
    if (q && !(m.title || "").toLowerCase().includes(q)) return false;
    return true;
  });

  $("#lib-count").textContent = state.library.length;

  const totalBytes = (state.library || []).reduce((a, m) => a + (m.bytes || 0), 0);
  const ret = state.status ? state.status.retention_days : 7;
  $("#lib-summary").textContent = state.library.length
    ? `${state.library.length} item(s), ${fmtBytes(totalBytes)} on disk · ` +
      `everything is erased automatically ${ret} days after download.`
    : "";

  if (!items.length) {
    grid.innerHTML = `<div class="empty">${
      state.library.length
        ? "Nothing matches that filter."
        : "No downloads yet. Completed files land here and stay available " +
          `for ${ret} days.`
    }</div>`;
    return;
  }

  grid.innerHTML = items.map((m) => {
      // Prefer the thumbnail yt-dlp cached on the Pi; fall back to YouTube's.
      const thumb = m.thumb
        ? `/thumb/${encodeURI(m.thumb)}`
        : `https://i.ytimg.com/vi/${encodeURI(m.video_id || "")}/mqdefault.jpg`;
      const exp = m.expires_in_days == null ? "" :
        (m.expires_in_days <= 1
          ? `<span class="expires">expires ${m.expires_in_days}d</span>`
          : `<span>${m.expires_in_days}d left</span>`);
      return `
        <div class="lcard" data-open="${m.id}">
          <div class="lthumbwrap">
            <img src="${esc(thumb)}" alt=""
                 onerror="this.src='https://i.ytimg.com/vi/${encodeURI(m.video_id || '')}/mqdefault.jpg'">
          <div class="lplay"><span>▶</span></div>
        </div>
        <div class="lbody">
          <p class="ltitle">${esc(m.title || m.filename)}</p>
          <div class="lfacts">
            <span><span class="badge ${esc(m.kind)}">${esc(m.kind)}</span></span>
            <span>${esc(m.bytes_human || "")}</span>
          </div>
          <div class="lfacts" style="margin-bottom:10px">
            <span>${esc(m.quality || "")} ${m.duration ? "· " + fmtDuration(m.duration) : ""}</span>
            ${exp}
          </div>
          <div class="lactions">
            <button class="btn sm" data-dl="${esc(m.rel_path)}">Download</button>
            <button class="btn sm danger" data-del="${m.id}">Delete</button>
          </div>
        </div>
      </div>`;
  }).join("");
}

$("#lib-search").addEventListener("input", renderLibrary);
$("#lib-filter").addEventListener("change", renderLibrary);

document.addEventListener("click", async (e) => {
  const dl = e.target.closest("[data-dl]");
  if (dl) {
    e.stopPropagation();
    window.location.href = `/media/${encodeURI(dl.dataset.dl)}?dl=1`;
    return;
  }
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    if (!confirm("Delete this file from the Pi?")) return;
    try {
      await api("/api/delete", {
        method: "POST", body: JSON.stringify({ id: Number(del.dataset.del) }),
      });
      toast("Deleted");
      loadLibrary();
      pollJobs(true);
    } catch (err) { toast(err.message, true); }
    return;
  }
  const card = e.target.closest("[data-open]");
  if (card) openModal(Number(card.dataset.open));
});

$("#btn-clean-expired").addEventListener("click", async () => {
  try {
    const data = await api("/api/clean", { method: "POST", body: "{}" });
    toast(data.removed ? `Removed ${data.removed} expired file(s)` : "Nothing expired yet");
    loadLibrary();
  } catch (e) { toast(e.message, true); }
});

$("#btn-clean-all").addEventListener("click", async () => {
  if (!confirm("Delete every downloaded file and thumbnail?")) return;
  try {
    const data = await api("/api/clean", {
      method: "POST", body: JSON.stringify({ force: true }),
    });
    toast(`Removed ${data.removed} item(s)`);
    loadLibrary();
  } catch (e) { toast(e.message, true); }
});

/* ------------------------------------------------------------------ */
/* modal preview                                                      */
/* ------------------------------------------------------------------ */

function openModal(id) {
  const m = (state.library || []).find((x) => x.id === id);
  if (!m) return;
  closeModal();

  const isVideo = m.kind === "video";
  const src = `/media/${encodeURI(m.rel_path)}`;
  const player = isVideo
    ? `<video controls preload="metadata" poster="${esc(
        m.thumb ? `/thumb/${m.thumb}` : `https://i.ytimg.com/vi/${esc(m.video_id || "")}/mqdefault.jpg`
      )}"><source src="${esc(src)}" type="video/mp4"></video>`
    : `<audio controls preload="metadata" style="width:100%">
         <source src="${esc(src)}" type="audio/mp4">
       </audio>`;

  const el = document.createElement("div");
  el.className = "modal";
  el.id = "modal";
  el.innerHTML = `
    <div class="modalbox">
      <div class="modalhead">
        <div>
          <h2>${esc(m.title || m.filename)}</h2>
          <div class="mfacts">${esc(m.kind)} · ${esc(m.quality || "")} ·
            ${esc(m.bytes_human || "")} ${m.duration ? "· " + fmtDuration(m.duration) : ""}</div>
        </div>
        <button class="btn sm" id="modal-close">Close</button>
      </div>
      ${player}
      <div class="mfoot">
        <span class="mfacts">
          ${m.expires_in_days != null
            ? `auto-deleted in ${m.expires_in_days} day(s)` : ""}
        </span>
        <span style="display:flex;gap:9px">
          <a class="btn primary" href="${esc(src)}?dl=1">Download file</a>
          <a class="btn" href="${esc(m.source_url)}" target="_blank"
             rel="noreferrer noopener">Open on YouTube</a>
        </span>
      </div>
    </div>`;
  document.body.appendChild(el);
  el.addEventListener("click", (ev) => { if (ev.target === el) closeModal(); });
  $("#modal-close").addEventListener("click", closeModal);
}

function closeModal() {
  const el = $("#modal");
  if (el) el.remove();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

/* ------------------------------------------------------------------ */
/* boot                                                               */
/* ------------------------------------------------------------------ */

refreshAll();
pollJobs(true);
setInterval(() => { if (!document.hidden) pollJobs(false); }, 5000);