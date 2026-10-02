/* LocalYTManager front end.
 *
 * No framework, no build step: the Pi serves these static files as-is.
 * Styling comes from theme.css (Patio, shared with PiHome) plus app.css.
 *
 * State model: the Pi owns everything. This page never holds a download in
 * progress -- it POSTs a request, then polls /api/jobs. That is what lets the
 * user close the tab, refresh, or open another device entirely without
 * disturbing a transfer.
 *
 * LocalYTManager by Harsh Gandhi (harshgandhi.com) / Buho Smart Tools
 * (buho.co.in). SPDX-License-Identifier: GPL-3.0-or-later
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

function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = Number(n), i = 0;
  if (!v) return "0 B";
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

/* The reja mark for a state. Colour alone never carries state: the mark and
   the text label always accompany it. */
const REJA = {
  ok: "reja-ok", busy: "reja-busy", warn: "reja-warn",
  down: "reja-down", unknown: "reja-unknown",
};
const JOB_STATE = {
  queued: "warn", running: "busy", done: "ok",
  error: "down", cancelled: "unknown",
};

function reja(stateName, cls) {
  const id = REJA[stateName] || REJA.unknown;
  return `<svg class="${cls || ""}" width="20" height="20" aria-hidden="true"><use href="#${id}"/></svg>`;
}

/* ------------------------------------------------------------------ */
/* toast                                                               */
/* ------------------------------------------------------------------ */

let toastTimer = null;
function toast(msg, isErr) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "ytm-toast" + (isErr ? " ytm-toast--err" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
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
/* tabs                                                                */
/* ------------------------------------------------------------------ */

const tabs = $$(".ytm-tab");

function selectTab(name, focus) {
  tabs.forEach((t) => {
    const on = t.dataset.tab === name;
    t.setAttribute("aria-selected", on ? "true" : "false");
    if (on && focus) t.focus();
  });
  ["download", "library", "activity"].forEach((n) => {
    $(`#panel-${n}`).hidden = n !== name;
  });
  if (name === "library") loadLibrary();
  if (name === "activity") pollJobs(true);
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  // Arrow-key navigation, which is what a tablist is expected to support.
  tab.addEventListener("keydown", (e) => {
    const i = tabs.indexOf(tab);
    let next = null;
    if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
    else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (e.key === "Home") next = tabs[0];
    else if (e.key === "End") next = tabs[tabs.length - 1];
    if (next) { e.preventDefault(); selectTab(next.dataset.tab, true); }
  });
});

/* ------------------------------------------------------------------ */
/* status + polling                                                    */
/* ------------------------------------------------------------------ */

function renderStatus(st) {
  if (!st) return;
  state.status = st;

  $("#retention-hint").textContent = st.retention_days;
  $("#retention-note").textContent = `kept ${st.retention_days} days`;

  const chip = $("#chip-retention");
  chip.textContent = `${st.retention_days} day retention`;

  const svc = $("#chip-service");
  svc.textContent = `port ${st.port}`;
  svc.className = "patio-chip patio-chip--live";

  $("#sub-status").textContent =
    `yt-dlp ${st.yt_dlp} on port ${st.port} — files stay on this Pi`;

  $("#foot-ytdlp").textContent = st.yt_dlp || "—";
  $("#foot-disk").textContent = st.free_disk ? fmtBytes(st.free_disk.free) : "—";
}

function renderCounts() {
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  $("#count-activity").textContent = active;
  $("#count-library").textContent = state.library.length;
  $("#foot-library").textContent = state.library.length;
}

async function refreshAll() {
  try {
    const data = await api("/api/state");
    renderStatus(data.status);
    state.jobs = data.jobs || [];
    state.library = data.library || [];
    renderJobs();
    renderLibrary();
    renderCounts();
  } catch (e) {
    $("#sub-status").textContent = "cannot reach the Pi — is the service running?";
    $("#chip-service").textContent = "unreachable";
    $("#chip-service").className = "patio-chip patio-chip--down";
  }
}

