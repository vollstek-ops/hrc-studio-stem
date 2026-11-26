/* ==========================================================================
   HRC Studio Stem — front-end controller (vanilla JS, no build step)

   Two halves:
     1. upload + pipeline progress (polling /api/jobs)
     2. the practice studio: one shared clock driving every stem through a
        WebAudio graph (gain per track, analyser per track, master bus)
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
    eta: $("#progress-eta"),
    elapsed: $("#progress-elapsed"),
    pipeline: $("#pipeline"),
    cancelBtn: $("#cancel-btn"),
    errorBox: $("#error-box"),
    studio: $("#studio"),
    resultsMeta: $("#results-meta"),
    stems: $("#stems"),
    downloadAll: $("#download-all"),
    bounceBtn: $("#bounce-btn"),
    bounceLabel: $("#bounce-label"),
    bounceNote: $("#bounce-note"),
    newTrack: $("#new-track"),
    playBtn: $("#play-btn"),
    stopBtn: $("#stop-btn"),
    timeCur: $("#time-cur"),
    timeDur: $("#time-dur"),
    speedButtons: Array.from(document.querySelectorAll(".seg-btn")),
    loopToggle: $("#loop-toggle"),
    setA: $("#set-a"),
    setB: $("#set-b"),
    clearLoop: $("#clear-loop"),
    loopReadout: $("#loop-readout"),
    masterVol: $("#master-vol"),
    masterVolVal: $("#master-vol-val"),
    masterMeter: $("#master-meter-fill"),
    masterWave: $("#master-wave"),
    timelineNote: $("#timeline-note"),
    timelineRate: $("#timeline-rate"),
    timelineTotal: $("#timeline-total"),
    timelineLoopTag: $("#timeline-loop-tag"),
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
  const FALLBACK_COLORS = ["#6ee7ff", "#a78bfa", "#f472b6", "#fbbf24", "#34d399", "#f87171"];

  const state = {
    modes: [],
    defaultMode: "2stem",
    mode: "2stem",
    job: null,
    pollTimer: null,
    startedAt: 0,
    elapsedBase: 0,
    elapsedStamp: 0,
    elapsedTimer: null,
    uploadAbort: null,
    busy: false,
  };

  /* The mixer owns every audio node. One clock (mixer.leader) is the truth for
     the playhead; everyone else is nudged back onto it while playing. */
  const mixer = {
    ctx: null,
    masterGain: null,
    masterAnalyser: null,
    tracks: [],
    leader: 0,
    focus: 0,
    playing: false,
    rate: 1,
    masterVolume: 1,
    loop: { a: null, b: null, on: false },
    raf: 0,
    lastPaint: 0,
    frames: 0,
    masterPeaks: [],
    masterLayers: { dim: makeLayer("dim"), bright: makeLayer("bright") },
  };

  /* -------------------------------------------------------------- helpers */

  const esc = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  const fmtTime = (seconds) => {
    const total = Math.max(0, Math.round(seconds || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };

  const fmtClock = (seconds) => {
    const value = Math.max(0, seconds || 0);
    const mins = Math.floor(value / 60);
    const secs = Math.floor(value % 60);
    const tenths = Math.floor((value * 10) % 10);
    return `${mins}:${String(secs).padStart(2, "0")}.${tenths}`;
  };

  const fmtDuration = (seconds) => {
    if (seconds == null) return "";
    if (seconds < 60) return `${Math.max(0.1, seconds).toFixed(seconds < 10 ? 1 : 0)}s`;
    return fmtTime(seconds);
  };

  const fmtBytes = (bytes) => {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / 1024 ** i;
    return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
  };

  const fmtDb = (percent) => {
    if (percent <= 0) return "-∞ dB";
    const db = 20 * Math.log10(percent / 100);
    return `${db > 0 ? "+" : ""}${db.toFixed(1)} dB`;
  };

  const withAlpha = (color, alpha) => {
    const hex = String(color).replace("#", "");
    if (hex.length !== 6) return `rgba(148, 163, 184, ${alpha})`;
    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
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
      els.modes.innerHTML = `<p class="muted">Could not load modes: ${esc(err.message)}</p>`;
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
          <span class="mode-name">${esc(mode.name)}</span>
          <span class="mode-check" aria-hidden="true"></span>
        </div>
        <span class="mode-tagline">${esc(mode.tagline)}</span>
        <p class="mode-desc">${esc(mode.description)}</p>
        <div class="mode-chips">${mode.stems.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>
        <span class="mode-model">${esc(mode.model)}</span>
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

    disposeStudio();
    state.busy = true;
    els.dropzone.classList.add("busy");
    els.studio.hidden = true;
    showError("");
    els.progressPanel.hidden = false;
    els.bar.classList.add("running");
    els.barFill.style.width = "0%";
    els.pct.textContent = "0%";
    els.eta.textContent = "uploading…";
    els.pipeline.hidden = true;
    els.pipeline.innerHTML = "";
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
        els.bar.setAttribute("aria-valuenow", String(pct));
      });
      state.job = created;
      els.progressTitle.textContent = "Separating…";
      els.eta.textContent = "estimating…";
      poll();
    } catch (err) {
      state.busy = false;
      els.dropzone.classList.remove("busy");
      els.bar.classList.remove("running");
      els.progressPanel.hidden = false;
      els.progressTitle.textContent = "Upload failed";
      els.stageBadge.textContent = "Error";
      els.eta.textContent = "—";
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

    if (job.status === "running" || job.status === "queued") {
      // Trust the server clock between polls so reopening a running job shows
      // the real elapsed time instead of restarting from zero.
      syncElapsed(job.elapsed || 0);
    }

    if (job.status === "running") {
      els.bar.classList.add("running");
      els.eta.textContent =
        job.eta_seconds != null ? `≈ ${fmtTime(job.eta_seconds)} left` : "estimating…";
    } else if (job.status === "queued") {
      els.eta.textContent = "waiting for the worker";
    }

    renderPipeline(job.pipeline || []);
  }

  function renderPipeline(steps) {
    if (!steps.length) {
      els.pipeline.hidden = true;
      els.pipeline.innerHTML = "";
      return;
    }
    els.pipeline.hidden = false;
    els.pipeline.innerHTML = steps
      .map((step) => {
        const time =
          step.seconds != null && step.seconds >= 0.15 ? `<span class="step-time">${fmtDuration(step.seconds)}</span>` : "";
        return `
          <li class="step" data-state="${esc(step.state)}">
            <span class="step-mark" aria-hidden="true"></span>
            <span class="step-label">${esc(step.label)}</span>
            ${time}
          </li>`;
      })
      .join("");
  }

  function startElapsedTimer(initial = 0) {
    stopElapsedTimer();
    syncElapsed(initial);
    state.elapsedTimer = setInterval(() => {
      const seconds = state.elapsedBase + (Date.now() - state.elapsedStamp) / 1000;
      els.elapsed.textContent = `${fmtTime(seconds)} elapsed`;
    }, 500);
  }

  function syncElapsed(seconds) {
    state.elapsedBase = Math.max(0, seconds || 0);
    state.elapsedStamp = Date.now();
    els.elapsed.textContent = `${fmtTime(state.elapsedBase)} elapsed`;
  }

  function stopElapsedTimer() {
    if (state.elapsedTimer) clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
  }

  /* ========================================================== practice studio */

  const isStudioOpen = () => Boolean(state.job && state.job.status === "done" && !els.studio.hidden);

  function finishSuccess(job) {
    state.busy = false;
    els.dropzone.classList.remove("busy");
    els.bar.classList.remove("running");
    els.progressPanel.hidden = true;
    els.errorBox.hidden = true;
    els.studio.hidden = false;

    const count = job.stems.length;
    const first = job.stems[0] || {};
    const depth = first.bit_depth || 24;
    const rate = (first.sample_rate || 44100) / 1000;
    mixer.format = `${depth}-bit · ${rate.toFixed(1)} kHz`;
    els.resultsMeta.textContent =
      `${job.filename} · ${job.mode_name} · ${count} stem${count === 1 ? "" : "s"} · ` +
      `${depth}-bit / ${rate.toFixed(1)} kHz WAV · ${fmtTime(job.duration)} · ` +
      `separated in ${fmtTime(job.elapsed)}`;

    renderStudio(job);
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
    els.eta.textContent = "—";
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

  /* ------------------------------------------------------------ audio graph */

  function ensureContext() {
    if (mixer.ctx) return mixer.ctx;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try {
      mixer.ctx = new Ctx();
      mixer.masterGain = mixer.ctx.createGain();
      mixer.masterGain.gain.value = mixer.masterVolume;
      mixer.masterAnalyser = mixer.ctx.createAnalyser();
      mixer.masterAnalyser.fftSize = 256;
      mixer.masterAnalyser.smoothingTimeConstant = 0.15;
      mixer.masterBuf = new Uint8Array(mixer.masterAnalyser.fftSize);
      mixer.masterGain.connect(mixer.masterAnalyser);
      mixer.masterAnalyser.connect(mixer.ctx.destination);
    } catch {
      mixer.ctx = null;
      mixer.masterGain = null;
      mixer.masterAnalyser = null;
    }
    return mixer.ctx;
  }

  function buildTrack(stem, index) {
    const color = STEM_COLORS[stem.slug] || FALLBACK_COLORS[index % FALLBACK_COLORS.length];
    const row = document.createElement("article");
    row.className = "track";
    row.setAttribute("role", "listitem");
    row.tabIndex = 0;
    row.dataset.slug = stem.slug;
    row.style.setProperty("--dot", color);
    row.innerHTML = `
      <div class="track-id">
        <span class="track-num">${index + 1}</span>
        <div class="track-text">
          <span class="track-name"><span class="track-dot" aria-hidden="true"></span>${esc(stem.name)}</span>
          <span class="track-meta">${stem.bit_depth}-bit · ${(stem.sample_rate / 1000).toFixed(1)}k · ${fmtBytes(stem.bytes)}</span>
        </div>
      </div>
      <canvas class="wave" role="img" aria-label="${esc(stem.name)} waveform: click or drag to seek"></canvas>
      <div class="track-mix">
        <button class="ms-btn" type="button" data-act="mute" title="Mute (M)" aria-pressed="false">M</button>
        <button class="ms-btn" type="button" data-act="solo" title="Solo (S)" aria-pressed="false">S</button>
        <input type="range" class="fader" min="0" max="125" value="100" step="1"
               aria-label="${esc(stem.name)} volume" />
        <span class="db">0.0 dB</span>
        <div class="meter" role="img" aria-label="${esc(stem.name)} output level">
          <span class="meter-fill"></span>
        </div>
        <a class="dl-btn" href="${stem.download_url}" download
           title="Download ${esc(stem.name)} WAV" aria-label="Download ${esc(stem.name)} WAV">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10m0 0l4-4m-4 4l-4-4M5 18h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
        </a>
      </div>
      <audio preload="auto" src="${stem.stream_url}" class="visually-hidden"></audio>
    `;

    els.stems.appendChild(row);

    const audio = row.querySelector("audio");
    const track = {
      index,
      stem,
      color,
      audio,
      source: null,
      gain: null,
      analyser: null,
      meterBuf: null,
      level: 0,
      muted: false,
      solo: false,
      silent: false,
      volume: 1,
      peaks: Array.isArray(stem.peaks) ? stem.peaks : [],
      layers: { dim: makeLayer("dim"), bright: makeLayer("bright") },
      meterScale: 0,
      els: {
        row,
        canvas: row.querySelector("canvas"),
        muteBtn: row.querySelector('[data-act="mute"]'),
        soloBtn: row.querySelector('[data-act="solo"]'),
        fader: row.querySelector(".fader"),
        db: row.querySelector(".db"),
        meterFill: row.querySelector(".meter-fill"),
      },
    };

    // WebAudio: element -> track gain -> track analyser -> master bus.
    const ctx = ensureContext();
    if (ctx) {
      try {
        track.source = ctx.createMediaElementSource(audio);
        track.gain = ctx.createGain();
        track.gain.gain.value = 1;
        track.analyser = ctx.createAnalyser();
        track.analyser.fftSize = 256;
        track.analyser.smoothingTimeConstant = 0.15;
        track.meterBuf = new Uint8Array(track.analyser.fftSize);
        track.source.connect(track.gain);
        track.gain.connect(track.analyser);
        track.analyser.connect(mixer.masterGain);
      } catch {
        track.source = null;
        track.gain = null;
        track.analyser = null;
      }
    }

    /* -- per-track interaction -- */
    row.addEventListener("pointerdown", () => selectTrack(index));
    row.addEventListener("focusin", () => selectTrack(index));

    track.els.muteBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      setMute(index);
      selectTrack(index);
    });
    track.els.soloBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      setSolo(index);
      selectTrack(index);
    });
    track.els.fader.addEventListener("input", () => {
      setVolume(index, Number(track.els.fader.value));
      selectTrack(index);
    });

    attachScrubber(track.els.canvas, () => index);

    audio.addEventListener("loadedmetadata", () => {
      updateReadout(currentTime(), duration());
      paintFrame(currentTime(), true);
    });

    mixer.tracks.push(track);
    return row;
  }

  /* Click-or-drag scrubbing, shared by every waveform canvas. */
  function attachScrubber(canvas, getIndex) {
    const seekFromEvent = (event) => {
      const dur = duration();
      if (!dur) return;
      const rect = canvas.getBoundingClientRect();
      if (!rect.width) return;
      const fraction = clamp((event.clientX - rect.left) / rect.width, 0, 1);
      seekAll(fraction * dur, { paint: true });
    };

    canvas.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      try {
        canvas.setPointerCapture(event.pointerId);
      } catch {}
      canvas.dataset.dragging = "1";
      if (getIndex) selectTrack(getIndex());
      seekFromEvent(event);
    });
    canvas.addEventListener("pointermove", (event) => {
      if (canvas.dataset.dragging !== "1") return;
      seekFromEvent(event);
    });
    const end = (event) => {
      canvas.dataset.dragging = "0";
      try {
        canvas.releasePointerCapture(event.pointerId);
      } catch {}
    };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    canvas.addEventListener("pointerleave", () => {
      canvas.dataset.dragging = "0";
    });
  }

  function renderStudio(job) {
    disposeStudio();
    els.downloadAll.href = job.download_all_url || `/api/jobs/${job.id}/download-all`;
    els.downloadAll.setAttribute("download", "");
    job.stems.forEach((stem, index) => buildTrack(stem, index));
    mixer.masterPeaks = compositePeaks(job.stems, 480);
    mixer.leader = 0;
    mixer.masterLevel = 0;

    showBounceNote("");
    setSpeed(1, { silent: true });
    updateLoopUI();
    updateTransport();
    updateReadout(0, duration());
    selectTrack(0);
    paintFrame(0, true);
    renderRateNote();
  }

  function disposeStudio() {
    stopFrameLoop();
    mixer.playing = false;
    mixer.tracks.forEach((track) => {
      try {
        track.audio.pause();
      } catch {}
      [track.source, track.gain, track.analyser].forEach((node) => {
        try {
          node && node.disconnect();
        } catch {}
      });
      try {
        track.audio.removeAttribute("src");
        track.audio.load();
      } catch {}
    });
    mixer.tracks = [];
    mixer.masterPeaks = [];
    mixer.leader = 0;
    mixer.focus = 0;
    mixer.loop = { a: null, b: null, on: false };
    mixer.rate = 1;
    if (els.stems) els.stems.innerHTML = "";
    updateTransport();
  }

  /* --------------------------------------------------------------- drawing */

  /* Every lane is drawn in two cached layers (dim = unplayed, bright = played).
     A frame is then two blits plus the playhead, instead of ~1500 fillRects,
     which is what keeps six lanes at 60 fps on a laptop. */
  function makeLayer(name) {
    const canvas = document.createElement("canvas");
    return { name, canvas, ctx: canvas.getContext("2d"), key: "" };
  }

  function prepareLayer(canvas, layer, dpr, key) {
    const width = canvas.width;
    const height = canvas.height;
    if (layer.canvas.width !== width || layer.canvas.height !== height) {
      layer.canvas.width = width;
      layer.canvas.height = height;
      layer.key = "";
    }
    if (layer.key === key) return false;
    layer.key = key;
    layer.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    layer.ctx.clearRect(0, 0, width / dpr, height / dpr);
    return true;
  }

  function blitLayers(canvas, dim, bright, playX, dpr) {
    const ctx = canvas.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (dim.key) ctx.drawImage(dim.canvas, 0, 0);
    const px = Math.round(clamp(playX, 0, canvas.width));
    if (px > 0 && bright.key) {
      ctx.drawImage(bright.canvas, 0, 0, px, canvas.height, 0, 0, px, canvas.height);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function sizeCanvas(canvas, cssHeight) {
    const dpr = window.devicePixelRatio || 1;
    const cssWidth = Math.max(1, canvas.clientWidth || canvas.parentElement.clientWidth || 320);
    const width = Math.round(cssWidth * dpr);
    const height = Math.round(cssHeight * dpr);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    return { ctx: canvas.getContext("2d"), w: cssWidth, h: cssHeight, dpr };
  }

  function compositePeaks(stems, buckets) {
    const out = new Array(buckets).fill(0);
    stems.forEach((stem) => {
      const peaks = stem.peaks || [];
      if (!peaks.length) return;
      const scale = peaks.length / buckets;
      for (let i = 0; i < buckets; i++) {
        const value = peaks[Math.floor(i * scale)] || 0;
        if (value > out[i]) out[i] = value;
      }
    });
    let max = 0;
    for (let i = 0; i < out.length; i++) if (out[i] > max) max = out[i];
    if (!max) return out;
    return out.map((value) => value / max);
  }

  function drawLoopBand(ctx, w, h, dur) {
    const { a, b, on } = mixer.loop;
    if (!on || !dur) return;
    const start = a == null ? 0 : a;
    const end = b == null ? dur : b;
    if (end <= start) return;
    const x1 = clamp(start / dur, 0, 1) * w;
    const x2 = clamp(end / dur, 0, 1) * w;
    ctx.fillStyle = "rgba(110, 231, 255, 0.07)";
    ctx.fillRect(x1, 0, x2 - x1, h);
    ctx.strokeStyle = "rgba(110, 231, 255, 0.5)";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(Math.round(x1) + 0.5, 0);
    ctx.lineTo(Math.round(x1) + 0.5, h);
    ctx.moveTo(Math.round(x2) - 0.5, 0);
    ctx.lineTo(Math.round(x2) - 0.5, h);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function paintBars(ctx, peaks, w, h, color, inset) {
    if (!peaks.length || w <= 0) return;
    const mid = h / 2;
    const bars = Math.min(peaks.length, Math.max(80, Math.round(w / 3)));
    const barW = w / bars;
    const scale = peaks.length / bars;
    ctx.fillStyle = color;
    for (let i = 0; i < bars; i++) {
      const height = Math.max(0.02, peaks[Math.floor(i * scale)] || 0) * (h - inset);
      ctx.fillRect(i * barW, mid - height / 2, Math.max(1, barW - 0.7), height);
    }
  }

  function drawTrackWave(track, time, dur) {
    const canvas = track.els.canvas;
    if (!canvas || !canvas.clientWidth) return;
    const { ctx, w, h, dpr } = sizeCanvas(canvas, 62);
    const playX = dur ? clamp(time / dur, 0, 1) * w : 0;
    const layers = track.layers;
    const key = `${track.silent ? "s" : "a"}|${track.peaks.length}`;

    if (prepareLayer(canvas, layers.dim, dpr, key)) {
      layers.dim.ctx.fillStyle = "rgba(255,255,255,0.06)";
      layers.dim.ctx.fillRect(0, h / 2 - 0.5, w, 1);
      paintBars(layers.dim.ctx, track.peaks, w, h, withAlpha(track.color, track.silent ? 0.1 : 0.26), 12);
    }
    if (prepareLayer(canvas, layers.bright, dpr, key)) {
      paintBars(layers.bright.ctx, track.peaks, w, h, withAlpha(track.color, 0.95), 12);
    }

    blitLayers(canvas, layers.dim, layers.bright, playX * dpr, dpr);
    drawLoopBand(ctx, w, h, dur);
    ctx.fillStyle = "rgba(240, 250, 255, 0.9)";
    ctx.fillRect(playX - 0.75, 1, 1.5, h - 2);
  }

  function drawMasterWave(time, dur) {
    const canvas = els.masterWave;
    if (!canvas || !canvas.clientWidth) return;
    const { ctx, w, h, dpr } = sizeCanvas(canvas, 92);
    const playX = dur ? clamp(time / dur, 0, 1) * w : 0;
    const peaks = mixer.masterPeaks;
    const layers = mixer.masterLayers;
    const key = `${dur.toFixed(3)}|${peaks.length}`;

    if (prepareLayer(canvas, layers.dim, dpr, key)) {
      const dimCtx = layers.dim.ctx;
      if (dur) {
        const step = niceStep(dur);
        dimCtx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
        dimCtx.textBaseline = "bottom";
        const endLabel = fmtTime(dur);
        const endWidth = dimCtx.measureText(endLabel).width;
        for (let t = 0; t <= dur + 0.001; t += step) {
          const x = (t / dur) * w;
          dimCtx.fillStyle = "rgba(255, 255, 255, 0.05)";
          dimCtx.fillRect(Math.round(x), 0, 1, h);
          if (x + endWidth + 14 > w) continue;
          dimCtx.fillStyle = "rgba(139, 151, 169, 0.7)";
          dimCtx.fillText(fmtTime(t), x + 3, h - 4);
        }
        dimCtx.fillStyle = "rgba(139, 151, 169, 0.85)";
        dimCtx.fillText(endLabel, w - endWidth - 4, h - 4);
      }
      paintBars(dimCtx, peaks, w, h, "rgba(255, 255, 255, 0.12)", 26);
    }

    if (prepareLayer(canvas, layers.bright, dpr, key)) {
      const grad = layers.bright.ctx.createLinearGradient(0, 0, w, 0);
      grad.addColorStop(0, "rgba(110, 231, 255, 0.95)");
      grad.addColorStop(0.55, "rgba(167, 139, 250, 0.95)");
      grad.addColorStop(1, "rgba(52, 211, 153, 0.95)");
      paintBars(layers.bright.ctx, peaks, w, h, grad, 26);
    }

    blitLayers(canvas, layers.dim, layers.bright, playX * dpr, dpr);
    drawLoopBand(ctx, w, h, dur);

    ctx.fillStyle = "rgba(255, 255, 255, 0.92)";
    ctx.fillRect(playX - 1, 0, 2, h);
    ctx.beginPath();
    ctx.moveTo(playX - 5, 0);
    ctx.lineTo(playX + 5, 0);
    ctx.lineTo(playX, 7);
    ctx.closePath();
    ctx.fill();
  }

  function niceStep(dur) {
    const options = [5, 10, 15, 30, 60, 120, 300, 600];
    for (const step of options) if (dur / step <= 9) return step;
    return 900;
  }

  function paintFrame(time, force) {
    const dur = duration();
    if (!force) {
      const now = performance.now();
      if (now - mixer.lastPaint < 33) return;
      mixer.lastPaint = now;
    } else {
      mixer.lastPaint = performance.now();
    }
    mixer.tracks.forEach((track) => drawTrackWave(track, time, dur));
    drawMasterWave(time, dur);
    updateReadout(time, dur);
    updateMeters();
  }

  const setText = (el, value) => {
    if (el && el.textContent !== value) el.textContent = value;
  };

  function updateReadout(time, dur) {
    setText(els.timeCur, fmtClock(time));
    setText(els.timeDur, fmtClock(dur));
    setText(els.timelineTotal, fmtClock(dur));
  }

  /* ---------------------------------------------------------------- meters */

  /* rms -> meter scale is a perceptual curve, so bake it into a lookup table
     instead of calling Math.pow seven times per frame. */
  const LEVEL_CURVE = new Float32Array(256);
  for (let i = 0; i < 256; i += 1) {
    LEVEL_CURVE[i] = Math.min(1, Math.pow(i / 255, 0.42) * 1.3);
  }

  function readLevel(analyser, buf) {
    if (!analyser || !buf) return 0;
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const value = (buf[i] - 128) / 128;
      sum += value * value;
    }
    const rms = Math.sqrt(sum / buf.length);
    return LEVEL_CURVE[Math.min(255, (rms * 255) | 0)];
  }

  /* Meters are written as a compositor-only transform, so a frame never
     triggers layout or style recalculation. */
  function updateMeters() {
    const tracks = mixer.tracks;
    for (let i = 0; i < tracks.length; i++) {
      const track = tracks[i];
      const raw = track.silent ? 0 : readLevel(track.analyser, track.meterBuf);
      track.level = Math.max(raw, track.level * 0.8);
      track.meterScale = clamp(track.level, 0, 1);
      track.els.meterFill.style.transform = `scaleX(${track.meterScale.toFixed(3)})`;
    }
    const masterRaw = readLevel(mixer.masterAnalyser, mixer.masterBuf);
    mixer.masterLevel = Math.max(masterRaw, (mixer.masterLevel || 0) * 0.8);
    els.masterMeter.style.transform = `scaleX(${clamp(mixer.masterLevel, 0, 1).toFixed(3)})`;
  }

  function decayMeters() {
    let frames = 0;
    const step = () => {
      frames += 1;
      updateMeters();
      if (frames < 26) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  /* -------------------------------------------------------------- transport */

  function duration() {
    const track = mixer.tracks[mixer.leader] || mixer.tracks[0];
    if (track && Number.isFinite(track.audio.duration) && track.audio.duration > 0) {
      return track.audio.duration;
    }
    const stem = state.job && state.job.stems ? state.job.stems[mixer.leader] : null;
    if (stem && stem.duration) return stem.duration;
    return (state.job && state.job.duration) || 0;
  }

  function currentTime() {
    const track = mixer.tracks[mixer.leader] || mixer.tracks[0];
    return track ? track.audio.currentTime || 0 : 0;
  }

  function seekAll(seconds, { paint = true } = {}) {
    const dur = duration();
    if (!dur) return;
    const target = clamp(seconds, 0, Math.max(0, dur - 0.03));
    mixer.tracks.forEach((track) => {
      try {
        track.audio.currentTime = target;
      } catch {
        /* element not ready yet: it will pick up the position on play */
      }
    });
    if (paint) paintFrame(target, true);
  }

  async function play() {
    if (!mixer.tracks.length) return;
    const ctx = ensureContext();
    if (ctx && ctx.state !== "running") {
      try {
        await ctx.resume();
      } catch {}
    }

    const dur = duration();
    let at = currentTime();
    if (dur && at >= dur - 0.06) at = mixer.loop.on && mixer.loop.a != null ? mixer.loop.a : 0;

    mixer.tracks.forEach((track) => {
      track.audio.playbackRate = mixer.rate;
      setPreservesPitch(track.audio);
      try {
        if (Math.abs((track.audio.currentTime || 0) - at) > 0.04) track.audio.currentTime = at;
        const started = track.audio.play();
        if (started && started.catch) started.catch(() => {});
      } catch {}
    });

    mixer.playing = true;
    updateTransport();
    startFrameLoop();
    paintFrame(at, true);
  }

  function pause() {
    if (!mixer.playing) return;
    mixer.playing = false;
    mixer.tracks.forEach((track) => {
      try {
        track.audio.pause();
      } catch {}
    });
    updateTransport();
    stopFrameLoop();
    paintFrame(currentTime(), true);
    decayMeters();
  }

  function togglePlay() {
    if (mixer.playing) pause();
    else play();
  }

  function stopTransport() {
    pause();
    seekAll(0);
  }

  function setPreservesPitch(audio) {
    try {
      if ("preservesPitch" in audio) audio.preservesPitch = true;
      if ("webkitPreservesPitch" in audio) audio.webkitPreservesPitch = true;
    } catch {}
  }

  function setSpeed(rate, { silent = false } = {}) {
    mixer.rate = rate;
    mixer.tracks.forEach((track) => {
      track.audio.playbackRate = rate;
      setPreservesPitch(track.audio);
    });
    els.speedButtons.forEach((button) => {
      const on = Number(button.dataset.rate) === rate;
      button.classList.toggle("is-on", on);
      button.setAttribute("aria-checked", String(on));
    });
    renderRateNote();
    if (!silent) paintFrame(currentTime(), true);
  }

  function renderRateNote() {
    const format = mixer.format || "24-bit WAV";
    const speed = mixer.rate === 1 ? "1.0" : String(mixer.rate);
    els.timelineRate.textContent = `${speed}× · ${format}`;
  }

  function updateTransport() {
    els.playBtn.setAttribute("aria-pressed", String(mixer.playing));
    els.playBtn.setAttribute("aria-label", mixer.playing ? "Pause (Space)" : "Play (Space)");
    els.playBtn.classList.toggle("is-playing", mixer.playing);
  }

  function startFrameLoop() {
    if (mixer.raf) return;
    mixer.frames = 0;
    mixer.raf = requestAnimationFrame(frame);
  }

  function stopFrameLoop() {
    if (mixer.raf) cancelAnimationFrame(mixer.raf);
    mixer.raf = 0;
  }

  function frame() {
    if (!mixer.playing) {
      mixer.raf = 0;
      return;
    }
    mixer.raf = requestAnimationFrame(frame);
    mixer.frames += 1;

    const leader = mixer.tracks[mixer.leader] || mixer.tracks[0];
    if (!leader) return;
    const dur = duration();
    let time = leader.audio.currentTime || 0;

    if (mixer.loop.on && dur) {
      const a = mixer.loop.a == null ? 0 : mixer.loop.a;
      const b = mixer.loop.b == null ? dur : mixer.loop.b;
      if (b - a > 0.2 && time >= b) {
        seekAll(a, { paint: false });
        time = a;
      }
    }

    // Every stem is nudged back onto the leader's clock so drift never builds up.
    mixer.tracks.forEach((track, index) => {
      if (index === mixer.leader || track.audio.readyState < 2) return;
      if (Math.abs((track.audio.currentTime || 0) - time) > 0.08) {
        try {
          track.audio.currentTime = time;
        } catch {}
      }
    });

    if (leader.audio.ended) {
      if (mixer.loop.on) {
        seekAll(mixer.loop.a == null ? 0 : mixer.loop.a, { paint: false });
        play();
      } else {
        pause();
      }
      return;
    }

    if (mixer.frames % 2 === 0) paintFrame(time, false);
  }

  /* ------------------------------------------------------------ mute / solo */

  function applyGains() {
    const anySolo = mixer.tracks.some((track) => track.solo);
    mixer.tracks.forEach((track) => {
      const audible = !track.muted && (!anySolo || track.solo);
      track.silent = !audible;
      const target = audible ? track.volume : 0;
      if (track.gain && mixer.ctx) {
        track.gain.gain.setTargetAtTime(target, mixer.ctx.currentTime, 0.015);
      } else {
        track.audio.muted = !audible;
        track.audio.volume = clamp(track.volume, 0, 1);
      }
      track.els.row.classList.toggle("is-muted", track.muted);
      track.els.row.classList.toggle("is-solo", track.solo);
      track.els.row.classList.toggle("is-silent", !audible);
      track.els.muteBtn.setAttribute("aria-pressed", String(track.muted));
      track.els.soloBtn.setAttribute("aria-pressed", String(track.solo));
    });
    paintFrame(currentTime(), true);
  }

  function setMute(index) {
    const track = mixer.tracks[index];
    if (!track) return;
    track.muted = !track.muted;
    applyGains();
  }

  function setSolo(index) {
    const track = mixer.tracks[index];
    if (!track) return;
    track.solo = !track.solo;
    applyGains();
  }

  function setVolume(index, percent) {
    const track = mixer.tracks[index];
    if (!track) return;
    track.volume = clamp(percent, 0, 125) / 100;
    track.els.db.textContent = fmtDb(percent);
    applyGains();
  }

  function setMasterVolume(percent) {
    mixer.masterVolume = clamp(percent, 0, 125) / 100;
    els.masterVolVal.textContent = fmtDb(percent);
    if (mixer.masterGain && mixer.ctx) {
      mixer.masterGain.gain.setTargetAtTime(mixer.masterVolume, mixer.ctx.currentTime, 0.02);
    } else {
      mixer.tracks.forEach((track) => {
        if (!track.gain) track.audio.volume = clamp(track.volume * mixer.masterVolume, 0, 1);
      });
    }
    paintFrame(currentTime(), true);
  }

  function selectTrack(index) {
    if (!mixer.tracks.length) return;
    mixer.focus = clamp(index, 0, mixer.tracks.length - 1);
    mixer.tracks.forEach((track, i) => track.els.row.classList.toggle("is-focused", i === mixer.focus));
  }

  /* ------------------------------------------------------------- A–B loop */

  function setLoopPoint(kind) {
    const dur = duration();
    if (!dur) return;
    const time = currentTime();
    if (kind === "a") {
      mixer.loop.a = time;
      if (mixer.loop.b != null && mixer.loop.b <= time + 0.2) mixer.loop.b = null;
    } else {
      mixer.loop.b = time;
      if (mixer.loop.a != null && mixer.loop.a >= time - 0.2) mixer.loop.a = null;
    }
    if (mixer.loop.a != null && mixer.loop.b != null) mixer.loop.on = true;
    updateLoopUI();
    paintFrame(currentTime(), true);
  }

  function toggleLoop() {
    if (mixer.loop.a == null && mixer.loop.b == null) {
      const dur = duration();
      const start = currentTime();
      mixer.loop.a = start;
      mixer.loop.b = Math.min(dur || start + 15, start + 15);
      mixer.loop.on = true;
    } else {
      mixer.loop.on = !mixer.loop.on;
    }
    updateLoopUI();
    paintFrame(currentTime(), true);
  }

  function clearLoop() {
    mixer.loop = { a: null, b: null, on: false };
    updateLoopUI();
    paintFrame(currentTime(), true);
  }

  function updateLoopUI() {
    const { a, b, on } = mixer.loop;
    els.loopToggle.setAttribute("aria-pressed", String(on));
    els.loopToggle.classList.toggle("is-on", on);
    els.loopReadout.textContent = `A ${a == null ? "—" : fmtClock(a)} · B ${b == null ? "—" : fmtClock(b)}`;
    els.loopReadout.classList.toggle("is-armed", on);
    if (els.timelineLoopTag) {
      els.timelineLoopTag.textContent = on
        ? `loop ${a == null ? "0:00.0" : fmtClock(a)} – ${b == null ? "end" : fmtClock(b)}`
        : "no loop";
      els.timelineLoopTag.classList.toggle("is-armed", on);
    }
    els.timelineNote.textContent = on
      ? `Looping ${a == null ? "0:00.0" : fmtClock(a)} → ${b == null ? "end" : fmtClock(b)} across every stem`
      : "Click or drag the timeline to seek every stem";
  }

  /* ---------------------------------------------------------- keyboard map */

  const NUMBER_KEYS = ["1", "2", "3", "4", "5", "6"];

  function onKeyDown(event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target;
    const tag = (target && target.tagName ? target.tagName : "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || (target && target.isContentEditable)) {
      return;
    }
    if (!isStudioOpen()) return;

    const key = event.key;
    const slot = NUMBER_KEYS.indexOf(key);
    if (slot >= 0 && slot < mixer.tracks.length) {
      event.preventDefault();
      setSolo(slot);
      selectTrack(slot);
      return;
    }

    switch (key) {
      case " ":
        event.preventDefault();
        togglePlay();
        break;
      case "m":
      case "M":
        event.preventDefault();
        setMute(mixer.focus);
        break;
      case "s":
      case "S":
        event.preventDefault();
        setSolo(mixer.focus);
        break;
      case "[":
        event.preventDefault();
        setLoopPoint("a");
        break;
      case "]":
        event.preventDefault();
        setLoopPoint("b");
        break;
      case "l":
      case "L":
        event.preventDefault();
        toggleLoop();
        break;
      case "b":
      case "B":
        event.preventDefault();
        bounceMix();
        break;
      case "Escape":
        clearLoop();
        break;
      case "ArrowLeft":
        event.preventDefault();
        seekAll(currentTime() - (event.shiftKey ? 5 : 2));
        break;
      case "ArrowRight":
        event.preventDefault();
        seekAll(currentTime() + (event.shiftKey ? 5 : 2));
        break;
      case "Home":
        event.preventDefault();
        seekAll(0);
        break;
      case "End": {
        event.preventDefault();
        const dur = duration();
        seekAll(Math.max(0, dur - 0.05));
        break;
      }
      default:
        break;
    }
  }

  /* --------------------------------------------------------------- bounce */

  function showBounceNote(message, kind = "info") {
    if (!els.bounceNote) return;
    els.bounceNote.hidden = !message;
    els.bounceNote.dataset.kind = kind;
    els.bounceNote.innerHTML = message;
  }

  function triggerDownload(url) {
    const link = document.createElement("a");
    link.href = url;
    link.download = "";
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  async function bounceMix() {
    if (!isStudioOpen() || !state.job) return;
    const stems = mixer.tracks.map((track) => ({
      slug: track.stem.slug,
      gain: track.silent ? 0 : track.volume,
    }));
    if (!stems.some((stem) => stem.gain > 0.0005)) {
      showBounceNote("Every stem is muted — unmute one to bounce a mix.", "warn");
      return;
    }

    els.bounceBtn.disabled = true;
    els.bounceBtn.classList.add("is-busy");
    setText(els.bounceLabel, "Bouncing…");
    showBounceNote("");

    const live = mixer.playing;
    pause();

    try {
      const result = await api(`/api/jobs/${state.job.id}/mixdown`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stems }),
      });
      const used = (result.stems || []).join(" + ");
      showBounceNote(
        `Bounced <a href="${esc(result.download_url)}" download>${esc(result.filename)}</a> · ` +
          `${fmtBytes(result.bytes)} · ${result.bit_depth}-bit ` +
          `${((result.sample_rate || 44100) / 1000).toFixed(1)} kHz · ${used}`,
        "ok"
      );
      triggerDownload(result.download_url);
    } catch (err) {
      showBounceNote(`Bounce failed: ${esc(err.message)}`, "warn");
    } finally {
      els.bounceBtn.disabled = false;
      els.bounceBtn.classList.remove("is-busy");
      setText(els.bounceLabel, "Bounce mix");
      if (live) play();
    }
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
          <span class="h-status ${esc(job.status)}">${esc(job.status)}</span>
          <span class="h-name">${esc(job.filename)}</span>
          <span class="h-tag">${esc(job.mode_name)} · ${stamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
        `;
        if (job.status === "done" && job.stems.length) {
          const open = document.createElement("button");
          open.type = "button";
          open.className = "btn ghost small";
          open.textContent = "Open";
          open.addEventListener("click", () => {
            state.job = job;
            state.busy = false;
            finishSuccess(job);
            window.scrollTo({ top: els.studio.offsetTop - 90, behavior: "smooth" });
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

  function resetForNewTrack() {
    stopPolling();
    stopElapsedTimer();
    disposeStudio();
    state.job = null;
    state.busy = false;
    els.studio.hidden = true;
    els.progressPanel.hidden = true;
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
  els.newTrack.addEventListener("click", resetForNewTrack);
  els.bounceBtn.addEventListener("click", bounceMix);
  els.playBtn.addEventListener("click", togglePlay);
  els.stopBtn.addEventListener("click", stopTransport);
  els.speedButtons.forEach((button) =>
    button.addEventListener("click", () => setSpeed(Number(button.dataset.rate)))
  );
  els.setA.addEventListener("click", () => setLoopPoint("a"));
  els.setB.addEventListener("click", () => setLoopPoint("b"));
  els.loopToggle.addEventListener("click", toggleLoop);
  els.clearLoop.addEventListener("click", clearLoop);
  els.masterVol.addEventListener("input", () => setMasterVolume(Number(els.masterVol.value)));
  attachScrubber(els.masterWave, null);
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("resize", () => {
    if (mixer.tracks.length) paintFrame(currentTime(), true);
  });

  boot();
})();
