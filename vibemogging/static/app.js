"use strict";

// Vibemogging V1 editor UI. Talks only to the local backend described in docs/API.md.

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};

const OPERATIONS = {
  replace_video: { label: "Заменить видео", track: "video", upload: true },
  brighten: { label: "Осветлить", track: "video" },
  darken: { label: "Затемнить", track: "video" },
  fill: { label: "Залить цветом", track: "video" },
  replace_audio: { label: "Заменить звук", track: "audio", upload: true },
  mute: { label: "Отключить звук", track: "audio" },
  gain: { label: "Громкость", track: "audio" },
  replace_av: { label: "Заменить видео и звук", track: "both", upload: true },
};
const CHOICES = { base: "Исходная", ours: "Наша", theirs: "Входящая" };
const BRANCH_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$/;
const PEER_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;
const MAX_AUDIO_SPAN = 60;

const state = {
  status: null,
  busy: false,          // a job or upload started by this page is running
  offline: false,
  view: "video",        // "video" | "frame"
  playerRef: null,      // null = working draft, otherwise a commit hash being viewed
  playerUrl: null,
  time: 0,
  selection: null,      // { start, end } seconds, frame-snapped
  compare: null,        // { left, right, diff }
  waveKey: null,
  wavePeaks: null,
  projects: null,       // last /api/projects listing
  cloud: null,          // last /api/cloud status
  cloneCandidates: [],
};

/* ---------- formatting ---------- */

