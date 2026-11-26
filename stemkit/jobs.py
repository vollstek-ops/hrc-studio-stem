"""Job queue, state and lifecycle for separation requests.

One worker thread runs one separation at a time (the models saturate the CPU,
and running two at once is the fastest way to OOM a laptop). Jobs are tracked
in memory and mirrored to `work/<id>/job.json` so a restart does not lose
finished results.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from . import audio, progress
from .config import (
    ACCEPTED_EXTENSIONS,
    DEFAULT_QUALITY,
    JOB_TTL_HOURS,
    WORK_DIR,
    get_mode,
    get_quality,
)
from .separator import separate_file

logger = logging.getLogger("stemkit.jobs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

TERMINAL = {DONE, ERROR, CANCELLED}

# The fixed pipeline every separation walks through. The UI draws these as
# steps with a live state, so the ids must stay stable.
PIPELINE_FETCH: list[tuple[str, str]] = [
    ("fetch", "Fetching audio"),
]

PIPELINE: list[tuple[str, str]] = [
    ("decode", "Decoding to 24-bit"),
    ("model", "Loading model"),
    ("separate", "Separating stems"),
    ("write", "Writing 24-bit WAVs"),
    ("finalise", "Verifying & finalising"),
]

# `audio-separator` emits its own stage names through a log handler; map every
# name we know onto one pipeline step.
STAGE_STEPS = {
    "Fetching audio": "fetch",
    "Starting": "decode",
    "Decoding to 24-bit": "decode",
    "Loading model": "model",
    "Downloading model": "model",
    "Separating": "separate",
    "Writing stems": "write",
    "Finalising": "finalise",
    "Complete": "finalise",
    "Cancelling": "separate",
}

STEP_ORDER = {
    step_id: index
    for index, (step_id, _) in enumerate(PIPELINE_FETCH + [step for step in PIPELINE if step[0] != "fetch"])
}


# A separation runs in a dedicated subprocess (see stemkit/worker.py). If the
# GPU deadlocks, the parent kills the process and retries in a fresh one.
MAX_HANG_RETRIES = 2
HANG_TIMEOUT_SECONDS = 900.0  # no job.json progress for 15 min -> hung
CANCEL_GRACE_SECONDS = 60.0   # worker gets this long to honour a cancel


def safe_filename(name: str) -> str:
    """Strip directories, control characters and anything shell-hostile."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip().strip(".")
    if not name:
        name = "audio"
    if len(name) > 180:
        stem, _, suffix = name.rpartition(".")
        name = f"{stem[:160]}.{suffix}" if stem else name[:180]
    return name


def sanitize_stem_filename(name: str) -> str:
    keep = []
    for ch in safe_filename(name):
        keep.append(ch if (ch.isalnum() or ch in " ._-()[]") else "_")
    cleaned = "".join(keep).strip()
    return cleaned or "audio"


