/* ============================================================
   NyxDownloader — frontend application (v5)
   Vanilla JS (no build step) + Tailwind via CDN.
   ============================================================ */

"use strict";


// --------------------------------------------------------------
// Platform branding (inline SVG — no CDN dependency)
// --------------------------------------------------------------
const PLATFORMS = {
  tiktok: {
    name: "TikTok",
    icon: (c = "#000") => `
      <svg viewBox="0 0 24 24" fill="${c}" width="15" height="15">
        <path d="M16.6 5.82a4.28 4.28 0 0 1-1.05-2.82h-3.1v12.4a2.59 2.59 0 1 1-2.59-2.59c.27 0 .53.04.78.12V9.75a5.76 5.76 0 1 0 4.96 5.7V9.9a7.35 7.35 0 0 0 4.3 1.38V8.2a4.3 4.3 0 0 1-3.3-2.38Z"/>
      </svg>`,
  },
  instagram: {
    name: "Instagram",
    icon: (c = "#E1306C") => `
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="${c}" stroke-width="2">
        <rect x="3" y="3" width="18" height="18" rx="5.5"/>
        <circle cx="12" cy="12" r="4.2"/>
        <circle cx="17.4" cy="6.6" r="1.15" fill="${c}" stroke="none"/>
      </svg>`,
  },
  snapchat: {
    name: "Snapchat",
    icon: (c = "#F5C518") => `
      <svg viewBox="0 0 24 24" width="15" height="15" fill="${c}">
        <path d="M12 2.5c2.9 0 5.2 2.2 5.2 5.3 0 .5 0 1-.05 1.5.5.1 1-.15 1.3-.15.5 0 .95.3.95.75 0 .55-.7.85-1.35 1.1-.5.2-1.05.45-.9 1.05.4 1.6 1.5 2.9 3.1 3.3.4.1.65.45.5.8-.3.7-1.6.9-2.5 1.05-.15.3-.25.9-.55.9-.45 0-1-.3-1.75-.3-.6 0-1.2.25-1.75.8-1.15 1.15-2 1.15-2.25 1.15s-1.1 0-2.25-1.15c-.55-.55-1.15-.8-1.75-.8-.75 0-1.3.3-1.75.3-.3 0-.4-.6-.55-.9-.9-.15-2.2-.35-2.5-1.05-.15-.35.1-.7.5-.8 1.6-.4 2.7-1.7 3.1-3.3.15-.6-.4-.85-.9-1.05C4.85 10.6 4.15 10.3 4.15 9.75c0-.45.45-.75.95-.75.3 0 .8.25 1.3.15A15 15 0 0 1 6.35 7.8c0-3.1 2.3-5.3 5.2-5.3h.45Z"/>
      </svg>`,
  },
  facebook: {
    name: "Facebook",
    icon: (c = "#1877F2") => `
      <svg viewBox="0 0 24 24" width="15" height="15" fill="${c}">
        <path d="M22 12a10 10 0 1 0-11.56 9.88v-6.99H7.9V12h2.54V9.8c0-2.5 1.5-3.89 3.78-3.89 1.09 0 2.23.2 2.23.2v2.46h-1.26c-1.24 0-1.63.77-1.63 1.56V12h2.78l-.44 2.89h-2.34v6.99A10 10 0 0 0 22 12Z"/>
      </svg>`,
  },
  twitter: {
    name: "X",
    icon: (c = "#000") => `
      <svg viewBox="0 0 24 24" width="14" height="14" fill="${c}">
        <path d="M17.75 3h3.07l-6.7 7.66L22 21h-6.17l-4.84-6.32L5.46 21H2.38l7.17-8.2L2 3h6.33l4.37 5.78L17.75 3Zm-1.08 16.16h1.7L7.4 4.74H5.57L16.67 19.16Z"/>
      </svg>`,
  },
  reddit: {
    name: "Reddit",
    icon: (c = "#FF4500") => `
      <svg viewBox="0 0 24 24" width="15" height="15" fill="${c}">
        <circle cx="12" cy="13.5" r="7.5" opacity=".95"/>
        <circle cx="18.8" cy="8.3" r="1.7"/>
        <circle cx="5.2" cy="8.3" r="1.7"/>
        <path d="M13 2.8 15.6 3.4 14.2 6" stroke="${c}" stroke-width="1.4" fill="none" stroke-linecap="round"/>
        <circle cx="12" cy="2.7" r="1.4"/>
        <circle cx="9.3" cy="12.4" r="1.3" fill="#fff"/>
        <circle cx="14.7" cy="12.4" r="1.3" fill="#fff"/>
        <path d="M8.7 16.4c1 .9 2.1 1.3 3.3 1.3s2.3-.4 3.3-1.3" stroke="#fff" stroke-width="1.3" fill="none" stroke-linecap="round"/>
      </svg>`,
  },
  pinterest: {
    name: "Pinterest",
    icon: (c = "#E60023") => `
      <svg viewBox="0 0 24 24" width="15" height="15" fill="${c}">
        <path d="M12 0C5.373 0 0 5.373 0 12c0 5.084 3.163 9.426 7.627 11.174-.105-.949-.2-2.405.042-3.441.218-.937 1.407-5.965 1.407-5.965s-.359-.719-.359-1.782c0-1.668.967-2.914 2.171-2.914 1.023 0 1.518.769 1.518 1.69 0 1.029-.655 2.568-.994 3.995-.283 1.194.599 2.169 1.777 2.169 2.133 0 3.772-2.249 3.772-5.495 0-2.873-2.064-4.882-5.012-4.882-3.414 0-5.418 2.561-5.418 5.207 0 1.031.397 2.138.893 2.738a.36.36 0 0 1 .083.345l-.333 1.36c-.053.22-.174.267-.402.161-1.499-.698-2.436-2.889-2.436-4.649 0-3.785 2.75-7.262 7.929-7.262 4.163 0 7.398 2.967 7.398 6.931 0 4.136-2.607 7.464-6.227 7.464-1.216 0-2.359-.631-2.75-1.378l-.748 2.853c-.271 1.043-1.002 2.35-1.492 3.146C9.57 23.812 10.763 24 12 24c6.627 0 12-5.373 12-12S18.627 0 12 0z"/>
      </svg>`,
  },
  youtube: {
    name: "YouTube",
    hold: true,
    holdMsg: "YouTube downloads are on hold while we work on a fix — back soon! 🚧",
    icon: (c = "#FF0000") => `
      <svg viewBox="0 0 24 24" width="16" height="16" fill="${c}">
        <path d="M21.6 7.2a2.5 2.5 0 0 0-1.76-1.77C18.25 5 12 5 12 5s-6.25 0-7.84.43A2.5 2.5 0 0 0 2.4 7.2 26.3 26.3 0 0 0 2 12a26.3 26.3 0 0 0 .4 4.8 2.5 2.5 0 0 0 1.76 1.77C5.75 19 12 19 12 19s6.25 0 7.84-.43a2.5 2.5 0 0 0 1.76-1.77A26.3 26.3 0 0 0 22 12a26.3 26.3 0 0 0-.4-4.8ZM10 15.2V8.8L15.5 12 10 15.2Z"/>
      </svg>`,
  },
  generic: { name: "Web", icon: () => "" },
};