async function loadLibrary() {
  try {
    const data = await api("/api/library");
    state.library = data.items || [];
    renderLibrary();
    renderCounts();
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
    renderCounts();
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
/* scan                                                                */
/* ------------------------------------------------------------------ */

function setScanStatus(msg, cls) {
  const el = $("#scan-status");
  if (!msg) { el.hidden = true; el.textContent = ""; return; }
  el.className = "ytm-msg" + (cls ? " ytm-msg--" + cls : "");
  el.textContent = msg;
  el.hidden = false;
}

$("#btn-scan").addEventListener("click", doScan);
$("#url").addEventListener("keydown", (e) => { if (e.key === "Enter") doScan(); });

async function doScan() {
  const url = $("#url").value.trim();
  if (!url) { toast("Paste a URL first", true); return; }

  const btn = $("#btn-scan");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  setScanStatus("Scanning with yt-dlp. This Pi is slow, so give it a few seconds.", "busy");
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
    btn.disabled = false;
    btn.textContent = "Scan";
  }
}

/* ---------------- single video ---------------- */

function renderVideo(res) {
  const audio = res.audio || [];
  const video = res.video || [];

  const facts = [
    res.uploader && `<span class="patio-chip">${esc(res.uploader)}</span>`,
    res.duration && `<span class="patio-chip">${esc(fmtDuration(res.duration))}</span>`,
    `<span class="patio-chip">${audio.length} audio · ${video.length} video</span>`,
  ].filter(Boolean).join("");

  $("#result").innerHTML = `
    <div class="patio-panel patio-cornice ytm-result">
      <div class="ytm-vhead">
        <img class="ytm-vthumb" src="${esc(res.thumbnail || "")}" alt=""
             onerror="this.style.visibility='hidden'">
        <div class="ytm-vmeta">
          <h2 class="ytm-vtitle" title="${esc(res.title)}">${esc(res.title)}</h2>
          <div class="ytm-vfacts">${facts}</div>
        </div>
      </div>
      <div class="ytm-switch" role="tablist" aria-label="Media kind">
        <button class="ytm-switch__btn" role="tab" data-kind="audio" aria-selected="false">
          <span class="ytm-switch__name">Audio</span>
          <span class="ytm-switch__count">${audio.length} option${audio.length === 1 ? "" : "s"}</span>
        </button>
        <button class="ytm-switch__btn" role="tab" data-kind="video" aria-selected="false">
          <span class="ytm-switch__name">Video</span>
          <span class="ytm-switch__count">${video.length} option${video.length === 1 ? "" : "s"}</span>
        </button>
      </div>
      <div id="fmt-area"></div>
    </div>`;

  $$(".ytm-switch__btn").forEach((b) => b.addEventListener("click", () => {
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
  $$(".ytm-switch__btn").forEach((b) =>
    b.setAttribute("aria-selected", b.dataset.kind === state.kind ? "true" : "false"));

  const list = state.kind === "audio" ? (res.audio || []) : (res.video || []);
  const area = $("#fmt-area");
  if (!area) return;

  if (!list.length) {
    area.innerHTML = `
      <div class="ytm-options">
        <p class="ytm-hint">No ${esc(state.kind)} options were offered for this video.</p>
      </div>`;
    return;
  }

  const rows = list.map((f, i) => {
      const sel = state.selection && state.selection.format_id === f.format_id;
      const size = f.size ? fmtBytes(f.size) : "size unknown";
      // Audio: bitrate leads, and the container is the detail. The size goes in
      // the right-hand column, so it is NOT repeated in the meta line.
      // Video: the resolution IS the headline, so it leads and the fps follows;
      // most YouTube video formats report no size at all, so the right-hand
      // column carries the container there instead.
      const audio = state.kind === "audio";
      const name = audio
        ? `${esc(f.bitrate_label || "Audio")} &middot; ${esc(f.ext)}`
        : `${esc(f.quality_label || f.label)}${f.needs_audio ? " + audio" : ""}`;
      const meta = audio ? "—" : (esc(f.ext) + (f.fps >= 50 ? ` &middot; ${f.fps}fps` : ""));
      const right = audio ? size : (f.size ? size : esc(f.ext));
      return `
        <label class="ytm-choice" data-idx="${i}" data-selected="${sel ? "true" : "false"}">
          <input type="radio" name="quality" value="${i}" ${sel ? "checked" : ""}>
          <span class="ytm-choice__text">
            <span class="ytm-choice__name">${name}</span>
            ${meta === "—" ? "" : `<span class="ytm-choice__meta">${meta}</span>`}
          </span>
          <span class="ytm-choice__size">${right}</span>
        </label>`;
    }).join("");

  area.innerHTML = `
    <div class="ytm-options">
      <span class="ytm-options__label">
        Choose ${state.kind === "audio" ? "an" : "a"} ${esc(state.kind)} quality
      </span>
      <div class="ytm-list" role="radiogroup" aria-label="${esc(state.kind)} quality">${rows}</div>
      <div class="ytm-options__foot">
        <button class="ytm-btn" id="btn-download" disabled>
          Download ${esc(state.kind)}
        </button>
        <span class="ytm-options__note" id="opt-note">
          Pick a quality to enable the download.
        </span>
      </div>
    </div>`;

  const choices = $$(".ytm-choice", area);
  const dl = $("#btn-download", area);

  choices.forEach((row) => row.addEventListener("click", () => {
    state.selection = list[Number(row.dataset.idx)];
    choices.forEach((r) => r.setAttribute("data-selected", "false"));
    row.setAttribute("data-selected", "true");
    row.querySelector("input").checked = true;
    if (dl) {
      dl.disabled = false;
      $("#opt-note").textContent = `Ready — ${state.selection.label || ""}`;
    }
  }));

  if (dl) dl.addEventListener("click", () => queueSelection(res, state.selection));
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
    toast(`Queued: ${res.title}`);
    pollJobs(true);
    selectTab("activity");
  } catch (e) {
    toast(e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = `Download ${state.kind}`; }
  }
}

/* ---------------- playlist ---------------- */

function renderPlaylist(res) {
  const entries = res.entries || [];
  $("#result").innerHTML = `
    <div class="patio-panel patio-cornice ytm-result">
      <div class="ytm-panel__head">
        <div>
          <h2 class="ytm-vtitle" style="margin-top:0" title="${esc(res.title)}">${esc(res.title)}</h2>
          <div class="ytm-vfacts">
            ${res.uploader ? `<span class="patio-chip">${esc(res.uploader)}</span>` : ""}
            <span class="patio-chip">${entries.length} video${entries.length === 1 ? "" : "s"}</span>
          </div>
        </div>
        <div class="ytm-pl-tools">
          <button class="ytm-btn ytm-btn--ghost ytm-btn--sm" id="pl-none" type="button">Clear</button>
          <button class="ytm-btn ytm-btn--ghost ytm-btn--sm" id="pl-all" type="button">Select all</button>
        </div>
      </div>

      <p class="ytm-hint" style="margin:0 0 var(--s-3)">
        Tick what you want. Everything is added to the queue and downloads in
        the background — or use <b>Options</b> on one row to pick its exact quality.
      </p>

      <div id="pl-list">${entries.map((e, i) => playlistRow(e, i)).join("")}</div>

      <div class="ytm-pl-actions">
        <select id="pl-kind" aria-label="Download as">
          <option value="audio">Audio only</option>
          <option value="video">Video</option>
        </select>
        <button class="ytm-btn" id="pl-queue" disabled type="button">Queue selected</button>
        <span class="ytm-options__note" id="pl-count">0 selected</span>
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
      toast(`Queued ${data.queued} download${data.queued === 1 ? "" : "s"}`);
      pollJobs(true);
      selectTab("activity");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.textContent = "Queue selected";
      refresh();
    }
  });

  // Per-item: scan one entry and offer its real format list.
  $$(".pl-scan").forEach((b) => b.addEventListener("click", () => {
    const e = entries[Number(b.dataset.i)];
    $("#url").value = e.url || `https://www.youtube.com/watch?v=${e.video_id}`;
    $("#force-playlist").checked = false;
    doScan();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
}

function playlistRow(e, i) {
  return `
    <div class="ytm-plrow">
      <input type="checkbox" id="plcb-${i}">
      <img class="ytm-plthumb" src="${esc(e.thumbnail || "")}" alt=""
           onerror="this.style.visibility='hidden'">
      <div class="ytm-pltext">
        <div class="ytm-plname" title="${esc(e.title)}">${esc(e.title)}</div>
        <div class="ytm-plid">${esc(e.video_id)}</div>
      </div>
      <span class="ytm-pldur">${esc(fmtDuration(e.duration))}</span>
      <button class="ytm-btn ytm-btn--ghost ytm-btn--sm pl-scan" data-i="${i}"
              type="button">Options</button>
    </div>`;
}

/* ------------------------------------------------------------------ */
/* jobs / activity                                                     */
/* ------------------------------------------------------------------ */

function renderJobs() {
  const jobs = state.jobs || [];
  const host = $("#jobs-list");
  if (!host) return;

  if (!jobs.length) {
    host.innerHTML = `<div class="ytm-empty">
      Nothing queued yet. Scan a URL and pick a quality to get started.
    </div>`;
    return;
  }
  host.innerHTML = `<div class="ytm-jobs">${jobs.map(jobRow).join("")}</div>`;
}

function jobRow(j) {
  const st = j.status;
  const pct = Number(j.percent || 0);
  const isLive = st === "running" || st === "queued";
  const noTotal = !j.total_bytes;
  const title = j.title || j.video_id || "download";

  // A live job with no known total gets a travelling bar rather than a
  // fabricated percentage — an invented number would be worse than an honest
  // "I do not know how big this is".
  const indet = isLive && noTotal;
  const width = indet ? "" : `${st === "done" ? 100 : pct}%`;

  let actions = "";
  if (st === "done") {
    actions = `
      <a class="ytm-btn ytm-btn--sm" href="/media/${encodeURI(j.result_path)}?dl=1">Download file</a>
      <button class="ytm-btn ytm-btn--ghost ytm-btn--sm" data-dismiss="${esc(j.id)}"
              type="button">Clear</button>`;
  } else if (isLive) {
    actions = `<button class="ytm-btn ytm-btn--ghost ytm-btn--sm"
        data-cancel="${esc(j.id)}" type="button">Cancel</button>`;
  }

  const bytes = j.total_bytes
    ? `${esc(j.downloaded_human)} / ${esc(j.total_human)}`
    : esc(j.downloaded_human || "");
  const speed = j.speed ? fmtBytes(j.speed) + "/s" : "";
  const eta = (st === "running" && j.eta_human && j.eta_human !== "--")
    ? "ETA " + esc(j.eta_human) : "";
  const figures = [bytes, speed, eta].filter(Boolean).map(esc).join("  ·  ");
  const pctText = (isLive && indet) ? "" : `  ·  ${pct.toFixed(1)}%`;
  // Quality, bytes and speed are separate facts and must be separated. Joining
  // the quality straight onto the byte count rendered "1080p60fps471.6 KiB".
  const parts = [esc(j.quality || ""), figures, pctText]
    .map((s) => s.trim()).filter(Boolean).join("  ·  ");

  const msgCls = (st === "error" || st === "cancelled") ? " ytm-job__msg--err" : "";

  return `
    <article class="ytm-job" data-state="${esc(st)}">
      <img class="ytm-job__thumb"
           src="https://i.ytimg.com/vi/${esc(j.video_id || "")}/mqdefault.jpg" alt=""
           onerror="this.style.visibility='hidden'">
      <div class="ytm-job__body">
        <div class="ytm-job__top">
          <div style="min-width:0">
            <p class="ytm-job__name" title="${esc(title)}">${esc(title)}</p>
            <p class="ytm-job__msg${msgCls}">${esc(j.message || j.phase || "")}</p>
          </div>
          ${reja(JOB_STATE[st], "patio-reja")}
        </div>
        <div class="ytm-bar">
          <div class="ytm-bar__fill${indet ? " ytm-bar__fill--indet" : ""}"
               style="--progress:${width}"></div>
        </div>
        <div class="ytm-job__foot">
          <span class="ytm-job__figures">
            <span class="patio-chip">${esc(j.kind)}</span>
            ${parts}
          </span>
          <span class="ytm-job__actions">${actions}</span>
        </div>
      </div>
    </article>`;
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
    dismiss.closest(".ytm-job").remove();
    renderCounts();
  }
});

/* ------------------------------------------------------------------ */
/* library                                                             */
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

  const totalBytes = (state.library || []).reduce((a, m) => a + (m.bytes || 0), 0);
  const ret = state.status ? state.status.retention_days : 7;
  $("#lib-summary").textContent = state.library.length
    ? `${state.library.length} item${state.library.length === 1 ? "" : "s"}, ` +
      `${fmtBytes(totalBytes)} on disk · erased automatically ${ret} days after download.`
    : "";

  if (!items.length) {
    grid.innerHTML = `<div class="ytm-empty">${
      state.library.length
        ? "Nothing matches that filter."
        : `No downloads yet. Finished files land here and stay for ${ret} days.`
    }</div>`;
    return;
  }

  grid.innerHTML = `<div class="ytm-grid">${items.map(libraryCard).join("")}</div>`;
}