function fmtTime(seconds, digits = 2) {
  if (!Number.isFinite(seconds)) return "—";
  const sign = seconds < 0 ? "-" : "";
  let s = Math.abs(seconds);
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  let sec = s.toFixed(digits);
  if (Number(sec) >= 60) sec = (59.99).toFixed(digits);
  const ss = sec.padStart(digits ? 3 + digits : 2, "0");
  const mm = String(m).padStart(2, "0");
  return sign + (h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`);
}

function parseTime(text) {
  const value = String(text).trim().replace(",", ".");
  if (!value) return NaN;
  if (!/^\d+(:\d{1,2})?(:\d{1,2})?(\.\d+)?$/.test(value)) return NaN;
  return value.split(":").reduce((acc, part) => acc * 60 + Number(part), 0);
}

function fmtRange(start, end) {
  return `${fmtTime(start)} – ${fmtTime(end)}`;
}

function shortHash(hash) {
  return hash ? hash.slice(0, 10) : "";
}

function fmtDate(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso || "";
  return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/* ---------- network ---------- */

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    setOffline(true);
    throw new Error("Нет связи с локальным сервером.");
  }
  setOffline(false);
  let data = null;
  const text = await response.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!response.ok) {
    throw new Error((data && data.error) || `Ошибка сервера (${response.status}).`);
  }
  return data;
}

function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { data = null; }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.upload) resolve(data.upload);
      else reject(new Error((data && data.error) || `Загрузка не удалась (${xhr.status}).`));
    };
    xhr.onerror = () => reject(new Error("Загрузка прервана: нет связи с сервером."));
    xhr.send(file);
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Run one backend action: optional upload, POST /api/action, poll the job,
 * report the outcome, then always refresh status. Returns the job result or null.
 */
async function runAction(payload, { label = "Обработка…", file = null } = {}) {
  const results = await runActions([payload], { labels: [label], file });
  return results ? results[0] : null;
}

/** Run several actions one after another under a single busy lock; stops at the first failure. */
async function runActions(payloads, { labels = [], file = null, quiet = false } = {}) {
  if (state.busy) {
    toast("Дождитесь завершения текущей операции.", "error");
    return null;
  }
  setBusy(true);
  const results = [];
  try {
    for (let i = 0; i < payloads.length; i++) {
      const payload = payloads[i];
      const label = labels[i] || labels[0] || "Обработка…";
      showJob(label, 0);
      if (file && i === 0) {
        const MB = (file.size / 1024 / 1024).toFixed(1);
        const text = `Загрузка «${file.name}» (${MB} МБ)…`;
        showJob(text, 0);
        payload.upload = await uploadFile(file, (p) => showJob(text, p));
        showJob(label, 0);
      }
      const result = await executeJob(payload, label);
      results.push(result);
      if (!quiet || i === payloads.length - 1) {
        const link = result.download_url;
        toast(result.message || "Готово", "success", link ? { href: link, text: "Скачать снова" } : null);
        if (link) triggerDownload(link);
      }
    }
  } catch (error) {
    toast(error.message, "error");
    return null;
  } finally {
    hideJob();
    setBusy(false);
    state.cloudStale = true;
    state.projectsFor = null;
    await refreshStatus();
  }
  return results;
}

async function executeJob(payload, label) {
  const { job } = await api("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  for (;;) {
    await sleep(600);
    let info;
    try {
      info = await api(`/api/jobs/${encodeURIComponent(job)}`);
    } catch (error) {
      if (state.offline) continue;
      throw error;
    }
    if (info.status === "running") {
      showJob(info.message && info.message !== "Обработка…" ? `${label} ${info.message}` : label, info.progress || 0);
    } else if (info.status === "done") {
      return info.result || {};
    } else {
      throw new Error(info.error || "Операция завершилась ошибкой.");
    }
  }
}

function triggerDownload(href) {
  const link = el("a", { href, download: "" });
  document.body.append(link);
  link.click();
  link.remove();
}

/* ---------- busy / job UI ---------- */

function setBusy(value) {
  state.busy = value;
  updateControls();
}

function setOffline(value) {
  if (state.offline === value) return;
  state.offline = value;
  $("offlineBanner").hidden = !value;
  updateControls();
}

function showJob(text, progress) {
  $("jobbar").hidden = false;
  $("jobText").textContent = text;
  const p = Math.max(0, Math.min(1, progress || 0));
  $("jobProgress").value = p;
  $("jobPercent").textContent = `${Math.round(p * 100)}%`;
}

function hideJob() {
  $("jobbar").hidden = true;
}

function toast(text, kind = "info", link = null) {
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" },
    el("div", { class: "toast-text" }, text, link ? [" ", el("a", { href: link.href, download: "" }, link.text)] : null),
    el("button", { type: "button", "aria-label": "Закрыть", onclick: () => node.remove() }, "✕"));
  $("toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 12000 : 6000);
}

const confirmDialog = $("confirmDialog");
function confirmAction(title, text, okLabel = "Подтвердить") {
  return new Promise((resolve) => {
    $("confirmTitle").textContent = title;
    $("confirmText").textContent = text;
    $("confirmOk").textContent = okLabel;
    confirmDialog.returnValue = "";
    confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "ok"), { once: true });
    confirmDialog.showModal();
  });
}

/* ---------- status ---------- */

let statusRequest = null;
async function refreshStatus() {
  if (statusRequest) return statusRequest;
  statusRequest = (async () => {
    try {
      state.status = await api("/api/status");
    } catch (error) {
      if (!state.status) $("loadingView").textContent = error.message;
      return;
    } finally {
      statusRequest = null;
    }
    render();
    if (state.status.busy && !state.busy) {
      // Another tab is running an action; check again shortly.
      setTimeout(refreshStatus, 1500);
    }
  })();
  return statusRequest;
}

const s = () => state.status || {};
const project = () => Boolean(s().project);
const fps = () => s().fps || 1;
const duration = () => s().duration || 0;
const frames = () => s().frames || 1;
const mergePending = () => Boolean(s().merge);
const serverBusy = () => state.busy || Boolean(s().busy);
const frameAt = (t) => Math.max(0, Math.min(frames() - 1, Math.floor(t * fps() + 1e-6)));
const snap = (t) => Math.max(0, Math.min(duration(), Math.round(t * fps()) / fps()));
const localBranches = () => Object.keys(s().branches || {}).filter((name) => !(s().incoming || []).includes(name));

/* ---------- rendering ---------- */

const chooser = () => s().mode === "workspace" && !s().open;
const projectTitle = () => (s().workspace_project && s().workspace_project.name) || (s().project && s().project.name) || "";

function render() {
  $("loadingView").hidden = true;
  $("ffmpegBanner").hidden = s().ffmpeg !== false;
  const loaded = project();
  const listing = chooser();
  $("projectsView").hidden = !listing;
  $("emptyView").hidden = loaded || listing;
  $("workspace").hidden = !loaded;
  $("topbarState").hidden = !loaded;
  $("topbarActions").hidden = !loaded;
  $("backToProjectsBtn").hidden = !(s().mode === "workspace" && s().open);
  $("projectName").textContent = listing ? "" : projectTitle();
  $("emptyTitle").textContent = s().workspace_project ? `Проект «${s().workspace_project.name}»` : "Новый проект";
  document.title = listing ? "Проекты — Vibemogging" : (projectTitle() ? `${projectTitle()} — Vibemogging` : "Vibemogging");
  if (listing) loadProjects();
  if (loaded) {
    if (state.selection && state.selection.end > duration()) state.selection = null;
    renderTopbar();
    renderPlayer();
    renderTimeline();
    renderVersions();
    renderBranches();
    renderCompareRefs();
    renderMerge();
    renderStorage();
    renderCloudBasics();
    loadWaveform();
  }
  updateControls();
}

function renderTopbar() {
  const st = s();
  const select = $("branchSelect");
  const names = Object.keys(st.branches);
  select.replaceChildren(...names.map((name) => el("option", { value: name, selected: name === st.branch },
    name + (st.drafts.includes(name) && name !== st.branch ? " • черновик" : ""))));
  $("dirtyChip").hidden = !st.dirty || mergePending();
  $("mergeChip").hidden = !mergePending();
  $("projectMeta").textContent = `${st.width}×${st.height} · ${Number(st.fps.toFixed(3))} к/с · ${fmtTime(st.duration)}`;
}

function updateControls() {
  const busy = serverBusy() || state.offline;
  const merge = mergePending();
  const st = s();
  document.querySelectorAll("[data-mutate]").forEach((node) => { node.disabled = busy; });
  document.querySelectorAll("[data-project-action]").forEach((node) => { node.disabled = busy; });
  if (!project()) {
    $("importVideoBtn").disabled = busy || !$("importVideoFile").files.length || st.ffmpeg === false;
    $("openRepoBtn").disabled = busy || !$("openRepoFile").files.length;
    return;
  }
  // Editing and branch switching are blocked while a merge is pending.
  document.querySelectorAll(".edit-panel input, .edit-panel button").forEach((node) => { node.disabled = busy || merge; });
  $("branchSelect").disabled = busy || merge;
  $("branchName").disabled = busy || merge;
  $("branchCreateBtn").disabled = busy || merge;
  $("discardBtn").disabled = busy || merge;
  $("commitMessage").disabled = busy || merge;
  $("commitAuthor").disabled = busy;
  $("commitBtn").disabled = busy || merge || !st.dirty;
  $("mergeSource").disabled = busy || merge;
  $("mergeBtn").disabled = busy || merge || st.dirty || !$("mergeSource").value;
  $("exportMp4Btn").disabled = busy || st.ffmpeg === false;
  $("exportRepoBtn").disabled = busy || merge;
  if (merge) {
    const unresolved = st.merge.conflicts.filter((c) => !c.choice).length;
    $("mergeCommitBtn").disabled = busy || unresolved > 0;
  }
  document.querySelectorAll("[data-needs-idle]").forEach((node) => { node.disabled = busy; });
  document.querySelectorAll("[data-needs-switch]").forEach((node) => { node.disabled = busy || merge; });
  $("applyEditBtn").textContent = busy ? "Операция выполняется…" : "Применить к диапазону";
}

/* ---------- player ---------- */

const video = $("video");
const frameImage = $("frameImage");

function currentPreviewUrl() {
  return state.playerRef ? state.playerUrl : s().preview_url;
}

function renderPlayer() {
  const url = currentPreviewUrl();
  const absolute = url ? new URL(url, location.href).href : "";
  if (video.src !== absolute) {
    if (url) {
      video.src = url;
      video.addEventListener("loadedmetadata", syncVideoToState, { once: true });
    } else {
      video.removeAttribute("src");
      video.load();
    }
  }
  const viewingCommit = Boolean(state.playerRef);
  $("playerSource").textContent = viewingCommit
    ? `Просмотр версии ${shortHash(state.playerRef)} (только просмотр)`
    : (s().dirty ? "Рабочая версия (черновик)" : "Рабочая версия");
  $("backToWorkingBtn").hidden = !viewingCommit;
  const frameView = state.view === "frame";
  video.hidden = frameView || !url;
  frameImage.hidden = !frameView;
  $("previewMissing").hidden = frameView || Boolean(url);
  $("frameTotal").textContent = `из ${frames()}`;
  $("frameInput").max = frames() - 1;
  updateTimeDisplay();
  if (frameView) loadFrameImage();
}

function playerRefParam() {
  return state.playerRef || "working";
}

let frameTimer = null;
function loadFrameImage() {
  clearTimeout(frameTimer);
  frameTimer = setTimeout(() => {
    if (state.view !== "frame" || !project() || serverBusy()) return;
    const ref = playerRefParam();
    const version = ref === "working" ? `&v=${s().manifest}` : "";
    const src = `/api/frame?ref=${encodeURIComponent(ref)}&frame=${frameAt(state.time)}${version}`;
    if (frameImage.getAttribute("src") !== src) frameImage.src = src;
    frameImage.alt = `Кадр ${frameAt(state.time)}`;
  }, 60);
}

frameImage.addEventListener("error", () => {
  if (frameImage.getAttribute("src")) toast("Не удалось получить кадр.", "error");
});

function seek(t, { fromVideo = false } = {}) {
  state.time = Math.max(0, Math.min(Math.max(0, duration() - 1e-6), t));
  if (!fromVideo && video.src && !video.hidden && Number.isFinite(video.duration)) {
    video.currentTime = Math.min(state.time, Math.max(0, video.duration - 0.001));
  }
  updateTimeDisplay();
  if (state.view === "frame") loadFrameImage();
}

function seekFrame(frame) {
  const f = Math.max(0, Math.min(frames() - 1, Math.round(frame)));
  seek((f + 0.5) / fps());
}

function updateTimeDisplay() {
  const f = frameAt(state.time);
  $("timecode").textContent = fmtTime(state.time);
  if (document.activeElement !== $("frameInput")) $("frameInput").value = f;
  positionPlayhead();
}

function syncVideoToState() {
  if (Number.isFinite(video.duration)) video.currentTime = Math.min(state.time, Math.max(0, video.duration - 0.001));
}

// In frame view the hidden <video> must not move the shared position.
const fromVideoEvent = () => { if (state.view === "video") seek(video.currentTime, { fromVideo: true }); };
video.addEventListener("timeupdate", () => { if (video.paused) fromVideoEvent(); });
video.addEventListener("seeked", fromVideoEvent);
video.addEventListener("play", () => { $("playBtn").textContent = "❚❚"; requestAnimationFrame(followVideo); });
video.addEventListener("pause", () => { $("playBtn").textContent = "▶︎"; });
video.addEventListener("error", () => {
  if (video.getAttribute("src")) toast("Не удалось загрузить предпросмотр. Подготовьте его заново.", "error");
});

function followVideo() {
  if (video.paused || video.ended) return;
  state.time = video.currentTime;
  updateTimeDisplay();
  requestAnimationFrame(followVideo);
}

function togglePlay() {
  if (state.view !== "video") setView("video");
  if (!video.getAttribute("src")) return;
  if (video.paused) video.play().catch(() => {});
  else video.pause();
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".seg-btn").forEach((btn) => {
    const active = btn.dataset.view === view;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });
  if (view === "frame") video.pause();
  renderPlayer();
  if (view === "video") syncVideoToState();
}

async function viewCommit(commitId) {
  const result = await runAction({ action: "preview", ref: commitId }, { label: "Подготовка предпросмотра версии…" });
  if (result && result.preview_url) {
    state.playerRef = commitId;
    state.playerUrl = result.preview_url;
    renderPlayer();
  }
}

function backToWorking() {
  state.playerRef = null;
  state.playerUrl = null;
  renderPlayer();
}

/* ---------- timeline ---------- */

const timeline = $("timeline");

function laneGeometry() {
  const box = timeline.getBoundingClientRect();
  const lane = $("videoLane").getBoundingClientRect();
  return { offset: lane.left - box.left, width: Math.max(1, lane.width), left: lane.left };
}

function timeToX(t) {
  const { offset, width } = laneGeometry();
  return offset + (duration() ? (t / duration()) * width : 0);
}

function clientXToTime(clientX) {
  const { left, width } = laneGeometry();
  const frac = Math.max(0, Math.min(1, (clientX - left) / width));
  return frac * duration();
}

function positionPlayhead() {
  if (!project()) return;
  $("playhead").style.left = `${timeToX(state.time) - 1}px`;
}

function renderSelection() {
  const sel = state.selection;
  const node = $("selection");
  if (!sel || sel.end <= sel.start) {
    node.hidden = true;
    $("rangeInfo").textContent = "Диапазон не выбран";
  } else {
    node.hidden = false;
    const x0 = timeToX(sel.start), x1 = timeToX(sel.end);
    node.style.left = `${x0}px`;
    node.style.width = `${Math.max(2, x1 - x0)}px`;
    const n = Math.round((sel.end - sel.start) * fps());
    $("rangeInfo").textContent = `${(sel.end - sel.start).toFixed(2)} с · ${n} ${plural(n, "кадр", "кадра", "кадров")}`;
  }
  if (document.activeElement !== $("rangeStart")) $("rangeStart").value = sel ? fmtTime(sel.start) : "";
  if (document.activeElement !== $("rangeEnd")) $("rangeEnd").value = sel ? fmtTime(sel.end) : "";
}

function setSelection(start, end) {
  let a = snap(Math.min(start, end)), b = snap(Math.max(start, end));
  if (b - a < 1 / fps() - 1e-9) b = Math.min(duration(), a + 1 / fps());
  state.selection = b > a ? { start: a, end: b } : null;
  renderSelection();
}

function renderRuler() {
  const { width } = laneGeometry();
  const total = duration();
  const ruler = $("ruler");
  ruler.replaceChildren();
  if (!total) return;
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  const step = steps.find((candidate) => (total / candidate) * 64 <= width) || 3600;
  for (let t = 0; t <= total + 1e-9; t += step) {
    const tick = el("div", { class: "tick" }, el("span", {}, fmtTime(t, step < 1 ? 1 : 0)));
    tick.style.left = `${(t / total) * 100}%`;
    ruler.append(tick);
  }
}

function addMark(lane, start, end, kind, title) {
  const total = duration();
  if (!total) return;
  const mark = el("div", { class: `mark ${kind}`, title });
  mark.style.left = `${(start / total) * 100}%`;
  mark.style.width = `${Math.max(0, ((end - start) / total) * 100)}%`;
  lane.append(mark);
}

function renderTimeline() {
  const st = s();
  const videoLane = $("videoLane"), audioLane = $("audioLane");
  videoLane.querySelectorAll(".mark").forEach((node) => node.remove());
  audioLane.querySelectorAll(".mark").forEach((node) => node.remove());
  const diff = st.diff || { video: [], audio: [] };
  // Before resolution the backend reports conflicts via merge; show diff only outside of conflicts.
  for (const range of diff.video) addMark(videoLane, range.start, range.end, "diff", `Изменено: ${fmtRange(range.start, range.end)}`);
  for (const range of coalesce(diff.audio, 1 / fps())) addMark(audioLane, range.start, range.end, "diff", `Изменено: ${fmtRange(range.start, range.end)}`);
  if (st.merge) {
    for (const conflict of st.merge.conflicts) {
      if (conflict.choice) continue;
      const lane = conflict.kind === "video" ? videoLane : audioLane;
      addMark(lane, conflict.start_time, conflict.end_time, "conflict", `Конфликт: ${fmtRange(conflict.start_time, conflict.end_time)}`);
    }
  }
  renderRuler();
  renderSelection();
  positionPlayhead();
  drawWaveform();
}

let drag = null;
timeline.addEventListener("pointerdown", (event) => {
  if (!project() || event.button !== 0) return;
  timeline.setPointerCapture(event.pointerId);
  drag = { x: event.clientX, t: clientXToTime(event.clientX), moved: false };
});
timeline.addEventListener("pointermove", (event) => {
  if (!drag) return;
  if (!drag.moved && Math.abs(event.clientX - drag.x) < 4) return;
  drag.moved = true;
  setSelection(drag.t, clientXToTime(event.clientX));
});
const endDrag = (event) => {
  if (!drag) return;
  if (!drag.moved) seek(clientXToTime(event.clientX));
  drag = null;
};
timeline.addEventListener("pointerup", endDrag);
timeline.addEventListener("pointercancel", () => { drag = null; });

new ResizeObserver(() => { if (project()) renderTimeline(); }).observe(timeline);

/* waveform: drawn from the real working audio (WAV via /api/audio, ≤60 s per request) */

const MAX_WAVE_SECONDS = 300;

async function loadWaveform() {
  const st = s();
  const key = st.manifest;
  if (!key || state.waveKey === key || serverBusy()) return;
  state.waveKey = key;
  state.wavePeaks = null;
  drawWaveform();
  const total = Math.min(st.duration, MAX_WAVE_SECONDS);
  if (!total) return;
  const buckets = 2000;
  const peaks = new Float32Array(buckets * 2);
  try {
    for (let start = 0; start < total; start += MAX_AUDIO_SPAN) {
      const end = Math.min(total, start + MAX_AUDIO_SPAN);
      const response = await fetch(`/api/audio?ref=working&start=${start}&end=${end}`);
      if (!response.ok || state.waveKey !== key) return;
      const samples = parseWav(await response.arrayBuffer());
      if (!samples) return;
      const frameCount = samples.length / 2;
      for (let i = 0; i < frameCount; i += 8) {
        const t = start + (i / frameCount) * (end - start);
        const bucket = Math.min(buckets - 1, Math.floor((t / total) * buckets));
        const value = (Math.abs(samples[i * 2]) + Math.abs(samples[i * 2 + 1])) / 65536;
        if (value > peaks[bucket * 2]) peaks[bucket * 2] = value;
      }
      peaks[1] = total / st.duration; // fraction of the timeline covered
    }
  } catch {
    return;
  }
  if (state.waveKey !== key) return;
  state.wavePeaks = peaks;
  drawWaveform();
}

function parseWav(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 44 || view.getUint32(0, false) !== 0x52494646) return null; // "RIFF"
  let offset = 12;
  while (offset + 8 <= buffer.byteLength) {
    const id = view.getUint32(offset, false);
    const size = view.getUint32(offset + 4, true);
    if (id === 0x64617461) { // "data"
      const length = Math.min(size, buffer.byteLength - offset - 8);
      return new Int16Array(buffer.slice(offset + 8, offset + 8 + length - (length % 2)));
    }
    offset += 8 + size + (size % 2);
  }
  return null;
}

function drawWaveform() {
  const canvas = $("waveform");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.max(1, Math.round(rect.height * ratio));
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const peaks = state.wavePeaks;
  if (!peaks) return;
  const buckets = peaks.length / 2;
  const covered = peaks[1] || 1;
  const mid = canvas.height / 2;
  ctx.fillStyle = "rgba(140, 230, 190, 0.75)";
  const columns = Math.round(canvas.width * covered);
  for (let x = 0; x < columns; x++) {
    const from = Math.floor((x / columns) * buckets), to = Math.max(from + 1, Math.floor(((x + 1) / columns) * buckets));
    let peak = 0;
    for (let b = from; b < to; b++) peak = Math.max(peak, peaks[b * 2]);
    const h = Math.max(1, Math.min(1, peak * 1.8) * (mid - 2));
    ctx.fillRect(x, mid - h, 1, h * 2);
  }
}

/* ---------- edit panel ---------- */

function currentOperation() {
  return document.querySelector("input[name=op]:checked").value;
}

function renderOperationParams() {
  const op = currentOperation();
  document.querySelectorAll(".param").forEach((node) => {
    node.hidden = !node.dataset.for.split(" ").includes(op);
  });
  $("replaceLabel").textContent = {
    replace_audio: "Файл-источник (аудио или видео)",
    replace_av: "Видеофайл со звуком: обе дорожки заменятся вместе",
  }[op] || "Файл-источник (видео)";
  $("replaceFile").accept = op === "replace_audio" ? "audio/*,video/*" : "video/*";
}

async function applyEdit() {
  const sel = readRangeInputs();
  if (!sel) return;
  const op = currentOperation();
  const info = OPERATIONS[op];
  const payload = { action: "edit", operation: op, start: sel.start, end: sel.end };
  let file = null;
  if (info.upload) {
    file = $("replaceFile").files[0];
    if (!file) {
      toast("Выберите файл-источник для замены.", "error");
      return;
    }
    const offset = Number($("replaceOffset").value || 0);
    if (!Number.isFinite(offset) || offset < 0) {
      toast("Смещение должно быть неотрицательным числом секунд.", "error");
      return;
    }
    payload.offset = offset;
  }
  if (op === "fill") payload.value = $("fillColor").value;
  if (op === "gain") payload.value = Number($("gainValue").value);
  const result = await runAction(payload, { label: `${info.label}: ${fmtRange(sel.start, sel.end)}…`, file });
  if (result && file) $("replaceFile").value = "";
}

function readRangeInputs() {
  const start = parseTime($("rangeStart").value);
  const end = parseTime($("rangeEnd").value);
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    toast("Укажите начало и конец диапазона в формате мм:сс.сс или выделите его на таймлайне.", "error");
    return null;
  }
  if (end <= start) {
    toast("Конец диапазона должен быть позже начала.", "error");
    return null;
  }
  if (end > duration() + 1e-6) {
    toast(`Диапазон выходит за длительность ролика (${fmtTime(duration())}).`, "error");
    return null;
  }
  setSelection(start, end);
  return state.selection;
}

function formatRangeOnBlur() {
  const start = parseTime($("rangeStart").value);
  const end = parseTime($("rangeEnd").value);
  // Preserve a partially entered range until both fields are valid. Clearing
  // the first field on blur makes it impossible to enter the second one.
  if (Number.isFinite(start) && Number.isFinite(end) && end > start && end <= duration() + 1e-6) {
    renderSelection();
  }
}

function onRangeInput() {
  const start = parseTime($("rangeStart").value);
  const end = parseTime($("rangeEnd").value);
  $("rangeStart").setCustomValidity(Number.isFinite(start) ? "" : "Формат: мм:сс.сс");
  $("rangeEnd").setCustomValidity(Number.isFinite(end) ? "" : "Формат: мм:сс.сс");
  if (Number.isFinite(start) && Number.isFinite(end) && end > start && end <= duration() + 1e-6) {
    state.selection = { start: snap(start), end: snap(end) };
    const keep = document.activeElement;
    renderSelection();
    keep && keep.focus();
  }
}

/* ---------- versions ---------- */

// Sample-level audio diffs can split one edit into many slivers where old and new
// PCM values happen to match; join gaps shorter than one video frame for display.
function coalesce(ranges, gap) {
  const out = [];
  for (const range of ranges || []) {
    const last = out[out.length - 1];
    if (last && range.start - last.end <= gap) last.end = Math.max(last.end, range.end);
    else out.push({ ...range });
  }
  return out;
}

function intervalItems(diff, { onShow } = {}) {
  const items = [];
  for (const range of diff.video || []) items.push({ kind: "video", ...range });
  for (const range of coalesce(diff.audio, 1 / fps())) items.push({ kind: "audio", ...range });
  items.sort((a, b) => a.start - b.start || a.kind.localeCompare(b.kind));
  return items.map((range) => el("li", {},
    el("span", { class: `kind kind-${range.kind}` }, range.kind === "video" ? "Изобр." : "Звук"),
    el("span", { class: "iv-main" },
      el("span", { class: "range-text" }, fmtRange(range.start, range.end)),
      range.kind === "video" && Number.isFinite(range.first) ? el("span", { class: "muted" }, `кадры ${range.first}–${range.last - 1}`) : null),
    el("button", { type: "button", onclick: () => (onShow || showRange)(range) }, "Показать")));
}

function showRange(range) {
  setSelection(range.start, range.end);
  seek(range.start + 0.5 / fps());
}

function renderVersions() {
  const st = s();
  const box = $("draftBox");
  const merge = mergePending();
  const dirty = st.dirty;
  box.classList.toggle("dirty", dirty);
  if (merge) {
    $("draftTitle").textContent = "Идёт слияние — завершите или отмените его на вкладке «Слияние».";
  } else if (dirty) {
    const nv = st.diff.video.length, na = coalesce(st.diff.audio, 1 / fps()).length;
    $("draftTitle").textContent = `Черновик ветки ${st.branch}: изменено ${nv} ${plural(nv, "интервал", "интервала", "интервалов")} изображения и ${na} звука`;
  } else {
    $("draftTitle").textContent = `Нет несохранённых изменений в ветке ${st.branch}`;
  }
  $("discardBtn").hidden = !dirty || merge;
  $("draftIntervals").replaceChildren(...(dirty ? intervalItems(st.diff) : []));
  $("commitForm").hidden = merge;
  if (!$("commitAuthor").value) {
    try { $("commitAuthor").value = localStorage.getItem("vm-author") || ""; } catch { /* storage unavailable */ }
  }

  const list = $("historyList");
  list.replaceChildren(...st.history.map((commit) => {
    const isHead = commit.id === st.head;
    return el("li", { class: isHead ? "head" : "" },
      el("div", { class: "msg" }, commit.message),
      el("div", { class: "sub" },
        el("span", { class: "hash", title: commit.id }, shortHash(commit.id)),
        el("span", {}, fmtDate(commit.created)),
        el("span", {}, commit.author),
        el("span", { class: "refs" },
          commit.parents.length > 1 ? el("span", { class: "ref-tag merge" }, "слияние") : null,
          commit.branches.map((name) => el("span", { class: "ref-tag", title: name === st.branch ? "Текущая ветка" : "" }, (name === st.branch ? "● " : "") + name)))),
      el("div", { class: "row-actions" },
        el("button", { type: "button", "data-needs-idle": true, onclick: () => viewCommit(commit.id) }, "Смотреть"),
        el("button", { type: "button", onclick: () => openCompare(commit.id, "working") }, "Сравнить с рабочей"),
        el("button", { type: "button", "data-needs-switch": true, onclick: () => restoreCommit(commit) }, "Восстановить"),
        el("button", { type: "button", "data-needs-idle": true, onclick: () => runAction({ action: "export", ref: commit.id }, { label: `Экспорт версии ${shortHash(commit.id)} в MP4…` }) }, "MP4")));
  }));
}

async function restoreCommit(commit) {
  if (s().dirty) {
    const ok = await confirmAction("Восстановить версию?",
      `Текущий черновик ветки ${s().branch} будет заменён версией «${commit.message}». Сохранённые коммиты не изменятся.`, "Заменить черновик");
    if (!ok) return;
  }
  await runAction({ action: "restore", ref: commit.id }, { label: `Восстановление версии ${shortHash(commit.id)}…` });
  backToWorking();
}

async function commitDraft(event) {
  event.preventDefault();
  const message = $("commitMessage").value.trim();
  if (!message) {
    toast("Добавьте комментарий к версии.", "error");
    $("commitMessage").focus();
    return;
  }
  const author = $("commitAuthor").value.trim();
  try { localStorage.setItem("vm-author", author); } catch { /* storage unavailable */ }
  const payload = { action: "commit", message };
  if (author) payload.author = author;
  const result = await runAction(payload, { label: "Сохранение версии…" });
  if (result) $("commitMessage").value = "";
}

async function discardDraft() {
  const ok = await confirmAction("Сбросить черновик?",
    `Все несохранённые изменения ветки ${s().branch} будут удалены без возможности восстановления.`, "Сбросить");
  if (ok) await runAction({ action: "discard" }, { label: "Сброс черновика…" });
}

/* ---------- branches ---------- */

function branchRow(name, { incoming = false } = {}) {
  const st = s();
  const current = name === st.branch;
  const draft = st.drafts.includes(name);
  return el("li", { class: current ? "current" : "" },
    el("span", { class: "name" }, name),
    current ? el("span", { class: "chip chip-ok" }, "текущая") : null,
    draft ? el("span", { class: "chip", title: "У ветки есть несохранённый черновик" }, "черновик") : null,
    el("span", { class: "muted small grow hash", title: st.branches[name] }, shortHash(st.branches[name])),
    current ? null : el("button", { type: "button", "data-needs-switch": true, onclick: () => checkout(name) }, "Переключить"),
    current ? null : el("button", { type: "button", "data-needs-switch": true, onclick: () => prepareMerge(name) }, "Влить в текущую"),
    incoming || current ? null : el("button", { type: "button", onclick: () => openCompare(name, st.branch) }, "Сравнить"));
}

function renderBranches() {
  const st = s();
  const local = localBranches();
  $("branchList").replaceChildren(...local.map((name) => branchRow(name)));
  const incoming = st.incoming || [];
  $("incomingTitle").hidden = !incoming.length;
  $("incomingList").replaceChildren(...incoming.map((name) => branchRow(name, { incoming: true })));
}

async function checkout(name) {
  if (name === s().branch) return;
  const result = await runAction({ action: "checkout", name }, { label: `Переключение на ${name}…` });
  if (result) backToWorking();
}

async function createBranch(event) {
  event.preventDefault();
  const name = $("branchName").value.trim();
  if (!BRANCH_RE.test(name) || name.includes("..") || name.endsWith("/") || name === "working") {
    toast("Имя ветки: латинские буквы, цифры, «.», «_», «-», «/»; до 80 символов.", "error");
    return;
  }
  if (s().branches[name]) {
    toast("Ветка с таким именем уже существует.", "error");
    return;
  }
  const result = await runAction({ action: "branch", name }, { label: `Создание ветки ${name}…` });
  if (result) $("branchName").value = "";
}

/* ---------- compare ---------- */

function refOptions(selected) {
  const st = s();
  const options = [el("option", { value: "working", selected: selected === "working" }, "Рабочая версия")];
  const branchGroup = el("optgroup", { label: "Ветки" },
    Object.keys(st.branches).map((name) => el("option", { value: name, selected: selected === name }, name)));
  const commitGroup = el("optgroup", { label: "Коммиты" },
    st.history.map((commit) => el("option", { value: commit.id, selected: selected === commit.id },
      `${shortHash(commit.id)} · ${commit.message.slice(0, 40)}`)));
  return [...options, branchGroup, commitGroup];
}

function renderCompareRefs() {
  const left = $("cmpLeft").value || s().branch;
  const right = $("cmpRight").value || "working";
  $("cmpLeft").replaceChildren(...refOptions(left));
  $("cmpRight").replaceChildren(...refOptions(right));
  if (state.compare) loadCompareFrames();
}

function refLabel(ref) {
  if (ref === "working") return "рабочая версия";
  if (s().branches[ref]) return `ветка ${ref}`;
  return `коммит ${shortHash(ref)}`;
}

async function openCompare(left, right) {
  selectTab("compare");
  renderCompareRefs();
  $("cmpLeft").value = left;
  $("cmpRight").value = right;
  await runCompare();
}

async function runCompare() {
  const left = $("cmpLeft").value, right = $("cmpRight").value;
  if (serverBusy()) {
    toast("Дождитесь завершения текущей операции.", "error");
    return;
  }
  const box = $("cmpResult");
  box.replaceChildren(el("p", { class: "muted" }, "Сравнение…"));
  try {
    const diff = await api(`/api/diff?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
    state.compare = { left, right, diff };
    const items = intervalItems(diff, { onShow: (range) => {
      showRange(range);
      $("cmpFrame").value = range.kind === "video" && Number.isFinite(range.first) ? range.first : frameAt(range.start);
      loadCompareFrames();
    } });
    box.replaceChildren(
      el("div", { class: "summary" },
        `${refLabel(left)} → ${refLabel(right)}: `,
        diff.video_frames || diff.audio_samples
          ? `изменено ${diff.video_frames} ${plural(diff.video_frames, "кадр", "кадра", "кадров")}, ${(diff.audio_samples / 48000).toFixed(2)} с звука`
          : "различий нет"),
      items.length ? el("ul", { class: "intervals" }, items) : null);
    $("cmpFrames").hidden = false;
    const first = diff.video[0];
    $("cmpFrame").value = first && Number.isFinite(first.first) ? first.first : frameAt(state.time);
    $("cmpAudioPair").hidden = true;
    loadCompareFrames();
  } catch (error) {
    state.compare = null;
    $("cmpFrames").hidden = true;
    box.replaceChildren(el("p", { class: "muted" }, error.message));
  }
}