// --------------------------------------------------------------
// DOM & state
// --------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const state = {
  meta: null,
  mode: "video",          // "video" | "photos" | "audio"
  job: null,
  pollTimer: null,
  previewTimer: null,
  previewUrl: null,       // finished 480p preview file
  downloading: false,
};

const els = {
  input: $("urlInput"), fetch: $("fetchBtn"), hint: $("urlHint"),
  pills: $("platformPills"), toast: $("toast"),
  skeleton: $("analyzingCard"), card: $("previewCard"),
  thumbBox: $("thumbBox"), thumb: $("thumb"),
  previewBadge: $("previewBadge"), playBtn: $("playPreviewBtn"),
  durBadge: $("durBadge"), platBadge: $("platBadge"),
  title: $("mediaTitle"), uploader: $("mediaUploader"),
  seg: $("modeSeg"), select: $("qualitySelect"),
  dlBtn: $("downloadBtn"), dlLabel: $("dlLabel"),
  panel: $("progressPanel"), track: $("progressTrack"),
  fill: $("progressFill"), stage: $("stageLabel"), pct: $("pctLabel"),
  speed: $("speedLabel"), eta: $("etaLabel"),
  save: $("saveLink"), reset: $("resetBtn"),
};

// --------------------------------------------------------------
// Helpers
// --------------------------------------------------------------
function fmtBytes(bytes) {
  if (!bytes) return null;
  const units = ["B", "KB", "MB", "GB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function fmtDuration(s) {
  if (!s) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
           : `${m}:${String(sec).padStart(2, "0")}`;
}

function showToast(msg, kind = "error") {
  const styles = {
    error:   "bg-red-50 border-red-200 text-red-700 dark:bg-red-500/10 dark:border-red-400/25 dark:text-red-300",
    success: "bg-emerald-50 border-emerald-200 text-emerald-700 dark:bg-emerald-500/10 dark:border-emerald-400/25 dark:text-emerald-300",
    warn:    "bg-amber-50 border-amber-200 text-amber-800 dark:bg-amber-500/10 dark:border-amber-400/25 dark:text-amber-300",
  };
  els.toast.className = `toast max-w-2xl mx-auto mt-6 rounded-2xl border px-4 py-3 text-sm ${styles[kind]}`;
  els.toast.textContent = msg;
  els.toast.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => els.toast.classList.add("hidden"), 6000);
}

