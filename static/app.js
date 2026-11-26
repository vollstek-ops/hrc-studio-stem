/* ==========================================================================
   STEM — front-end controller (vanilla JS, no build step)
   ========================================================================== */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const els = {
    modes: $("#modes"),
    dropzone: $("#dropzone"),
    fileInput: $("#file-input"),
    uploadNote: $("#upload-note"),
    enginePill: $("#engine-pill"),
    ffmpegPill: $("#ffmpeg-pill"),
    progressPanel: $("#progress-panel"),
    progressTitle: $("#progress-title"),
    progressFile: $("#progress-file"),
    stageBadge: $("#stage-badge"),
    bar: $("#bar"),
    barFill: $("#bar-fill"),
    pct: $("#progress-pct"),
    elapsed: $("#progress-elapsed"),
    cancelBtn: $("#cancel-btn"),
    errorBox: $("#error-box"),
    resultsPanel: $("#results-panel"),
    resultsMeta: $("#results-meta"),
    stems: $("#stems"),
    downloadAll: $("#download-all"),
    newTrack: $("#new-track"),
    historyPanel: $("#history-panel"),
    history: $("#history"),
    footEngine: $("#foot-engine"),
  };

  const STEM_COLORS = {
    vocals: "#6ee7ff",
    instrumental: "#a78bfa",
    drums: "#f472b6",
    bass: "#fbbf24",
    guitar: "#34d399",
    piano: "#f87171",
    other: "#94a3b8",
  };

  const state = {
    modes: [],
    defaultMode: "2stem",
    mode: "2stem",
    job: null,
    pollTimer: null,
    startedAt: 0,
    elapsedTimer: null,
    uploadAbort: null,
    busy: false,
  };

  /* -------------------------------------------------------------- helpers */

  const fmtTime = (seconds) => {
    const total = Math.max(0, Math.round(seconds || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  };

  const fmtBytes = (bytes) => {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / 1024 ** i;
    return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
  };

  const showError = (message) => {
    els.errorBox.textContent = message;
    els.errorBox.hidden = !message;
  };

  async function api(path, options = {}) {
    const res = await fetch(path, options);
    const isJson = (res.headers.get("content-type") || "").includes("application/json");
    const body = isJson ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = body && body.detail ? body.detail : `Request failed (${res.status})`;
      throw new Error(detail);
    }
    return body;
  }

  /* ----------------------------------------------------------------- boot */

  async function boot() {
    try {
      const health = await api("/api/health");
      els.ffmpegPill.textContent = health.ffmpeg ? "ffmpeg ready" : "ffmpeg missing";
      els.ffmpegPill.className = `pill ${health.ffmpeg ? "ok" : "bad"}`;
      els.uploadNote.textContent =
        `Lossless 24-bit PCM WAV · ${health.max_upload_mb} MB per file · ` +
        `accepted: ${health.accepted_extensions.join(" ")}`;
    } catch {
      els.ffmpegPill.textContent = "offline";
      els.ffmpegPill.className = "pill bad";
    }

    try {
      const payload = await api("/api/modes");
      state.modes = payload.modes;
      state.defaultMode = payload.default;
      state.mode = payload.default;
      renderModes();
      updateEnginePill();
    } catch (err) {
      els.modes.innerHTML = `<p class="muted">Could not load modes: ${err.message}</p>`;
    }

    loadHistory();
    await restoreFromUrl();
  }

  /* A job id in the URL (?job=<id>) reopens those results directly. */
  async function restoreFromUrl() {
    const jobId = new URLSearchParams(window.location.search).get("job");
    if (!jobId) return;
    try {
      const job = await api(`/api/jobs/${jobId}`);
      state.job = job;
      if (job.status === "done") {
        state.mode = job.mode;
        syncModeSelection();
        updateEnginePill();
        finishSuccess(job);
      } else if (job.status === "running" || job.status === "queued") {
        state.busy = true;
        els.dropzone.classList.add("busy");
        els.progressPanel.hidden = false;
        els.progressTitle.textContent = "Separating…";
        startElapsedTimer();
        poll();
      }
    } catch {
      // Stale or expired link: fall back to the empty state silently.
    }
  }

  function updateEnginePill() {
    const mode = state.modes.find((m) => m.id === state.mode);
    if (mode) {
      els.enginePill.textContent = `${mode.model_label} · 24-bit`;
      els.footEngine.textContent = `${mode.model}`;
    }
  }

  /* ---------------------------------------------------------------- modes */

  function renderModes() {
    els.modes.innerHTML = "";
    state.modes.forEach((mode) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "mode-card";
      card.setAttribute("role", "radio");
      card.dataset.mode = mode.id;
      card.innerHTML = `
        <div class="mode-top">
          <span class="mode-name">${mode.name}</span>
          <span class="mode-check" aria-hidden="true"></span>
        </div>
        <span class="mode-tagline">${mode.tagline}</span>
        <p class="mode-desc">${mode.description}</p>
        <div class="mode-chips">${mode.stems.map((s) => `<span class="chip">${s}</span>`).join("")}</div>
        <span class="mode-model">${mode.model}</span>
      `;
      card.addEventListener("click", () => selectMode(mode.id));
      card.addEventListener("keydown", (event) => {
        if (event.key === " " || event.key === "Enter") {
          event.preventDefault();
          selectMode(mode.id);
        }
      });
      els.modes.appendChild(card);
    });
    syncModeSelection();
  }

  function selectMode(modeId) {
    state.mode = modeId;
    syncModeSelection();
    updateEnginePill();
  }

  function syncModeSelection() {
    els.modes.querySelectorAll(".mode-card").forEach((card) => {
      card.setAttribute("aria-checked", card.dataset.mode === state.mode ? "true" : "false");
      card.tabIndex = card.dataset.mode === state.mode ? 0 : -1;
    });
  }

  /* ------------------------------------------------------------- dropzone */

  function initDropzone() {
    const openPicker = () => {
      if (!state.busy) els.fileInput.click();
    };

    els.dropzone.addEventListener("click", openPicker);
    els.dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openPicker();
      }
    });

    els.fileInput.addEventListener("change", () => {
      const file = els.fileInput.files && els.fileInput.files[0];
      if (file) handleFile(file);
      els.fileInput.value = "";
    });

    ["dragenter", "dragover"].forEach((type) =>
      els.dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        els.dropzone.classList.add("dragging");
      })
    );

    ["dragleave", "drop"].forEach((type) =>
      els.dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        if (type === "dragleave" && els.dropzone.contains(event.relatedTarget)) return;
        els.dropzone.classList.remove("dragging");
      })
    );

    els.dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) handleFile(file);
    });

    // Dragging anywhere else should not navigate away from the app.
    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => event.preventDefault());
  }

  async function handleFile(file) {
    if (state.busy) return;
    if (!file.size) {
      showError("That file is empty.");
      return;
    }

    state.busy = true;
    els.dropzone.classList.add("busy");
    els.resultsPanel.hidden = true;
    showError("");
    els.progressPanel.hidden = false;
    els.bar.classList.add("running");
    els.barFill.style.width = "0%";
    els.pct.textContent = "0%";
    els.progressTitle.textContent = "Uploading…";
    els.progressFile.textContent = `${file.name} · ${fmtBytes(file.size)}`;
    els.stageBadge.textContent = "Upload";
    els.cancelBtn.disabled = false;
    startElapsedTimer();

    try {
      const created = await uploadFile(file, (fraction) => {
        const pct = Math.round(fraction * 100);
        els.barFill.style.width = `${Math.min(100, pct)}%`;
        els.pct.textContent = `${pct}%`;
      });
      state.job = created;
      els.progressTitle.textContent = "Separating…";
      poll();
    } catch (err) {
      state.busy = false;
      els.dropzone.classList.remove("busy");
      els.bar.classList.remove("running");
      els.progressPanel.hidden = false;
      els.progressTitle.textContent = "Upload failed";
      els.stageBadge.textContent = "Error";
      showError(err.message);
      stopElapsedTimer();
    }
  }

  function uploadFile(file, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", file, file.name);
      form.append("mode", state.mode);

      const xhr = new XMLHttpRequest();
      state.uploadAbort = xhr;
      xhr.open("POST", "/api/jobs");

      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      });

      xhr.addEventListener("load", () => {
        state.uploadAbort = null;
        let body = {};
        try {
          body = JSON.parse(xhr.responseText || "{}");
        } catch {
          body = {};
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          onProgress(1);
          resolve(body);
        } else {
          reject(new Error(body.detail || `Upload failed (${xhr.status})`));
        }
      });

      xhr.addEventListener("error", () => {
        state.uploadAbort = null;
        reject(new Error("Network error while uploading."));
      });

      xhr.addEventListener("abort", () => {
        state.uploadAbort = null;
        reject(new Error("Upload cancelled."));
      });

      xhr.send(form);
    });
  }

  /* -------------------------------------------------------------- polling */

  function poll() {
    stopPolling();
    state.pollTimer = setInterval(tick, 800);
    tick();
  }

  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }

  async function tick() {
    if (!state.job) return;
    try {
      const job = await api(`/api/jobs/${state.job.id}`);
      state.job = job;
      renderProgress(job);

      if (job.status === "done") {
        stopPolling();
        stopElapsedTimer();
        finishSuccess(job);
      } else if (job.status === "error") {
        stopPolling();
        stopElapsedTimer();
        finishError(job);
      } else if (job.status === "cancelled") {
        stopPolling();
        stopElapsedTimer();
        finishCancelled(job);
      }
    } catch (err) {
      stopPolling();
      stopElapsedTimer();
      finishError({ error: err.message });
    }
  }

  function renderProgress(job) {
    const pct = Math.round((job.progress || 0) * 100);
    els.barFill.style.width = `${pct}%`;
    els.pct.textContent = `${pct}%`;
    els.stageBadge.textContent = job.stage || job.status;
    els.bar.setAttribute("aria-valuenow", String(pct));
    els.progressFile.textContent =
      `${job.filename} · ${fmtBytes(job.size)}` +
      (job.duration ? ` · ${fmtTime(job.duration)} of audio` : "") +
      ` · ${job.mode_name}`;
    if (job.status === "running") els.bar.classList.add("running");
  }

  function startElapsedTimer() {
    stopElapsedTimer();
    state.startedAt = Date.now();
    els.elapsed.textContent = "0:00 elapsed";
    state.elapsedTimer = setInterval(() => {
      const seconds = (Date.now() - state.startedAt) / 1000;
      els.elapsed.textContent = `${fmtTime(seconds)} elapsed`;
    }, 1000);
  }

  function stopElapsedTimer() {
    if (state.elapsedTimer) clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
  }

  /* -------------------------------------------------------------- results */

  function finishSuccess(job) {
    state.busy = false;
    els.dropzone.classList.remove("busy");
    els.bar.classList.remove("running");
    els.progressPanel.hidden = true;
    els.errorBox.hidden = true;
    els.resultsPanel.hidden = false;

    const count = job.stems.length;
    const depth = job.stems[0] ? job.stems[0].bit_depth : 24;
    const rate = job.stems[0] ? (job.stems[0].sample_rate || 44100) / 1000 : 44.1;
    els.resultsMeta.textContent =
      `${job.filename} · ${job.mode_name} · ${count} stem${count === 1 ? "" : "s"} · ` +
      `${depth}-bit / ${rate.toFixed(1)} kHz WAV · ${fmtTime(job.duration)} · ` +
      `separated in ${fmtTime(job.elapsed)}`;

    renderStems(job);
    loadHistory();

    // Keep the address bar shareable/reloadable for this result.
    const url = new URL(window.location.href);
    url.searchParams.set("job", job.id);
    window.history.replaceState({}, "", url);
  }

  function finishError(job) {
    state.busy = false;
    els.dropzone.classList.remove("busy");
    els.bar.classList.remove("running");
    els.progressTitle.textContent = "Separation failed";
    els.stageBadge.textContent = "Error";
    showError(job.error || "Unknown error.");
    loadHistory();
  }

  function finishCancelled() {
    state.busy = false;
    els.dropzone.classList.remove("busy");
    els.bar.classList.remove("running");
    els.progressPanel.hidden = true;
    showError("");
    loadHistory();
  }

  function renderStems(job) {
    els.stems.innerHTML = "";
    job.stems.forEach((stem, index) => {
      const color = STEM_COLORS[stem.slug] || STEM_COLORS.other;
      const card = document.createElement("article");
      card.className = "stem";
      card.style.animationDelay = `${index * 45}ms`;
      card.style.setProperty("--dot", color);

      const head = document.createElement("div");
      head.className = "stem-head";
      head.innerHTML = `
        <span class="stem-name"><span class="stem-dot"></span>${stem.name}</span>
        <span class="stem-meta">${stem.bit_depth}-bit · ${(stem.sample_rate / 1000).toFixed(1)}kHz · ${fmtBytes(stem.bytes)}</span>
      `;

      const canvas = document.createElement("canvas");
      canvas.className = "wave";
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", `${stem.name} waveform`);
      canvas.title = "Click to jump in the preview";

      const audio = document.createElement("audio");
      audio.controls = true;
      audio.preload = "metadata";
      audio.src = stem.stream_url;

      const actions = document.createElement("div");
      actions.className = "stem-actions";
      const download = document.createElement("a");
      download.className = "btn";
      download.href = stem.download_url;
      download.textContent = "Download WAV";
      download.setAttribute("download", "");
      actions.appendChild(download);

      card.append(head, canvas, audio, actions);
      els.stems.appendChild(card);

      requestAnimationFrame(() => drawWave(canvas, stem.peaks, color));
      canvas.addEventListener("click", (event) => {
        const rect = canvas.getBoundingClientRect();
        const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
          audio.currentTime = fraction * audio.duration;
          audio.play().catch(() => {});
        }
      });

      const onTime = () => {
        const total = audio.duration || stem.duration || 0;
        const played = total ? audio.currentTime / total : 0;
        drawWave(canvas, stem.peaks, color, played);
      };
      audio.addEventListener("timeupdate", onTime);
      audio.addEventListener("loadedmetadata", onTime);
      audio.addEventListener("ended", () => drawWave(canvas, stem.peaks, color, 0));
    });
  }

  function drawWave(canvas, peaks, color, playedFraction = 0) {
    if (!canvas || !peaks || !peaks.length) return;
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = canvas.clientWidth || canvas.parentElement.clientWidth || 280;
    const cssHeight = 46;
    canvas.width = Math.max(1, Math.round(cssWidth * dpr));
    canvas.height = Math.round(cssHeight * dpr);

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    const mid = cssHeight / 2;
    const barWidth = cssWidth / peaks.length;
    const playX = playedFraction * cssWidth;

    for (let i = 0; i < peaks.length; i++) {
      const x = i * barWidth;
      const amplitude = Math.max(0.02, peaks[i]);
      const height = amplitude * (cssHeight - 8);
      ctx.fillStyle = x <= playX ? color : "rgba(255,255,255,0.20)";
      ctx.fillRect(x, mid - height / 2, Math.max(1, barWidth - 0.6), height);
    }

    ctx.fillStyle = "rgba(110,231,255,0.55)";
    ctx.fillRect(mid ? playX : 0, 0, 1.4, cssHeight);
  }

  /* -------------------------------------------------------------- history */

  async function loadHistory() {
    try {
      const payload = await api("/api/jobs?limit=8");
      const jobs = payload.jobs.filter((job) => job.status !== "queued").slice(0, 6);
      if (!jobs.length) {
        els.historyPanel.hidden = true;
        return;
      }
      els.historyPanel.hidden = false;
      els.history.innerHTML = "";
      jobs.forEach((job) => {
        const item = document.createElement("li");
        const stamp = new Date(job.created_at * 1000);
        item.innerHTML = `
          <span class="h-status ${job.status}">${job.status}</span>
          <span class="h-name">${job.filename}</span>
          <span class="h-tag">${job.mode_name} · ${stamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
        `;
        if (job.status === "done" && job.stems.length) {
          const open = document.createElement("button");
          open.type = "button";
          open.className = "btn ghost small";
          open.textContent = "Open";
          open.addEventListener("click", () => {
            state.job = job;
            state.busy = false;
            els.progressPanel.hidden = true;
            finishSuccess(job);
            window.scrollTo({ top: els.resultsPanel.offsetTop - 90, behavior: "smooth" });
          });
          item.appendChild(open);
        }
        els.history.appendChild(item);
      });
    } catch {
      els.historyPanel.hidden = true;
    }
  }

  /* --------------------------------------------------------------- actions */

  async function cancel() {
    if (state.uploadAbort) {
      state.uploadAbort.abort();
      return;
    }
    if (!state.job) return;
    els.cancelBtn.disabled = true;
    els.stageBadge.textContent = "Cancelling";
    try {
      const payload = await api(`/api/jobs/${state.job.id}/cancel`, { method: "POST" });
      if (payload.job) state.job = payload.job;
    } catch (err) {
      showError(err.message);
    }
  }

  function downloadAll() {
    if (!state.job || !state.job.stems.length) return;
    state.job.stems.forEach((stem, index) => {
      setTimeout(() => {
        const link = document.createElement("a");
        link.href = stem.download_url;
        link.download = "";
        document.body.appendChild(link);
        link.click();
        link.remove();
      }, index * 450);
    });
  }

  function resetForNewTrack() {
    stopPolling();
    stopElapsedTimer();
    state.job = null;
    state.busy = false;
    els.resultsPanel.hidden = true;
    els.progressPanel.hidden = true;
    els.stems.innerHTML = "";
    showError("");
    els.dropzone.classList.remove("busy");
    const url = new URL(window.location.href);
    url.searchParams.delete("job");
    window.history.replaceState({}, "", url);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  /* ------------------------------------------------------------------ init */

  initDropzone();
  els.cancelBtn.addEventListener("click", cancel);
  els.downloadAll.addEventListener("click", downloadAll);
  els.newTrack.addEventListener("click", resetForNewTrack);
  window.addEventListener("resize", () => {
    if (!state.job || !state.job.stems) return;
    els.stems.querySelectorAll("canvas.wave").forEach((canvas, index) => {
      const stem = state.job.stems[index];
      if (stem) drawWave(canvas, stem.peaks, STEM_COLORS[stem.slug] || STEM_COLORS.other);
    });
  });

  boot();
})();