function frameUrl(ref, frame, diffWith) {
  let url = `/api/frame?ref=${encodeURIComponent(ref)}&frame=${frame}`;
  if (diffWith) url += `&diff_with=${encodeURIComponent(diffWith)}`;
  if (ref === "working" || diffWith === "working") url += `&v=${s().manifest}`;
  return url;
}

function loadCompareFrames() {
  if (!state.compare || serverBusy()) return;
  const { left, right } = state.compare;
  const frame = Math.max(0, Math.min(frames() - 1, Math.round(Number($("cmpFrame").value) || 0)));
  $("cmpFrame").value = frame;
  $("cmpImgLeft").src = frameUrl(left, frame);
  $("cmpImgRight").src = frameUrl(right, frame);
  $("cmpImgDiff").src = frameUrl(left, frame, right);
}

function loadCompareAudio() {
  if (!state.compare) return;
  const range = audioRange();
  if (!range) return;
  const { left, right } = state.compare;
  $("cmpAudioLeft").src = audioUrl(left, range.start, range.end);
  $("cmpAudioRight").src = audioUrl(right, range.start, range.end);
  $("cmpAudioPair").hidden = false;
}

function audioRange() {
  const sel = state.selection;
  if (!sel) {
    toast("Выделите диапазон на таймлайне (до 60 секунд), чтобы прослушать звук.", "error");
    return null;
  }
  const end = Math.min(sel.end, sel.start + MAX_AUDIO_SPAN);
  if (end < sel.end) toast("Звук ограничен первыми 60 секундами выбранного диапазона.", "info");
  return { start: sel.start, end };
}