function setBusy(busy) {
  els.fetch.disabled = busy;
  els.fetch.textContent = busy ? "Working…" : "Fetch";
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 429) throw new Error("Rate limit hit — please wait a moment and try again.");
  if (!res.ok) {
    const detail = data && (data.detail || data.error);
    throw new Error(typeof detail === "string" ? detail : `Request failed (HTTP ${res.status}).`);
  }
  return data;
}

// Which download modes does this post support?
function availableModes(m) {
  const modes = [];
  const composite = isComposite(m);
  if (m.video_options.length || composite) modes.push("video");
  if (m.image_options.length) modes.push("photos");
  if (m.audio_options.length) modes.push("audio");
  return modes;
}

// Photo post that carries a music track → we can synthesize "image + music" video.
function isComposite(m) {
  return !m.video_options.length && m.image_options.length > 0 && m.audio_options.length > 0;
}

function resetMediaUI() {
  stopPolling();
  if (state.previewTimer) { clearTimeout(state.previewTimer); state.previewTimer = null; }
  state.previewUrl = null;
  els.thumbBox.querySelectorAll("video, audio").forEach((el) => el.remove());
  els.thumb.classList.remove("hidden");
  els.previewBadge.classList.add("hidden");
  els.playBtn.classList.add("hidden");
  els.playBtn.classList.remove("flex");
}

// --------------------------------------------------------------
// Analyze flow
// --------------------------------------------------------------
async function analyze() {
  const urls = extractUrls(els.input.value);
  if (!urls.length) { showToast("Paste a link first.", "warn"); return; }

  // Bulk mode: several pasted links → tray of rows instead of one card.
  if (urls.length > 1) {
    resetMediaUI();
    setBusy(true);
    try { await runBulk(urls); } finally { setBusy(false); }
    return;
  }
  const url = urls[0];
  $("bulkBox").classList.add("hidden");

  const held = detectPlatform(url);
  if (held && PLATFORMS[held].hold) { showToast(PLATFORMS[held].holdMsg, "warn"); return; }

  resetMediaUI();
  state.meta = null; state.job = null; state.downloading = false;
  els.toast.classList.add("hidden");
  els.card.classList.add("hidden");
  els.skeleton.classList.remove("hidden");
  setBusy(true);

  try {
    const meta = await api("/api/analyze", { url });
    state.meta = meta;
    renderPreview();
    setupPreview();
  } catch (err) {
    showToast(err.message);
  } finally {
    els.skeleton.classList.add("hidden");
    setBusy(false);
  }
}

