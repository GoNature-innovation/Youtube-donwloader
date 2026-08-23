"use strict";

const $ = (id) => document.getElementById(id);

const el = {
  urls: $("urls"), paste: $("paste"), start: $("start"), cancel: $("cancel"),
  copy: $("copy"), modes: $("modes"), quality: $("quality"),
  audioFormat: $("audio_format"), transcript: $("transcript"), lang: $("lang"),
  browser: $("browser"), cookies: $("cookies"), subfolder: $("subfolder"),
  timestamps: $("timestamps"), keepVtt: $("keep_vtt"), playlist: $("playlist"),
  bar: $("bar"), status: $("status"), log: $("log"), files: $("files"),
  refresh: $("refresh"),
};

let mode = "video";
let jobId = null;
let source = null;

// --------------------------------------------------------------------------- //
// Mode chips — the same three the desktop build had
// --------------------------------------------------------------------------- //

el.modes.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  mode = chip.dataset.mode;
  for (const c of el.modes.children) c.classList.toggle("selected", c === chip);
  syncMode();
});

function syncMode() {
  el.quality.disabled = mode !== "video";
  el.audioFormat.disabled = mode !== "audio";
  // "Transcript only" implies a transcript, so the toggle has nothing to say.
  el.transcript.disabled = mode === "transcript";
  if (mode === "transcript") el.transcript.checked = true;
}

// --------------------------------------------------------------------------- //
// Log
// --------------------------------------------------------------------------- //

function classify(line) {
  const text = line.trim();
  if (text.startsWith("===")) return "muted";
  if (text.startsWith("error") || text.includes("cancelled")) return "err";
  if (text.startsWith("media") || text.startsWith("transcript")) {
    return text.includes("->") && !text.includes("none") ? "ok" : "muted";
  }
  return "title";
}

function appendLog(line) {
  const span = document.createElement("span");
  span.className = classify(line);
  span.textContent = line + "\n";
  el.log.appendChild(span);
  el.log.scrollTop = el.log.scrollHeight;
}

function setStatus(text) { el.status.textContent = text; }
function setProgress(pct) { el.bar.style.width = `${Math.max(0, Math.min(100, pct))}%`; }

// --------------------------------------------------------------------------- //
// Files
// --------------------------------------------------------------------------- //

function humanSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function renderFiles(files) {
  el.files.textContent = "";
  if (!files.length) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "Nothing downloaded yet.";
    el.files.appendChild(empty);
    return;
  }
  for (const file of files) {
    const row = document.createElement("li");
    const link = document.createElement("a");
    link.href = `/files/${file.path.split("/").map(encodeURIComponent).join("/")}`;
    link.textContent = file.path;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = humanSize(file.size);
    row.append(link, size);
    el.files.appendChild(row);
  }
}

async function refreshFiles() {
  const response = await fetch("/api/files");
  const data = await response.json();
  renderFiles(data.files);
}

// --------------------------------------------------------------------------- //
// Running a job
// --------------------------------------------------------------------------- //

function formData() {
  const data = new FormData();
  data.set("urls", el.urls.value);
  data.set("mode", mode);
  data.set("subfolder", el.subfolder.value);
  data.set("quality", el.quality.value);
  data.set("audio_format", el.audioFormat.value);
  data.set("transcript", el.transcript.checked ? "1" : "0");
  data.set("lang", el.lang.value);
  data.set("browser", el.browser.value);
  data.set("timestamps", el.timestamps.checked ? "1" : "0");
  data.set("keep_vtt", el.keepVtt.checked ? "1" : "0");
  data.set("playlist", el.playlist.checked ? "1" : "0");
  if (el.cookies.files[0]) data.set("cookies", el.cookies.files[0]);
  return data;
}

async function start() {
  if (!el.urls.value.trim()) {
    setStatus("Paste at least one link");
    return;
  }

  el.start.disabled = true;
  el.cancel.disabled = false;
  el.copy.disabled = true;
  el.log.textContent = "";
  setProgress(0);
  setStatus("Starting…");

  let data;
  try {
    const response = await fetch("/api/jobs", { method: "POST", body: formData() });
    data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
  } catch (error) {
    appendLog(`error: ${error.message}`);
    finish();
    return;
  }

  jobId = data.id;
  listen(jobId);
}

function listen(id) {
  if (source) source.close();
  source = new EventSource(`/api/jobs/${id}/events`);

  source.addEventListener("log", (e) => appendLog(JSON.parse(e.data)));
  source.addEventListener("status", (e) => setStatus(JSON.parse(e.data)));
  source.addEventListener("progress", (e) => setProgress(JSON.parse(e.data)));
  source.addEventListener("file", () => {
    refreshFiles();
    // A transcript landed, so the copy button has something to fetch.
    el.copy.disabled = false;
  });
  source.addEventListener("done", (e) => {
    const result = JSON.parse(e.data);
    if (result.cancelled) setStatus("Cancelled");
    else if (result.failures) setStatus(`Finished · ${result.failures} failed`);
    else { setStatus("Done"); setProgress(100); }
    finish();
    refreshFiles();
  });
  source.onerror = () => {
    // EventSource reconnects on its own; the stream replays from the start.
    if (source.readyState === EventSource.CLOSED) finish();
  };
}

function finish() {
  if (source) { source.close(); source = null; }
  el.start.disabled = false;
  el.cancel.disabled = true;
}

async function cancel() {
  if (!jobId) return;
  el.cancel.disabled = true;
  await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}

// --------------------------------------------------------------------------- //
// Clipboard — plain HTTP has no navigator.clipboard, so keep a fallback
// --------------------------------------------------------------------------- //

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  const scratch = document.createElement("textarea");
  scratch.value = text;
  scratch.style.position = "fixed";
  scratch.style.opacity = "0";
  document.body.appendChild(scratch);
  scratch.select();
  const ok = document.execCommand("copy");
  scratch.remove();
  return ok ? Promise.resolve() : Promise.reject(new Error("copy blocked"));
}

async function copyTranscript() {
  if (!jobId) return;
  const response = await fetch(`/api/jobs/${jobId}/transcript`);
  if (!response.ok) { setStatus("No transcript yet"); return; }
  const data = await response.json();
  try {
    await copyText(data.text);
    setStatus(`Copied · ${data.words.toLocaleString()} words`);
  } catch {
    setStatus("Copy blocked — open the file instead");
  }
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    el.urls.value += (el.urls.value && !el.urls.value.endsWith("\n") ? "\n" : "") + text.trim();
  } catch {
    setStatus("Clipboard blocked — paste with Ctrl+V");
  }
  el.urls.focus();
}

el.start.addEventListener("click", start);
el.cancel.addEventListener("click", cancel);
el.copy.addEventListener("click", copyTranscript);
el.paste.addEventListener("click", pasteFromClipboard);
el.refresh.addEventListener("click", refreshFiles);
el.urls.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) start();
});

syncMode();
refreshFiles();