function audioUrl(ref, start, end) {
  const version = ref === "working" ? `&v=${s().manifest}` : "";
  return `/api/audio?ref=${encodeURIComponent(ref)}&start=${start.toFixed(4)}&end=${end.toFixed(4)}${version}`;
}

/* ---------- merge ---------- */

function prepareMerge(name) {
  selectTab("merge");
  $("mergeSource").value = name;
  updateControls();
}

function renderMerge() {
  const st = s();
  const merge = st.merge;
  $("mergeIdle").hidden = Boolean(merge);
  $("mergeActive").hidden = !merge;
  const groups = merge ? groupConflicts(merge.conflicts) : [];
  const unresolved = groups.filter((g) => g.items.some((c) => !c.choice)).length;
  $("mergeBadge").hidden = !merge;
  $("mergeBadge").textContent = merge ? String(unresolved || "✓") : "";
  if (!merge) {
    $("mergeTarget").textContent = st.branch;
    const previous = $("mergeSource").value;
    const sources = Object.keys(st.branches).filter((name) => name !== st.branch);
    $("mergeSource").replaceChildren(
      el("option", { value: "" }, sources.length ? "— выберите ветку —" : "нет других веток"),
      ...sources.map((name) => el("option", { value: name, selected: name === previous },
        (st.incoming || []).includes(name) ? `${name} (входящая)` : name)));
    $("mergeHint").textContent = st.dirty
      ? "Сначала сохраните или сбросьте черновик текущей ветки: слияние выполняется между сохранёнными версиями."
      : "Если конфликтов нет, коммит слияния создаётся автоматически.";
    return;
  }
  const total = groups.length;
  const summary = $("mergeSummary");
  summary.className = `merge-summary ${unresolved ? "pending" : "ready"}`;
  summary.replaceChildren(
    el("div", {}, el("strong", {}, `Слияние ${merge.source} → ${st.branch}`)),
    el("div", {}, unresolved
      ? `Не разрешено ${unresolved} из ${total} ${plural(total, "конфликта", "конфликтов", "конфликтов")}. Выберите вариант для каждого.`
      : `Все ${total} ${plural(total, "конфликт разрешён", "конфликта разрешены", "конфликтов разрешены")}. Можно завершить слияние.`),
    el("div", { class: "muted small" }, "Независимые правки обеих веток уже включены. Пока конфликт не разрешён, в предпросмотре показана наша версия."));
  if (!$("mergeMessage").value) $("mergeMessage").value = `Слияние ${merge.source} → ${st.branch}`;
  const refs = { base: merge.base, ours: merge.parents[0], theirs: merge.parents[1] };
  $("conflictList").replaceChildren(...groups.map((group) => conflictRow(group, refs)));
}