function renderPreview() {
  const m = state.meta;
  els.thumb.src = m.thumbnail || "";
  els.thumb.alt = m.title || "Media preview";
  els.durBadge.textContent = fmtDuration(m.duration);
  els.durBadge.classList.toggle("hidden", !m.duration);

  const p = PLATFORMS[m.platform] || PLATFORMS.generic;
  els.platBadge.style.background = `${m.platform_color || "#000"}cc`;
  els.platBadge.innerHTML = `${p.icon("#fff")} ${m.platform_name}`;
  els.platBadge.classList.toggle("hidden", m.platform === "generic");

  els.title.textContent = m.title;
  els.uploader.textContent = m.uploader ? `by ${m.uploader}` : "";

  const modes = availableModes(m);
  state.mode = modes.includes("video") ? "video" : (modes.includes("photos") ? "photos" : "audio");
  renderSeg(modes);
  fillQualitySelect();

  els.panel.classList.add("hidden");
  els.dlBtn.disabled = false;
  els.dlLabel.textContent = "Download";
  els.card.classList.remove("hidden");
  els.card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

const MODE_LABELS = { video: "Video", photos: "Photos", audio: "Audio" };

function renderSeg(modes) {
  els.seg.innerHTML = modes.map((mode) => `
    <button data-mode="${mode}"
            class="px-5 py-1.5 rounded-lg transition ${mode === state.mode ? "seg-active" : "text-smoke"}">
      ${MODE_LABELS[mode]}
    </button>`).join("");
}

function updateSeg() {
  els.seg.querySelectorAll("button").forEach((b) => {
    const active = b.dataset.mode === state.mode;
    b.classList.toggle("seg-active", active);
    b.classList.toggle("text-smoke", !active);
  });
}

function fillQualitySelect() {
  const m = state.meta;
  let opts = [];
  if (state.mode === "video") {
    opts = isComposite(m)
      ? [{ label: "Image + music · MP4 (auto quality)" }]
      : m.video_options;
  } else if (state.mode === "photos") {
    opts = m.image_options;
  } else {
    opts = m.audio_options;
  }
  els.select.innerHTML = opts.map((o, i) => {
    const size = o.filesize ? ` · ~${fmtBytes(o.filesize)}` : "";
    const extra = o.fps && o.fps > 30 ? ` · ${Math.round(o.fps)}fps` : "";
    return `<option value="${i}">${o.label}${extra}${state.mode !== "photos" ? size : ""}</option>`;
  }).join("");
}

// --------------------------------------------------------------
// Inline 480p preview — NOTHING is fetched until the user taps
// "Play preview". Only then does the preview job start; when it's
// ready the player mounts and plays. Never autoplays, never preloads.
// Available everywhere EXCEPT YouTube, and only for real video posts.
// --------------------------------------------------------------
function setPlayLabel(t){ const l=$("playBtnLabel"); if(l) l.textContent=t; }

function setupPreview() {
  const m = state.meta;
  state.previewUrl = null;
  state.previewEligible = !!(m && m.platform !== "youtube" && m.video_options.length);
  if (state.previewEligible) {
    els.playBtn.disabled = false;
    setPlayLabel("Play preview");
    els.playBtn.classList.remove("hidden");
    els.playBtn.classList.add("flex");
  } else {
    els.playBtn.classList.add("hidden");
    els.playBtn.classList.remove("flex");
  }
}

function pollPreview(jobId) {
  if (state.previewTimer) clearTimeout(state.previewTimer);
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}?t=${Date.now()}`);
      if (job.status === "done") {
        state.previewUrl = job.download_url;
        els.durBadge.classList.add("hidden");         // player shows duration
        const video = mountPlayer(job.download_url);
        if (video && video.tagName === "VIDEO") video.play().catch(() => {});
        return;
      }
      if (job.status === "error") {
        els.playBtn.disabled = false;
        setPlayLabel("Play preview");
        toast("Preview unavailable: " + (job.error || "unknown error"));
        return;
      }
      state.previewTimer = setTimeout(tick, 800);
    } catch (e) {
      els.playBtn.disabled = false;
      setPlayLabel("Play preview");
    }
  };
  state.previewTimer = setTimeout(tick, 600);
}

els.playBtn.addEventListener("click", () => {
  if (!state.previewEligible) return;
  if (state.previewUrl) {                       // already loaded → just play
    const video = mountPlayer(state.previewUrl);
    if (video && video.tagName === "VIDEO") video.play().catch(() => {});
    return;
  }
  els.playBtn.disabled = true;
  setPlayLabel("Loading preview…");
  const url = state.meta.webpage_url || state.meta.url;
  api("/api/preview", { url })
    .then((job) => pollPreview(job.id))
    .catch((e) => {
      els.playBtn.disabled = false;
      setPlayLabel("Play preview");
      toast("Preview unavailable: " + e.message);
    });
});

function mountPlayer(fileUrl, filename) {
  const ext = (filename || fileUrl || "").split(".").pop().toLowerCase();
  const isAudio = ["mp3", "m4a", "aac", "opus", "wav"].includes(ext);
  els.thumbBox.querySelectorAll("video, audio").forEach((el) => el.remove());

  if (isAudio) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "metadata";
    audio.src = fileUrl;
    audio.className = "absolute inset-x-3 bottom-3 h-10 rounded-lg";
    els.thumbBox.appendChild(audio);
    return audio;
  }
  const video = document.createElement("video");
  video.controls = true;
  video.playsInline = true;
  video.preload = "metadata";
  if (state.meta?.thumbnail) video.poster = state.meta.thumbnail;
  video.src = fileUrl;
  video.className = "absolute inset-0 w-full h-full object-contain bg-black";
  els.thumbBox.appendChild(video);
  els.thumb.classList.add("hidden");
  return video;
}

// --------------------------------------------------------------
// Download flow
// --------------------------------------------------------------
async function startDownload() {
  if (!state.meta || state.downloading) return;
  const m = state.meta;

  const body = { url: m.webpage_url || m.url, mode: state.mode === "photos" ? "image" : state.mode,
                 title: m.title };

  if (state.mode === "photos") {
    const chosen = m.image_options[els.select.value] || m.image_options[0];
    body.image_url = chosen.url;
  } else if (state.mode === "video" && isComposite(m)) {
    body.image_url = m.image_options[0].url;      // slideshow from best image + post audio
  } else if (state.mode === "video") {
    const chosen = m.video_options[els.select.value] || m.video_options[0];
    body.height = chosen.height;
    if (chosen.direct_url) body.direct_url = chosen.direct_url;   // X syndication
  } else {
    const chosen = m.audio_options[els.select.value] || m.audio_options[0];
    body.audio_format = chosen.audio_format;
    if (chosen.direct_url) body.direct_url = chosen.direct_url;   // X syndication
  }

  state.downloading = true;
  els.dlBtn.disabled = true;
  els.dlLabel.textContent = "Preparing…";
  els.panel.classList.remove("hidden");
  els.save.classList.add("hidden");
  els.reset.classList.add("hidden");
  setProgress(0, "Queued — waiting for a worker…");
  els.track.classList.add("progress-indeterminate");

  try {
    state.job = await api("/api/download", body);
    state.bootId = state.job.boot_id;
    state.pollFails = 0;
    pollJob();
  } catch (err) {
    failDownload(err.message);
  }
}

function pollJob() {
  stopPolling();
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${state.job.id}?t=${Date.now()}`);
      state.pollFails = 0;
      // Server rebooted mid-job → worker is gone; never freeze silently.
      if (state.bootId && job.boot_id && job.boot_id !== state.bootId && job.status !== "done") {
        failDownload("The server restarted while working — the job was lost. Please try again.");
        return;
      }
      applyJob(job);
      if (["queued", "downloading", "processing"].includes(job.status)) {
        state.pollTimer = setTimeout(tick, 700);
      }
    } catch (err) {
      state.pollFails = (state.pollFails || 0) + 1;
      if (state.pollFails >= 12) { failDownload(err.message); return; }
      els.stage.textContent = "Connection hiccup — retrying…";
      state.pollTimer = setTimeout(tick, 1500);
    }
  };
  state.pollTimer = setTimeout(tick, 500);
}