class Job:
    def __init__(
        self,
        job_id: str,
        *,
        mode: str,
        original_name: str,
        size: int,
        quality: str = DEFAULT_QUALITY,
        source_url: str = "",
        device: str = "auto",
    ):
        self.id = job_id
        self.mode = mode
        self.quality = quality
        self.source_url = source_url
        self.device = device
        self.original_name = original_name
        self.size = size
        self.status = QUEUED
        self.stage = "Queued"
        self.progress = 0.0
        self.message = ""
        self.error = ""
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.duration = 0.0
        self.elapsed = 0.0
        self.stems: list[dict] = []
        self.cancel_requested = False
        self.step = ""
        self.step_started: dict[str, float] = {}
        self.step_finished: dict[str, float] = {}
        self._eta: float | None = None
        self._lock = threading.Lock()

    # -- paths ------------------------------------------------------------
    @property
    def dir(self) -> Path:
        return WORK_DIR / self.id

    @property
    def input_path(self) -> Path:
        return self.dir / "source" / self.original_name

    # -- state ------------------------------------------------------------
    def update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self, key, value)
            if "stage" in changes:
                self._enter_step(str(changes["stage"]))
            if self.status in TERMINAL:
                if self.finished_at is None:
                    self.finished_at = time.time()
                self._close_steps(self.finished_at)

    # -- pipeline timing --------------------------------------------------
    def pipeline(self) -> list[tuple[str, str]]:
        return PIPELINE_FETCH + PIPELINE if self.source_url else PIPELINE

    def _enter_step(self, stage: str) -> None:
        step = STAGE_STEPS.get(stage)
        if step is None or step == self.step:
            return
        now = time.time()
        # index within *this job's* pipeline: URL jobs carry an extra first step
        order = [step_id for step_id, _ in self.pipeline()]
        if step not in order:
            return
        index = order.index(step)
        self._close_steps(now, before=index, fill_skipped=True)
        self.step_started.setdefault(step, now)
        self.step_finished.pop(step, None)
        self.step = step

    def _close_steps(self, when: float, before: int | None = None, *, fill_skipped: bool = False) -> None:
        steps = self.pipeline()
        limit = len(steps) if before is None else before
        for step_id, _ in steps[:limit]:
            if step_id not in self.step_started:
                # A step we never saw (e.g. a warm cached model) is not a lie:
                # it took no meaningful time, so record it as instantaneous.
                if not fill_skipped:
                    continue
                self.step_started[step_id] = when
            if step_id not in self.step_finished:
                self.step_finished[step_id] = when

    def _step_payload(self) -> list[dict]:
        payload = []
        for step_id, label in self.pipeline():
            started = self.step_started.get(step_id)
            finished = self.step_finished.get(step_id)
            if self.status == DONE:
                started = started or self.started_at or self.created_at
                finished = finished or self.finished_at or started
                state = "done"
            elif finished is not None:
                state = "done"
            elif started is not None:
                state = "active"
            else:
                state = "pending"
            payload.append(
                {
                    "id": step_id,
                    "label": label,
                    "state": state,
                    "seconds": round(finished - started, 1) if started and finished else None,
                }
            )
        return payload

    def eta_seconds(self) -> float | None:
        """Smoothed seconds remaining, or None while it cannot be estimated."""
        if self.status != RUNNING or self.started_at is None or self.progress <= 0.02:
            return None
        elapsed = time.time() - self.started_at
        if elapsed < 1.5 or self.progress >= 0.999:
            return None
        rate = self.progress / elapsed
        if rate <= 1e-6:
            return None
        raw = max(0.0, (1.0 - self.progress) / rate)
        # Blend, so one fast chunk never makes the estimate leap around.
        self._eta = raw if self._eta is None else self._eta + (raw - self._eta) * 0.3
        return round(max(0.0, self._eta), 1)

    def snapshot(self, base_url: str = "", *, include_peaks: bool = True) -> dict:
        with self._lock:
            cfg = get_mode(self.mode)
            payload = {
                "id": self.id,
                "status": self.status,
                "mode": self.mode,
                "mode_name": cfg["name"],
                "quality": self.quality,
                "quality_name": get_quality(self.quality)["name"],
                "device": self.device,
                "source_kind": "url" if self.source_url else "upload",
                "source_url": self.source_url,
                "model": cfg["model"],
                "model_label": cfg["model_label"],
                "filename": self.original_name,
                "size": self.size,
                "stage": self.stage,
                "progress": round(self.progress, 4),
                "message": self.message,
                "error": self.error,
                "duration": self.duration,
                # Live wall-clock while running, final duration once finished.
                "elapsed": round(
                    self.elapsed
                    if self.finished_at
                    else (time.time() - self.started_at if self.started_at else 0.0),
                    1,
                ),
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "eta_seconds": self.eta_seconds(),
                "pipeline": self._step_payload(),
                "download_all_url": f"{base_url}/api/jobs/{self.id}/download-all",
                "stems": [
                    {
                        "name": stem["name"],
                        "slug": stem["slug"],
                        "filename": stem["filename"],
                        "bytes": stem["bytes"],
                        "duration": stem["duration"],
                        "sample_rate": stem["sample_rate"],
                        "channels": stem["channels"],
                        "bit_depth": stem["bit_depth"],
                        "subtype": stem.get("subtype", ""),
                        # 480 floats per stem: skipped for list views.
                        "peaks": stem.get("peaks", []) if include_peaks else [],
                        "stream_url": f"{base_url}/api/jobs/{self.id}/audio/{stem['slug']}",
                        "download_url": f"{base_url}/api/jobs/{self.id}/download/{stem['slug']}",
                    }
                    for stem in self.stems
                ],
            }
            return payload

    def persist(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            data = self.snapshot()
            data.pop("stems", None)
            data["stems"] = [
                {
                    key: value
                    for key, value in stem.items()
                    if key in {"name", "slug", "filename", "bytes", "duration", "sample_rate", "channels", "bit_depth", "subtype", "path", "peaks"}
                }
                for stem in self.stems
            ]
            data["step_started"] = dict(self.step_started)
            data["step_finished"] = dict(self.step_finished)
            # The cancel flag may be set by the parent process after this
            # process last wrote; never let a routine tick erase it.
            data["cancel_requested"] = bool(self.cancel_requested or self.cancel_flag_on_disk())
            payload = json.dumps(data, indent=2)
            target = self.dir / "job.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)  # atomic: readers never see a partial file
        except Exception:  # noqa: BLE001 - persistence is best effort
            logger.debug("Could not persist job %s", self.id, exc_info=True)

    def cancel_flag_on_disk(self) -> bool:
        """Read the cancel flag back from job.json (set by the parent process)."""
        try:
            data = json.loads((self.dir / "job.json").read_text(encoding="utf-8"))
            return bool(data.get("cancel_requested"))
        except Exception:  # noqa: BLE001 - unreadable file simply means "no"
            return False


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._janitor: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle --------------------------------------------------------
    def start(self, run_worker: bool = True, run_janitor: bool = True) -> None:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._load_existing()
        if run_worker and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._run_forever, name="stem-worker", daemon=True)
            self._worker.start()
        if run_janitor and (self._janitor is None or not self._janitor.is_alive()):
            self._janitor = threading.Thread(target=self._sweep_forever, name="stem-janitor", daemon=True)
            self._janitor.start()

    def stop(self) -> None:
        self._stop.set()

    def _load_existing(self) -> None:
        """Re-register jobs whose folders survived a restart."""
        if not WORK_DIR.exists():
            return
        for job_dir in WORK_DIR.iterdir():
            meta = job_dir / "job.json"
            if not job_dir.is_dir() or not meta.exists():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                job = Job(
                    data["id"],
                    mode=data["mode"],
                    original_name=data["filename"],
                    size=data.get("size", 0),
                    quality=data.get("quality", DEFAULT_QUALITY),
                    source_url=data.get("source_url", "") or "",
                    device=data.get("device", "auto") or "auto",
                )
                job.status = data.get("status", DONE)
                if job.status not in TERMINAL and not os.environ.get("STEM_WORKER_SPAWNED"):
                    # A crash mid-run: the audio may or may not be intact. A
                    # spawned worker loads a RUNNING job on purpose, so this
                    # rule is for real restarts only.
                    job.status, job.stage, job.error = ERROR, "Interrupted", "Server restarted during separation."
                job.stage = data.get("stage", job.stage)
                job.progress = float(data.get("progress", 0.0))
                job.duration = float(data.get("duration", 0.0))
                job.elapsed = float(data.get("elapsed", 0.0))
                job.created_at = float(data.get("created_at", job.created_at))
                job.started_at = data.get("started_at")
                job.finished_at = data.get("finished_at")
                job.stems = [stem for stem in data.get("stems", []) if (Path(stem.get("path", ""))).exists()]
                # Reload the per-step timings so a reopened job still shows them.
                for step in data.get("pipeline", []):
                    step_id = step.get("id")
                    if step_id not in STEP_ORDER:
                        continue
                    if step.get("state") == "done":
                        job.step_started.setdefault(step_id, job.created_at)
                        job.step_finished.setdefault(step_id, job.finished_at or job.created_at)
                    elif step.get("state") == "active":
                        job.step_started.setdefault(step_id, job.created_at)
                        job.step = step_id
                with self._lock:
                    self._jobs[job.id] = job
            except Exception:  # noqa: BLE001
                logger.debug("Skipping unreadable job folder %s", job_dir, exc_info=True)

    def _sweep_forever(self) -> None:
        ttl = JOB_TTL_HOURS * 3600
        while not self._stop.is_set():
            try:
                cutoff = time.time() - ttl
                expired: list[str] = []
                with self._lock:
                    for job_id, job in self._jobs.items():
                        if job.status in TERMINAL and (job.finished_at or job.created_at) < cutoff:
                            expired.append(job_id)
                for job_id in expired:
                    self.delete(job_id)
            except Exception:  # noqa: BLE001
                logger.debug("Janitor pass failed", exc_info=True)
            self._stop.wait(600)

    # -- public API -------------------------------------------------------
    def submit(
        self,
        *,
        mode: str,
        filename: str,
        source_path: str | Path,
        size: int | None = None,
        quality: str = DEFAULT_QUALITY,
    ) -> Job:
        get_mode(mode)  # raises ValueError for unknown modes
        profile = get_quality(quality)  # ... and for unknown qualities
        job_id = uuid.uuid4().hex[:12]
        original = sanitize_stem_filename(filename or "audio.wav")
        suffix = Path(original).suffix.lower()
        if suffix and suffix not in ACCEPTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type {suffix!r}. Accepted: "
                + ", ".join(sorted(ACCEPTED_EXTENSIONS))
            )

        job = Job(
            job_id,
            mode=mode,
            original_name=original,
            size=int(size or 0),
            quality=profile["id"],
        )
        job.dir.mkdir(parents=True, exist_ok=True)
        job.input_path.parent.mkdir(parents=True, exist_ok=True)

        source = Path(source_path)
        if source.resolve() != job.input_path.resolve():
            shutil.move(str(source), str(job.input_path))
        job.size = job.input_path.stat().st_size

        with self._lock:
            self._jobs[job.id] = job
        job.persist()
        self._queue.put(job.id)
        return job

    def submit_url(
        self,
        *,
        url: str,
        mode: str,
        quality: str = DEFAULT_QUALITY,
    ) -> Job:
        """Queue a job whose audio still has to be fetched from a link."""
        get_mode(mode)
        profile = get_quality(quality)
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id,
            mode=mode,
            original_name="fetched audio.wav",
            size=0,
            quality=profile["id"],
            source_url=url,
        )
        job.dir.mkdir(parents=True, exist_ok=True)
        job.input_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
        job.persist()
        self._queue.put(job.id)
        return job

    def submit_existing(
        self,
        *,
        mode: str,
        source_path: str | Path,
        quality: str = DEFAULT_QUALITY,
    ) -> Job:
        """Submit a file that already lives somewhere on disk (CLI use)."""
        source = Path(source_path)
        return self.submit(
            mode=mode,
            filename=source.name,
            source_path=source,
            size=source.stat().st_size,
            quality=quality,
        )

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 30) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)
        return jobs[:limit]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in TERMINAL:
            return False
        job.update(cancel_requested=True, stage="Cancelling", message="Stopping after the current chunk")
        job.persist()  # the worker process reads the flag from disk
        return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        job.update(cancel_requested=True)
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    def protected_dirs(self) -> list[Path]:
        """Job folders that must not be touched (queued or running work)."""
        with self._lock:
            return [job.dir for job in self._jobs.values() if job.status in {QUEUED, RUNNING}]

    def sweep_now(self) -> list[str]:
        """Delete every terminal job past its TTL immediately."""
        cutoff = time.time() - JOB_TTL_HOURS * 3600
        expired: list[str] = []
        with self._lock:
            for job_id, job in self._jobs.items():
                if job.status in TERMINAL and (job.finished_at or job.created_at) < cutoff:
                    expired.append(job_id)
        for job_id in expired:
            self.delete(job_id)
        return expired

    def stem_path(self, job_id: str, slug: str) -> tuple[Path | None, str]:
        """Resolve a stem file, refusing anything that escapes the job folder."""
        job = self.get(job_id)
        if job is None:
            return None, ""
        for stem in job.stems:
            if stem["slug"] == slug:
                path = Path(stem["path"]).resolve()
                if job.dir.resolve() in path.parents and path.exists():
                    return path, stem.get("filename", path.name)
        return None, ""

    # -- worker -----------------------------------------------------------
    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            job = self.get(job_id)
            if job is None:
                continue
            try:
                self._execute_in_subprocess(job)
            except Exception:  # noqa: BLE001 - a job must never kill the worker
                logger.exception("Unexpected failure in job %s", job_id)
                job.update(status=ERROR, stage="Failed", error="Unexpected internal error. See server logs.")
                job.persist()

    def _execute_in_subprocess(self, job: Job) -> None:
        """Run one job in `python -m stemkit.worker`, watching for a GPU hang.

        The worker reports progress by rewriting `job.json`; this process tails
        that file so the API keeps serving live progress. If the worker stops
        reporting (torch/MPS deadlock) it is killed and the job is retried in a
        fresh process — a new Metal allocator, no inherited fragmentation.
        """
        job.update(
            status=RUNNING,
            started_at=job.started_at or time.time(),
            stage="Fetching audio" if job.source_url else "Starting",
            progress=0.01,
        )
        job.persist()

        root = Path(__file__).resolve().parent.parent
        command = [sys.executable, "-m", "stemkit.worker", job.id]
        env = dict(os.environ, PYTHONUNBUFFERED="1", STEM_WORKER_SPAWNED="1")

        attempt = 0
        while attempt <= MAX_HANG_RETRIES:
            attempt += 1
            log_path = job.dir / "worker.log"
            hung = False
            last_change = time.time()
            cancel_sent_at: float | None = None
            with open(log_path, "ab") as log_handle:
                proc = subprocess.Popen(
                    command,
                    cwd=root,
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
                while proc.poll() is None:
                    time.sleep(1.5)
                    self._sync_from_disk(job)

                    if job.cancel_requested:
                        cancel_sent_at = cancel_sent_at or time.time()
                        if time.time() - cancel_sent_at > CANCEL_GRACE_SECONDS:
                            logger.warning("Job %s: worker did not honour cancel, killing it", job.id)
                            proc.kill()
                            break

                    try:
                        mtime = (job.dir / "job.json").stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    if job.status in TERMINAL:
                        # The worker reported a final state but its process is
                        # still alive (a blocked inference thread cannot exit).
                        logger.warning("Job %s: worker reported %s but would not exit; reaping it", job.id, job.status)
                        proc.kill()
                        break

                    if mtime > last_change:
                        last_change = mtime
                    elif time.time() - last_change > HANG_TIMEOUT_SECONDS:
                        logger.error(
                            "Job %s: worker made no progress for %.0f minutes — killing it (attempt %d/%d)",
                            job.id, HANG_TIMEOUT_SECONDS / 60, attempt, MAX_HANG_RETRIES + 1,
                        )
                        proc.kill()
                        hung = True
                        break

            proc.wait()
            self._sync_from_disk(job)

            if job.cancel_requested and job.status not in TERMINAL:
                # The worker was killed during the cancel grace period, so it
                # never got to report the cancellation itself.
                job.update(status=CANCELLED, stage="Cancelled", finished_at=time.time())
                job.persist()

            if job.status in TERMINAL:
                return

            if hung and attempt <= MAX_HANG_RETRIES:
                logger.warning("Job %s: retrying in a fresh GPU context (attempt %d/%d)", job.id, attempt + 1, MAX_HANG_RETRIES + 1)
                job.update(
                    status=RUNNING,
                    stage="Retrying on a fresh GPU context",
                    message=f"GPU stall detected — retrying (attempt {attempt + 1} of {MAX_HANG_RETRIES + 1})",
                )
                job.persist()
                continue

            if hung:
                job.update(
                    status=ERROR,
                    stage="Failed",
                    error=(
                        "The GPU stalled repeatedly during inference and the job could not be "
                        "completed after retries. Try again later, or use a shorter track."
                    ),
                )
                job.persist()
                return

            job.update(
                status=ERROR,
                stage="Failed",
                error=f"The separation process exited unexpectedly (code {proc.returncode}). See worker.log.",
            )
            job.persist()
            return

    def _sync_from_disk(self, job: Job) -> None:
        """Mirror worker-written job.json into this process's Job object."""
        try:
            data = json.loads((job.dir / "job.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - mid-write or missing file: keep current state
            return
        with job._lock:
            job.status = data.get("status", job.status)
            job.stage = data.get("stage", job.stage)
            job.progress = float(data.get("progress", job.progress))
            job.message = data.get("message", job.message)
            job.error = data.get("error", job.error)
            job.duration = float(data.get("duration") or job.duration)
            job.elapsed = float(data.get("elapsed") or job.elapsed)
            job.original_name = data.get("filename", job.original_name)
            job.size = int(data.get("size") or job.size)
            job.cancel_requested = job.cancel_requested or bool(data.get("cancel_requested"))
            if data.get("finished_at") and job.finished_at is None:
                job.finished_at = data["finished_at"]
            job.step_started.update({k: v for k, v in (data.get("step_started") or {}).items()})
            job.step_finished.update({k: v for k, v in (data.get("step_finished") or {}).items()})
            if data.get("stems") and not job.stems:
                job.stems = [
                    stem for stem in data["stems"]
                    if stem.get("path") and Path(stem["path"]).exists()
                ]

    def _fetch_source(self, job: Job) -> bool:
        """Download the job's link. Returns False when the job already ended."""
        from . import youtube  # local import: yt-dlp is an optional dependency

        def on_progress(fraction: float) -> None:
            # The download is the first 5% of the overall job.
            job.update(progress=min(0.05, 0.005 + 0.045 * max(0.0, min(1.0, fraction))))
            job.persist()

        try:
            info = youtube.download(
                job.source_url,
                job.input_path.parent,
                on_progress=on_progress,
                is_cancelled=lambda: job.cancel_requested,
            )
        except youtube.FetchCancelled:
            job.update(status=CANCELLED, stage="Cancelled", progress=0.0, message="Download cancelled")
            job.persist()
            return False
        except youtube.FetchError as exc:
            logger.warning("Could not fetch %s: %s", job.source_url, exc)
            job.update(status=ERROR, stage="Failed", error=str(exc)[:2000])
            job.persist()
            return False
        except Exception as exc:  # noqa: BLE001 - never kill the worker
            logger.exception("Unexpected fetch failure for %s", job.source_url)
            job.update(status=ERROR, stage="Failed", error=f"Could not fetch that link: {exc}"[:2000])
            job.persist()
            return False

        job.update(
            original_name=info["path"].name,
            size=int(info.get("bytes") or 0),
            duration=float(info.get("duration") or 0),
            progress=0.05,
            stage="Starting",
        )
        job.persist()
        return True

    def execute_job(self, job: Job) -> None:
        """Run a job to completion in THIS process (used by stemkit.worker)."""
        self._execute(job)

    def _execute(self, job: Job) -> None:
        job.update(
            status=RUNNING,
            started_at=time.time(),
            stage="Fetching audio" if job.source_url else "Starting",
            progress=0.01,
        )
        job.persist()

        if job.source_url and not self._fetch_source(job):
            return

        def on_stage(name: str, fraction: float | None) -> None:
            nonlocal last_tick
            last_tick = time.time()
            if fraction is None:
                job.update(stage=name)
            else:
                job.update(stage=name, progress=min(0.99, max(job.progress, fraction)))
            job.persist()  # the parent tails job.json for live progress

        def is_cancelled() -> bool:
            return job.cancel_requested or job.cancel_flag_on_disk()

        # Watchdog state: `last_tick` is touched by every real progress event
        # from the separator (each inference chunk reports one). The GPU can
        # deadlock inside torch/MPS (istft -> MPSStream sync) and hang the
        # worker with 0% CPU; without this the job would spin forever.
        last_tick = time.time()

        # Gentle creep so the bar never looks frozen while a chunk computes.
        stop_creep = threading.Event()

        def creep() -> None:
            nonlocal last_tick
            while not stop_creep.wait(2.5):
                if job.cancel_requested or job.status != RUNNING:
                    continue
                if job.stage == "Separating" and time.time() - last_tick > 900:
                    logger.error(
                        "Job %s stalled: no inference progress for 15 minutes (likely a torch/MPS hang).",
                        job.id,
                    )
                    job.update(
                        status=ERROR,
                        stage="Failed",
                        error=(
                            "Inference stalled on the GPU with no progress for 15 minutes "
                            "(a known PyTorch/MPS hang). Re-run the separation — a retry usually "
                            "completes. If it stalls again, restart the app to free the GPU."
                        ),
                    )
                    job.persist()
                    stop_creep.set()
                    return
                # No synthetic progress: the parent tails real ticks from
                # job.json, so the bar only ever moves for real work.

        creep_thread = threading.Thread(target=creep, name=f"creep-{job.id}", daemon=True)
        creep_thread.start()

        try:
            info = audio.probe(job.input_path)
            job.update(duration=float(info.get("duration") or 0))
        except Exception:  # noqa: BLE001 - surfaced properly by the runner
            pass

        try:
            result = separate_file(
                job.input_path,
                job.mode,
                job.dir,
                quality=job.quality,
                device=job.device,
                on_stage=on_stage,
                is_cancelled=is_cancelled,
            )
        except progress.CancelledError:
            stop_creep.set()
            job.update(status=CANCELLED, stage="Cancelled", progress=0.0, message="Separation cancelled")
            job.persist()
            return
        except Exception as exc:  # noqa: BLE001 - reported to the client verbatim
            stop_creep.set()
            logger.warning("Job %s failed: %s", job.id, exc)
            job.update(status=ERROR, stage="Failed", error=str(exc)[:2000])
            job.persist()
            return
        finally:
            stop_creep.set()

        job.update(
            status=DONE,
            stage="Complete",
            progress=1.0,
            message="Stems ready",
            stems=result["stems"],
            elapsed=result["elapsed"],
            duration=result.get("duration") or job.duration,
        )
        job.persist()


manager = JobManager()

__all__ = [
    "Job",
    "JobManager",
    "manager",
    "CANCELLED",
    "DONE",
    "ERROR",
    "PIPELINE",
    "QUEUED",
    "RUNNING",
    "TERMINAL",
]