// The backend may split one region into many adjacent conflicts (audio is compared per
// PCM value). Neighbouring conflicts of the same kind closer than one video frame are
// shown as one group; a choice is sent as a resolve call for every member.
function groupConflicts(conflicts) {
  const sorted = [...conflicts].sort((a, b) => a.kind.localeCompare(b.kind) || a.start_time - b.start_time);
  const groups = [];
  for (const conflict of sorted) {
    const last = groups[groups.length - 1];
    if (last && last.kind === conflict.kind && conflict.start_time - last.end_time <= 1 / fps()) {
      last.items.push(conflict);
      last.end_time = Math.max(last.end_time, conflict.end_time);
      last.end = Math.max(last.end, conflict.end);
    } else {
      groups.push({ kind: conflict.kind, start: conflict.start, end: conflict.end,
        start_time: conflict.start_time, end_time: conflict.end_time, items: [conflict] });
    }
  }
  for (const group of groups) {
    const choices = new Set(group.items.map((c) => c.choice));
    group.choice = choices.size === 1 ? group.items[0].choice : "mixed";
  }
  return groups.sort((a, b) => a.start_time - b.start_time || a.kind.localeCompare(b.kind));
}

function resolveGroup(group, choice) {
  const todo = group.items.filter((c) => c.choice !== choice);
  if (!todo.length) return;
  const label = CHOICES[choice].toLowerCase();
  runActions(todo.map((c) => ({ action: "resolve", conflict: c.id, choice })), {
    labels: todo.map((_, i) => todo.length > 1 ? `Выбор (${label}): часть ${i + 1} из ${todo.length}…` : `Выбор: ${label} версия…`),
    quiet: true,
  });
}