function applyJob(job) {
  state.job = job;
  els.track.classList.toggle("progress-indeterminate", job.status !== "downloading");

  if (job.status === "queued") {
    setProgress(0, "Queued — waiting for a worker…");
  } else if (job.status === "downloading") {
    const pct = Math.max(1, Math.floor(job.progress || 0));
    const speed = job.speed ? `${fmtBytes(job.speed)}/s` : "";
    const eta = Number.isFinite(job.eta) && job.eta > 0 ? `${Math.ceil(job.eta)}s left` : "";
    setProgress(pct, "Downloading media…", speed, eta);
  } else if (job.status === "processing") {
    setProgress(100, "Processing — merging / converting…");
  } else if (job.status === "done") {
    setProgress(100, `Done — ${job.filename || "your file"} (${fmtBytes(job.filesize) || ""})`);
    state.downloading = false;
    els.dlBtn.disabled = false;
    els.dlLabel.textContent = "Download again";
    els.reset.classList.remove("hidden");
    deliverFile(job);
  } else if (job.status === "error") {
    failDownload(job.error || "The download failed.");
  }
}

function deliverFile(job) {
  els.save.href = job.download_url;
  els.save.setAttribute("download", job.filename || "download");
  els.save.classList.remove("hidden");
  // A programmatic <a>.click() fired minutes after the user's original tap
  // has lost its "user activation" — mobile browsers silently swallow those
  // (the "only downloads on the second try" complaint). A same-frame
  // navigation to an attachment URL always starts the download and never
  // leaves the page, gesture or not.
  window.location.href = job.download_url;
  showToast("Your download is ready — check your browser's downloads.", "success");
}