function libraryCard(m) {
  const title = m.title || m.filename || "";
  // Prefer the thumbnail yt-dlp cached on the Pi; fall back to YouTube's.
  const thumb = m.thumb
    ? `/thumb/${encodeURI(m.thumb)}`
    : `https://i.ytimg.com/vi/${encodeURI(m.video_id || "")}/mqdefault.jpg`;
  const fallback = `https://i.ytimg.com/vi/${encodeURI(m.video_id || "")}/mqdefault.jpg`;

  const soon = m.expires_in_days != null && m.expires_in_days <= 1;
  const expiry = m.expires_in_days == null ? "" : (soon
    ? `expires ${m.expires_in_days}d`
    : `${m.expires_in_days}d left`);

  const facts = [m.quality, m.duration ? fmtDuration(m.duration) : ""]
    .filter(Boolean).map(esc).join(" · ");

  return `
    <article class="ytm-card" data-open="${m.id}" tabindex="0" role="button"
             aria-label="Preview ${esc(title)}">
      <div class="ytm-card__thumb">
        <span class="patio-chip ytm-card__kind">${esc(m.kind)}</span>
        <img src="${esc(thumb)}" alt="" loading="lazy"
             onerror="this.src='${esc(fallback)}'">
        <div class="ytm-card__play"><span>&#9654;</span></div>
      </div>
      <div class="ytm-card__body">
        <p class="ytm-card__name" title="${esc(title)}">${esc(title)}</p>
        <div class="ytm-card__facts">
          <span class="ytm-card__size">${esc(m.bytes_human || "")}</span>
          ${expiry ? `<span class="ytm-card__expiry${soon ? " ytm-card__expiry--soon" : ""}">${esc(expiry)}</span>` : ""}
        </div>
        <div class="ytm-card__facts" style="margin-bottom:0">
          <span class="ytm-card__expiry">${facts}</span>
        </div>
        <div class="ytm-card__actions">
          <button class="ytm-btn ytm-btn--sm" data-dl="${esc(m.rel_path)}"
                  type="button">Download</button>
          <button class="ytm-btn ytm-btn--danger ytm-btn--sm" data-del="${m.id}"
                  type="button">Delete</button>
        </div>
      </div>
    </article>`;
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

// Keyboard equivalent of clicking a card.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const card = e.target.closest(".ytm-card[data-open]");
  if (card) { e.preventDefault(); openModal(Number(card.dataset.open)); }
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
/* modal preview                                                       */
/* ------------------------------------------------------------------ */

let lastFocus = null;

function openModal(id) {
  const m = (state.library || []).find((x) => x.id === id);
  if (!m) return;

  const title = m.title || m.filename || "";
  const isVideo = m.kind === "video";
  const src = `/media/${encodeURI(m.rel_path)}`;
  const poster = m.thumb
    ? `/thumb/${encodeURI(m.thumb)}`
    : `https://i.ytimg.com/vi/${encodeURI(m.video_id || "")}/mqdefault.jpg`;

  const player = isVideo
    ? `<video controls preload="metadata" poster="${esc(poster)}">
         <source src="${esc(src)}" type="video/mp4">
       </video>`
    : `<audio controls preload="metadata">
         <source src="${esc(src)}" type="audio/mp4">
       </audio>`;

  const meta = [m.kind, m.quality, m.bytes_human,
                m.duration ? fmtDuration(m.duration) : ""]
               .filter(Boolean).map(esc).join(" · ");

  const el = $("#modal");
  lastFocus = document.activeElement;
  el.innerHTML = `
    <div class="ytm-modal__box">
      <div class="ytm-modal__head">
        <div style="min-width:0">
          <h2 class="ytm-modal__title" id="modal-title" title="${esc(title)}">${esc(title)}</h2>
          <p class="ytm-modal__meta">${meta}</p>
        </div>
        <button class="ytm-btn ytm-btn--ghost ytm-btn--sm" id="modal-close"
                type="button">Close</button>
      </div>
      ${player}
      <div class="ytm-modal__foot">
        <span class="ytm-modal__meta">
          ${m.expires_in_days != null
            ? `auto-deleted in ${m.expires_in_days} day${m.expires_in_days === 1 ? "" : "s"}`
            : ""}
        </span>
        <span style="display:flex; gap: var(--s-2); flex-wrap:wrap">
          <a class="ytm-btn" href="${esc(src)}?dl=1">Download file</a>
          <a class="ytm-btn ytm-btn--ghost" href="${esc(m.source_url)}"
             target="_blank" rel="noreferrer noopener">Open on YouTube</a>
        </span>
      </div>
    </div>`;
  el.hidden = false;

  el.addEventListener("click", (ev) => { if (ev.target === el) closeModal(); });
  $("#modal-close").addEventListener("click", closeModal);
  $("#modal-close").focus();
}

function closeModal() {
  const el = $("#modal");
  if (!el || el.hidden) return;
  el.hidden = true;
  el.innerHTML = "";
  if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  lastFocus = null;
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

/* ------------------------------------------------------------------ */
/* boot                                                                */
/* ------------------------------------------------------------------ */

refreshAll();
pollJobs(true);
setInterval(() => { if (!document.hidden) pollJobs(false); }, 5000);