function conflictRow(group, refs) {
  const isVideo = group.kind === "video";
  const frame = isVideo ? group.start : frameAt(group.start_time);
  const audioEnd = Math.min(group.end_time, group.start_time + MAX_AUDIO_SPAN);
  const resolved = group.choice && group.choice !== "mixed";
  const choices = Object.entries(CHOICES).map(([choice, label]) => el("div", { class: "choice" },
    el("span", { class: "choice-label" }, label),
    isVideo
      ? el("img", { src: `/api/frame?ref=${refs[choice]}&frame=${frame}`, alt: `${label}: кадр ${frame}`, loading: "lazy",
        title: "Открыть кадр в новой вкладке", onclick: () => window.open(`/api/frame?ref=${refs[choice]}&frame=${frame}`, "_blank", "noopener") })
      : el("audio", { controls: true, preload: "none", src: audioUrl(refs[choice], group.start_time, Math.max(audioEnd, group.start_time + 0.05)) }),
    el("button", { type: "button", class: group.choice === choice ? "chosen" : "", "data-needs-idle": true,
      "aria-pressed": String(group.choice === choice), onclick: () => resolveGroup(group, choice) },
    group.choice === choice ? "✓ Выбрано" : "Взять")));
  const frameCount = isVideo ? group.end - group.start : 0;
  const parts = group.items.length;
  return el("li", { class: `conflict ${resolved ? "resolved" : ""}` },
    el("div", { class: "conflict-head" },
      el("span", { class: `kind kind-${group.kind}` }, isVideo ? "Изобр." : "Звук"),
      el("span", { class: "range-text grow" }, fmtRange(group.start_time, group.end_time),
        el("span", { class: "muted small" },
          isVideo ? ` · ${frameCount} ${plural(frameCount, "кадр", "кадра", "кадров")}` : "",
          parts > 1 ? ` · ${parts} ${plural(parts, "часть", "части", "частей")}` : "",
          group.choice === "mixed" ? " · выбор различается" : "")),
      el("button", { type: "button", class: "small-btn", onclick: () => showRange({ start: group.start_time, end: group.end_time }) }, "Показать")),
    el("div", { class: "choices" }, choices));
}

async function startMerge() {
  const source = $("mergeSource").value;
  if (!source) return;
  if (s().dirty) {
    toast("Сначала сохраните или сбросьте черновик текущей ветки.", "error");
    return;
  }
  $("mergeMessage").value = "";  // renderMerge fills the default text for the new merge
  await runAction({ action: "merge", source }, { label: `Слияние ${source} → ${s().branch}…` });
}

async function finishMerge(event) {
  event.preventDefault();
  const merge = s().merge;
  if (!merge) return;
  if (merge.conflicts.some((c) => !c.choice)) {
    toast("Разрешите все конфликты перед завершением слияния.", "error");
    return;
  }
  const message = $("mergeMessage").value.trim();
  if (!message) {
    toast("Добавьте комментарий к коммиту слияния.", "error");
    return;
  }
  const payload = { action: "commit", message };
  const author = $("commitAuthor").value.trim();
  if (author) payload.author = author;
  const result = await runAction(payload, { label: "Сохранение коммита слияния…" });
  if (result) $("mergeMessage").value = "";
}

async function cancelMerge() {
  const ok = await confirmAction("Отменить слияние?",
    "Результат слияния и все выбранные варианты конфликтов будут удалены. Ветка вернётся к последнему коммиту.", "Отменить слияние");
  if (!ok) return;
  const result = await runAction({ action: "cancel_merge" }, { label: "Отмена слияния…" });
  if (result) $("mergeMessage").value = "";
}

/* ---------- repository exchange ---------- */

let pendingRepoFile = null;
const peerDialog = $("peerDialog");

function askPeer(file) {
  pendingRepoFile = file;
  $("peerFileName").textContent = `Файл: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} МБ)`;
  peerDialog.showModal();
  $("peerName").focus();
}

$("peerForm").addEventListener("submit", (event) => {
  const peer = $("peerName").value.trim();
  if (!PEER_RE.test(peer)) {
    event.preventDefault();
    toast("Имя участника: латинские буквы, цифры, «.», «_», «-».", "error");
    return;
  }
  const file = pendingRepoFile;
  pendingRepoFile = null;
  $("hiddenRepoFile").value = "";
  setTimeout(() => runAction({ action: "bundle_import", peer }, { label: "Импорт репозитория…", file }), 0);
});
$("peerCancel").addEventListener("click", () => {
  pendingRepoFile = null;
  $("hiddenRepoFile").value = "";
  peerDialog.close();
});

/* ---------- projects (workspace mode) ---------- */