function triggerSave(href, filename) {
  const a = document.createElement("a");
  a.href = href;
  if (filename) a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function setProgress(pct, stage, speed = "", eta = "") {
  els.fill.style.width = `${Math.min(100, pct)}%`;
  els.pct.textContent = `${Math.min(100, Math.round(pct))}%`;
  els.stage.textContent = stage;
  els.speed.innerHTML = speed || "&nbsp;";
  els.eta.innerHTML = eta || "&nbsp;";
}

function failDownload(msg) {
  stopPolling();
  els.track.classList.remove("progress-indeterminate");
  state.downloading = false;
  els.dlBtn.disabled = false;
  els.dlLabel.textContent = "Retry download";
  els.reset.classList.remove("hidden");
  setProgress(0, "");
  els.stage.textContent = "Something went wrong";
  showToast(msg);
}

function stopPolling() {
  if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
}

// --------------------------------------------------------------
// Bulk paste — several links at once, deduped, per-row quality
// --------------------------------------------------------------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function extractUrls(t) {
  return [...new Set(t.match(/https?:\/\/[^\s]+/g) || [])].slice(0, 10);
}

const BULK = [];

async function runBulk(urls) {
  els.card.classList.add("hidden");
  $("bulkBox").classList.remove("hidden");
  $("bulkCount").textContent = urls.length;
  $("bulkList").innerHTML = "";
  BULK.length = 0;
  for (const u of urls) {
    const row = { url: u, meta: null, started: false, el: document.createElement("div") };
    row.el.className = "flex flex-wrap items-center gap-2.5 bg-mist/60 rounded-2xl px-4 py-3 text-sm";
    row.el.innerHTML = `<span class="flex-1 min-w-0 truncate text-smoke">${esc(u)}</span>` +
                       `<span class="bulk-status text-xs text-smoke shrink-0">fetching…</span>`;
    $("bulkList").appendChild(row.el);
    BULK.push(row);
    try {
      row.meta = await api("/api/analyze", { url: u });
      fillRow(row);
    } catch (e) {
      const st = row.el.querySelector(".bulk-status");
      st.textContent = "✕ " + e.message.slice(0, 70);
      st.className = "bulk-status text-xs text-red-500 shrink-0";
    }
  }
}

function rowOptions(m) {
  const opts = [];
  if (m.video_options.length) m.video_options.forEach((v, i) => opts.push({ kind: "video", i, label: v.label }));
  else if (m.image_options.length) opts.push({ kind: "image", i: 0, label: `Image · ${m.image_options.length} photo(s)` });
  if (m.audio_options.length) opts.push({ kind: "audio", i: 0, label: "Audio · MP3" });
  return opts;
}

function fillRow(row) {
  const m = row.meta;
  const p = PLATFORMS[m.platform] || PLATFORMS.generic;
  row.opts = rowOptions(m);
  row.el.innerHTML = `
    <span class="shrink-0">${p.icon()}</span>
    <span class="flex-1 min-w-0 truncate font-medium">${esc(m.title || m.url)}</span>
    <select class="bulk-sel bg-mist text-xs rounded-lg px-2 py-1.5 focus:outline-none">
      ${row.opts.map((o, i) => `<option value="${i}">${esc(o.label)}</option>`).join("")}
    </select>
    <button class="bulk-dl grad-btn text-white text-xs font-semibold rounded-lg px-3.5 py-2 shrink-0">Download</button>
    <span class="bulk-status text-xs text-smoke shrink-0 w-24 text-right"></span>`;
  row.el.querySelector(".bulk-dl").addEventListener("click", () => startRow(row));
}

async function startRow(row) {
  if (!row.meta || row.started || !row.opts.length) return;
  row.started = true;
  const m = row.meta;
  const choice = row.opts[+row.el.querySelector(".bulk-sel").value] || row.opts[0];
  const body = { url: m.webpage_url || m.url, title: m.title,
                 mode: choice.kind === "image" ? "image" : choice.kind };
  if (choice.kind === "video") {
    const v = m.video_options[choice.i];
    body.height = v.height;
    if (v.direct_url) body.direct_url = v.direct_url;
  } else if (choice.kind === "audio") {
    const a = m.audio_options[choice.i];
    body.audio_format = a.audio_format || "mp3";
    if (a.direct_url) body.direct_url = a.direct_url;
  } else {
    body.image_url = m.image_options[0].url;
  }
  const st = row.el.querySelector(".bulk-status");
  st.textContent = "starting…";
  try {
    const job = await api("/api/download", body);
    pollRow(row, job.id, st);
  } catch (e) {
    row.started = false;
    st.textContent = "✕ " + e.message.slice(0, 60);
    st.className = "bulk-status text-xs text-red-500 shrink-0 w-24 text-right";
  }
}