function fmtBytes(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${unit ? value.toFixed(value < 10 ? 2 : 1) : value} ${units[unit]}`;
}

let projectsRequest = null;
async function loadProjects() {
  if (projectsRequest || state.projectsFor === state.status) return;
  state.projectsFor = state.status;
  projectsRequest = api("/api/projects").then((data) => {
    state.projects = data;
    renderProjects();
  }).catch((error) => toast(error.message, "error")).finally(() => { projectsRequest = null; });
}

function renderProjects() {
  const data = state.projects;
  if (!data) return;
  $("workspaceRoot").textContent = `Папка списка: ${data.root}`;
  $("projectListEmpty").hidden = data.projects.length > 0;
  $("projectList").replaceChildren(...data.projects.map((item) => {
    const size = item.storage && (item.storage.total_bytes ?? item.storage.folder_bytes);
    const meta = [
      item.available ? (item.initialised ? `видео: ${item.video_name}` : "пустой — нужно импортировать видео") : "папка не найдена",
      size !== undefined && size !== null ? `размер: ${fmtBytes(size)}` : null,
      item.legacy ? "старое хранение кадров — можно сделать компактную копию" : null,
      item.cloud ? "общая папка подключена" : null,
      item.opened ? `открыт ${fmtDate(item.opened)}` : `создан ${fmtDate(item.created)}`,
    ].filter(Boolean);
    return el("li", { class: `project-item ${item.available ? "" : "missing"}` },
      el("div", { class: "project-title" }, item.name),
      el("div", { class: "project-actions" },
        el("button", { type: "button", class: "primary", "data-project-action": true, disabled: !item.available,
          onclick: () => runAction({ action: "project_open", id: item.id }, { label: `Открытие «${item.name}»…` }) }, "Открыть"),
        item.initialised ? el("button", { type: "button", "data-project-action": true,
          title: "Новая папка с той же историей и компактным хранением кадров",
          onclick: () => askMigrate(item) }, "Компактная копия") : null,
        el("button", { type: "button", class: "danger-ghost", "data-project-action": true, onclick: () => forgetProject(item) },
          "Убрать")),
      el("div", { class: "project-meta" }, meta.map((text) => el("span", {}, text))),
      el("div", { class: "project-meta muted small", title: item.path }, item.path));
  }));
  updateControls();
}

async function forgetProject(item) {
  const ok = await confirmAction("Убрать проект из списка?",
    `«${item.name}» исчезнет из списка. Папка ${item.path} и все данные в ней останутся на диске.`, "Убрать из списка");
  if (ok) await runAction({ action: "project_forget", id: item.id }, { label: "Обновление списка…" });
}

let migrateTarget = null;
function askMigrate(item) {
  migrateTarget = item;
  $("migrateName").textContent = `Исходный проект: ${item.name} (${item.path})`;
  $("migrateSource").value = "";
  $("migrateDialog").showModal();
}
$("migrateForm").addEventListener("submit", () => {
  const item = migrateTarget;
  migrateTarget = null;
  if (!item) return;
  const payload = { action: "project_migrate", id: item.id };
  const original = $("migrateSource").value.trim();
  if (original) payload.source_media = original;
  setTimeout(async () => {
    const result = await runAction(payload, { label: `Компактная копия «${item.name}»…` });
    if (result && result.report) {
      const r = result.report;
      toast(`Кадров: ${r.frames}. Из исходного файла: ${r.from_original}, как эффект яркости: ${r.recovered_luts || 0}, ` +
        `упаковано lossless: ${r.packed}, перенесено рецептов: ${r.kept_recipes}, оставлено как есть: ${r.fallback}. ` +
        `Проверено бит в бит: ${r.verified_frames}.` +
        (r.original_matched === false ? " Исходный файл не совпал с первой версией и не использован." : ""), "info");
    }
  }, 0);
});
$("migrateCancel").addEventListener("click", () => { migrateTarget = null; $("migrateDialog").close(); });

async function findCloudProjects(event) {
  event.preventDefault();
  const folder = $("cloneFolder").value.trim();
  if (!folder) return;
  try {
    const data = await api(`/api/cloud/projects?folder=${encodeURIComponent(folder)}`);
    state.cloneCandidates = data.projects.flatMap((p) => p.devices.map((device) => ({ ...p, device })));
    if (!state.cloneCandidates.length) {
      toast("В этой папке нет опубликованных проектов Vibemogging (или они ещё синхронизируются).", "error");
      $("cloneForm").hidden = true;
      return;
    }
    $("cloneSource").replaceChildren(...state.cloneCandidates.map((c, i) =>
      el("option", { value: String(i) }, `${c.name} — устройство ${c.device}`)));
    $("cloneName").value = state.cloneCandidates[0].name || "";
    $("cloneForm").hidden = false;
  } catch (error) {
    toast(error.message, "error");
  }
}

function cloneFromCloud(event) {
  event.preventDefault();
  const candidate = state.cloneCandidates[Number($("cloneSource").value)];
  const device = $("cloneDevice").value.trim();
  if (!candidate) return;
  if (!PEER_RE.test(device)) {
    toast("Имя этого компьютера: латинские буквы, цифры, «.», «_», «-».", "error");
    return;
  }
  if (device === candidate.device) {
    toast("Имя этого компьютера должно отличаться от устройства-источника.", "error");
    return;
  }
  runAction({ action: "project_clone_cloud", folder: $("cloneFolder").value.trim(), project: candidate.id,
    source_device: candidate.device, device, name: $("cloneName").value.trim() || candidate.name },
  { label: "Получение общего проекта…" });
}

/* ---------- storage and cloud panel ---------- */

function storageRows(target, rows) {
  target.replaceChildren(...rows.filter(([, value]) => value !== null && value !== undefined)
    .flatMap(([label, value]) => [el("dt", {}, label), el("dd", {}, value)]));
}

function renderStorage() {
  const st = s().storage || {};
  const cache = s().preview_cache || {};
  storageRows($("storageInfo"), [
    ["Папка проекта", st.folder_bytes !== undefined ? fmtBytes(st.folder_bytes) : null],
    ["Исходники", st.asset_bytes !== undefined ? `${fmtBytes(st.asset_bytes)} (${st.assets})` : null],
    ["Объекты", st.object_bytes !== undefined ? `${fmtBytes(st.object_bytes)} (${st.objects})` : null],
    ["Рецепты кадров", st.recipes !== undefined ? `${st.recipes} · индекс ${fmtBytes(st.index_bytes)}` : null],
    ["Предпросмотры", cache.preview_limit ? `${fmtBytes(cache.preview_bytes)} из ${fmtBytes(cache.preview_limit)} (${cache.preview_files})` : null],
    ["Расположение", s().data_dir],
  ]);
}

function renderCloudBasics() {
  const cloud = s().cloud || {};
  $("cloudOff").hidden = Boolean(cloud.connected);
  $("cloudOn").hidden = !cloud.connected;
  if (cloud.error) toast(cloud.error, "error");
  if (cloud.connected && document.querySelector('.tab[data-tab="cloud"]').classList.contains("active") && state.cloudStale !== false) {
    loadCloud();
  }
  if (!cloud.connected) state.cloud = null;
  renderCloud();
}

let cloudRequest = null;
async function loadCloud() {
  if (cloudRequest || serverBusy()) return;
  state.cloudStale = false;
  cloudRequest = api("/api/cloud").then((data) => {
    state.cloud = data;
    renderCloud();
  }).catch((error) => toast(error.message, "error")).finally(() => { cloudRequest = null; });
}

function renderCloud() {
  const data = state.cloud;
  const basics = s().cloud || {};
  storageRows($("cloudInfo"), [
    ["Это устройство", basics.device],
    ["Папка", basics.folder],
    ["Последняя публикация", data ? (data.published_at
      ? `${fmtDate(data.published_at)} · ${Object.keys(data.published_branches || {}).join(", ")}` : "ещё не было") : "…"],
  ]);
  const devices = data ? data.devices : [];
  $("cloudDevices").replaceChildren(...(devices.length ? devices.map((device) => {
    const latest = device.latest;
    let status, kind = "";
    if (!latest) {
      status = device.unreadable ? "Снимок ещё синхронизируется." : "Ещё ничего не опубликовано.";
      kind = "warn";
    } else if (!latest.complete) {
      status = `Облако ещё синхронизирует: не хватает файлов — ${latest.missing_files} (${fmtBytes(latest.missing_bytes)}). Проект не изменится, пока всё не придёт.`;
      kind = "warn";
    } else if (latest.received) {
      status = `Получено: ${fmtDate(latest.created)}. Ветки: ${Object.keys(latest.branches).map((b) => `${device.device}/${b}`).join(", ")}.`;
      kind = "ok";
    } else {
      status = `Новый снимок от ${fmtDate(latest.created)}: ветки ${Object.keys(latest.branches).join(", ")}.`;
    }
    return el("li", {},
      el("span", { class: "name grow" }, device.device),
      el("button", { type: "button", "data-needs-switch": latest && latest.complete ? true : undefined,
        disabled: !(latest && latest.complete), title: latest && latest.complete ? "" : "Дождитесь окончания синхронизации",
        onclick: () => receiveFrom(device.device) }, latest && latest.received ? "Получить снова" : "Получить"),
      el("span", { class: `device-status ${kind}` }, status));
  }) : [el("li", { class: "empty-note" }, data ? "Других устройств пока нет. На втором компьютере выберите «Получить общий проект из облака»." : "Загрузка…")]));
  updateControls();
}

async function receiveFrom(device) {
  const result = await runAction({ action: "cloud_receive", device }, { label: `Получение версий ${device}…` });
  if (result && result.branches && result.branches.length) prepareMerge(result.branches[0]);
}

function connectCloud(event) {
  event.preventDefault();
  const device = $("cloudDevice").value.trim();
  if (!PEER_RE.test(device)) {
    toast("Имя этого компьютера: латинские буквы, цифры, «.», «_», «-»; до 40 символов.", "error");
    return;
  }
  runAction({ action: "cloud_connect", folder: $("cloudFolder").value.trim(), device }, { label: "Подключение общей папки…" });
}

async function disconnectCloud() {
  const ok = await confirmAction("Отключить общую папку?",
    "Этот проект перестанет публиковать и получать версии. Файлы в общей папке и полученные ветки останутся.", "Отключить");
  if (ok) await runAction({ action: "cloud_disconnect" }, { label: "Отключение…" });
}

/* ---------- tabs ---------- */

function selectTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".tab-body").forEach((body) => { body.hidden = body.dataset.body !== name; });
  try { localStorage.setItem("vm-tab", name); } catch { /* storage unavailable */ }
  if (name === "cloud" && s().cloud && s().cloud.connected) loadCloud();
}

/* ---------- wiring ---------- */

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));
document.querySelectorAll(".seg-btn").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.view)));

$("importVideoFile").addEventListener("change", updateControls);
$("openRepoFile").addEventListener("change", updateControls);
$("importVideoBtn").addEventListener("click", () => {
  const file = $("importVideoFile").files[0];
  if (file) runAction({ action: "import", name: file.name }, { label: "Импорт видео…", file });
});
$("openRepoBtn").addEventListener("click", () => {
  const file = $("openRepoFile").files[0];
  if (file) runAction({ action: "bundle_import" }, { label: "Открытие репозитория…", file });
});

$("branchSelect").addEventListener("change", (event) => {
  const name = event.target.value;
  event.target.value = s().branch;
  checkout(name);
});
$("exportMp4Btn").addEventListener("click", () => runAction({ action: "export" }, { label: "Экспорт рабочей версии в MP4…" }));
$("exportRepoBtn").addEventListener("click", () => {
  if (s().dirty) {
    toast("Сохраните или сбросьте черновик: в .vmrepo попадают только сохранённые версии.", "error");
    return;
  }
  runAction({ action: "bundle_export" }, { label: "Упаковка репозитория…" });
});
$("importRepoBtn").addEventListener("click", () => $("hiddenRepoFile").click());
$("hiddenRepoFile").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (file) askPeer(file);
});

$("renderPreviewBtn").addEventListener("click", () => runAction({ action: "preview" }, { label: "Подготовка предпросмотра…" }));
$("backToWorkingBtn").addEventListener("click", backToWorking);
$("playBtn").addEventListener("click", togglePlay);
$("prevFrameBtn").addEventListener("click", () => { video.pause(); seekFrame(frameAt(state.time) - 1); });
$("nextFrameBtn").addEventListener("click", () => { video.pause(); seekFrame(frameAt(state.time) + 1); });
$("gotoFrameBtn").addEventListener("click", () => { video.pause(); seekFrame(Number($("frameInput").value) || 0); });
$("frameInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { video.pause(); seekFrame(Number($("frameInput").value) || 0); }
});

$("rangeStart").addEventListener("input", onRangeInput);
$("rangeEnd").addEventListener("input", onRangeInput);
$("rangeStart").addEventListener("blur", formatRangeOnBlur);
$("rangeEnd").addEventListener("blur", formatRangeOnBlur);
$("startFromPlayhead").addEventListener("click", () => {
  const start = snap(frameAt(state.time) / fps());
  const end = state.selection && state.selection.end > start ? state.selection.end : Math.min(duration(), start + 1);
  setSelection(start, end);
});
$("endFromPlayhead").addEventListener("click", () => {
  const end = snap((frameAt(state.time) + 1) / fps());
  const start = state.selection && state.selection.start < end ? state.selection.start : Math.max(0, end - 1);
  setSelection(start, end);
});
document.querySelectorAll("input[name=op]").forEach((radio) => radio.addEventListener("change", renderOperationParams));
$("gainValue").addEventListener("input", () => { $("gainOut").textContent = `×${Number($("gainValue").value).toFixed(2)}`; });
$("applyEditBtn").addEventListener("click", applyEdit);

$("commitForm").addEventListener("submit", commitDraft);
$("discardBtn").addEventListener("click", discardDraft);
$("branchForm").addEventListener("submit", createBranch);
$("cmpRunBtn").addEventListener("click", runCompare);
$("cmpFrame").addEventListener("change", loadCompareFrames);
$("cmpFromPlayhead").addEventListener("click", () => { $("cmpFrame").value = frameAt(state.time); loadCompareFrames(); });
$("cmpAudioLoad").addEventListener("click", loadCompareAudio);
$("mergeSource").addEventListener("change", updateControls);
$("mergeBtn").addEventListener("click", startMerge);
$("mergeCommitForm").addEventListener("submit", finishMerge);
$("cancelMergeBtn").addEventListener("click", cancelMerge);

$("backToProjectsBtn").addEventListener("click", () => {
  video.pause();
  backToWorking();
  runAction({ action: "project_close" }, { label: "Закрытие проекта…" });
});
$("createProjectForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = $("newProjectName").value.trim();
  if (!name) return;
  runAction({ action: "project_create", name }, { label: `Создание проекта «${name}»…` })
    .then((result) => { if (result) $("newProjectName").value = ""; });
});
$("registerForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const path = $("registerPath").value.trim();
  if (path) runAction({ action: "project_register", path }, { label: "Добавление папки…" });
});
$("cloudFolderForm").addEventListener("submit", findCloudProjects);
$("cloneForm").addEventListener("submit", cloneFromCloud);
$("cloudConnectForm").addEventListener("submit", connectCloud);
$("cloudRefreshBtn").addEventListener("click", loadCloud);
$("cloudPublishBtn").addEventListener("click", () => runAction({ action: "cloud_publish" }, { label: "Публикация версий в общую папку…" }));
$("cloudDisconnectBtn").addEventListener("click", disconnectCloud);

document.addEventListener("keydown", (event) => {
  if (!project() || event.target.closest("input, textarea, select, dialog, button") || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === " ") { event.preventDefault(); togglePlay(); }
  else if (event.key === "ArrowLeft") { event.preventDefault(); video.pause(); seekFrame(frameAt(state.time) - (event.shiftKey ? Math.round(fps()) : 1)); }
  else if (event.key === "ArrowRight") { event.preventDefault(); video.pause(); seekFrame(frameAt(state.time) + (event.shiftKey ? Math.round(fps()) : 1)); }
});

window.addEventListener("beforeunload", (event) => {
  if (state.busy) event.preventDefault();
});

try {
  const tab = localStorage.getItem("vm-tab");
  if (tab && document.querySelector(`.tab[data-tab="${tab}"]`)) selectTab(tab);
} catch { /* storage unavailable */ }
renderOperationParams();
refreshStatus();