function pollRow(row, jobId, st) {
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}?t=${Date.now()}`);
      if (job.status === "downloading") { st.textContent = `${Math.round(job.progress || 0)}%`; setTimeout(tick, 900); }
      else if (job.status === "queued" || job.status === "processing") { st.textContent = `${job.status}…`; setTimeout(tick, 900); }
      else if (job.status === "done") {
        // Real anchor = fresh user gesture when tapped (mobile-safe), plus
        // an auto attempt for desktops that allow it.
        st.innerHTML = `<a href="${job.download_url}" download="${esc(job.filename || "download")}"
          class="text-emerald-600 dark:text-emerald-300 font-semibold underline">save</a>`;
        st.className = "bulk-status text-xs shrink-0 w-24 text-right";
        triggerSave(job.download_url, job.filename);
      } else {
        st.textContent = "✕ failed";
        st.className = "bulk-status text-xs text-red-500 shrink-0 w-24 text-right";
        row.started = false;
      }
    } catch (e) { setTimeout(tick, 1500); }
  };
  setTimeout(tick, 600);
}

// --------------------------------------------------------------
// Theme toggle
// --------------------------------------------------------------
function paintThemeIcon() {
  const dark = document.documentElement.classList.contains("dark");
  $("iconSun").classList.toggle("hidden", !dark);
  $("iconMoon").classList.toggle("hidden", dark);
}

$("themeToggle").addEventListener("click", () => {
  const isDark = !document.documentElement.classList.contains("dark");
  document.documentElement.classList.toggle("dark", isDark);
  try { localStorage.setItem("ss-theme", isDark ? "dark" : "light"); } catch (e) {}
  paintThemeIcon();
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  let saved = null;
  try { saved = localStorage.getItem("ss-theme"); } catch (err) {}
  if (saved) return;
  document.documentElement.classList.toggle("dark", e.matches);
  paintThemeIcon();
});

// --------------------------------------------------------------
// Wiring
// --------------------------------------------------------------
function renderPills() {
  // Icon-only nodes for the 3D icon cloud; names kept for a11y + detect chip.
  els.pills.innerHTML = Object.entries(PLATFORMS)
    .filter(([k]) => k !== "generic")
    .map(([key, p]) => `
      <span class="platform-pill${p.hold ? " hold" : ""}" data-platform="${key}"
            title="${p.name}${p.hold ? " · soon" : ""}" aria-label="${p.name}">
        ${p.icon()}<span class="sr-only">${p.name}</span>
      </span>`).join("");
}

function setActivePill(platform) {
  document.querySelectorAll(".platform-pill").forEach((el) =>
    el.classList.toggle("active", el.dataset.platform === platform));
}

function detectPlatform(url) {
  const host = (() => { try { return new URL(url.includes("://") ? url : `https://${url}`).hostname; } catch { return ""; } })();
  return Object.keys(PLATFORMS).find((k) => {
    const pats = { tiktok: /tiktok/, instagram: /instagram/, snapchat: /snapchat/,
                   facebook: /facebook|fb\./, twitter: /(^|\.)(twitter|x)\.|^t\.co$/,
                   reddit: /reddit|redd\.it/, pinterest: /pinterest|pin\.it/,
                   youtube: /youtube|youtu\.be/ };
    return pats[k]?.test(host);
  }) || null;
}

function onInput() {
  const url = els.input.value.trim();
  const platform = detectPlatform(url);
  setActivePill(platform || "");
  els.hint.textContent = platform
    ? (PLATFORMS[platform].hold
        ? PLATFORMS[platform].holdMsg
        : `${PLATFORMS[platform].name} link detected — hit Fetch.`)
    : "Tip: paste a link and we'll detect the platform automatically.";
}

renderPills();
paintThemeIcon();
els.fetch.addEventListener("click", analyze);
$("bulkAllBtn").addEventListener("click", () => {
  BULK.forEach((row, i) => setTimeout(() => startRow(row), i * 400));
});
els.dlBtn.addEventListener("click", startDownload);
els.reset.addEventListener("click", () => location.reload());   // "Download another file"
els.input.addEventListener("input", onInput);
els.input.addEventListener("keydown", (e) => { if (e.key === "Enter") analyze(); });

els.seg.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  state.mode = btn.dataset.mode;
  updateSeg();
  fillQualitySelect();
});

document.addEventListener("paste", (e) => {
  if (document.activeElement === els.input) return;
  const text = (e.clipboardData || window.clipboardData)?.getData("text") || "";
  if (/^https?:\/\//i.test(text.trim())) {
    els.input.value = text.trim();
    onInput();
    e.preventDefault();
  }
});
