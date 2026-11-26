"""HRC Studio Stem — studio-quality multi-track stem extractor.

FastAPI service + drag-and-drop web UI around `audio-separator`.
    python app.py            # http://127.0.0.1:8000
"""

from __future__ import annotations

import logging
import math
import mimetypes
import os
import re
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from stemkit import __version__, audio
from stemkit.config import (
    ACCEPTED_EXTENSIONS,
    DEFAULT_MODE,
    MAX_UPLOAD_BYTES,
    MODEL_DIR,
    get_mode,
    public_modes,
)
from stemkit.jobs import DONE, manager, safe_filename, sanitize_stem_filename
from stemkit.separator import drop_cache

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
TEMPLATE_DIR = ROOT / "templates"

logging.basicConfig(
    level=os.environ.get("STEM_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("audio_separator").setLevel(logging.INFO)
logger = logging.getLogger("stem")

CHUNK = 1024 * 512
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


@asynccontextmanager
async def lifespan(_: FastAPI):
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if not audio.have_ffmpeg():
        logger.warning("ffmpeg not found on PATH — uploads other than WAV/FLAC will fail.")
    manager.start()
    logger.info(
        "HRC Studio Stem %s ready · models in %s · uploads limited to %d MB",
        __version__,
        MODEL_DIR,
        MAX_UPLOAD_BYTES // (1024 * 1024),
    )
    try:
        yield
    finally:
        manager.stop()
        drop_cache()


app = FastAPI(title="HRC Studio Stem", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        TEMPLATE_DIR / "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/favicon.svg", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": __version__,
        "ffmpeg": audio.have_ffmpeg(),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "accepted_extensions": sorted(ACCEPTED_EXTENSIONS),
    }


@app.get("/api/modes")
async def modes() -> dict:
    return {"default": DEFAULT_MODE, "modes": public_modes()}


@app.get("/api/jobs")
async def list_jobs(request: Request, limit: int = 20) -> dict:
    base = _base_url(request)
    # The history list never draws a waveform, so the peak arrays are left out.
    return {
        "jobs": [
            job.snapshot(base, include_peaks=False)
            for job in manager.list(limit=max(1, min(limit, 100)))
        ]
    }


@app.post("/api/jobs", status_code=201)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form(default=DEFAULT_MODE),
) -> JSONResponse:
    base = _base_url(request)
    if mode not in {item["id"] for item in public_modes()}:
        raise HTTPException(status_code=400, detail=f"Unknown mode {mode!r}.")

    filename = safe_filename(file.filename or "audio.wav")
    suffix = Path(filename).suffix.lower()
    if not suffix:
        raise HTTPException(status_code=400, detail="The file has no extension, so its format is unknown.")
    if suffix not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"{suffix} is not a supported audio format. Try WAV, FLAC, MP3, M4A or OGG.",
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="stem-upload-"))
    tmp_path = tmp_dir / filename
    written = 0
    try:
        with tmp_path.open("wb") as handle:
            while chunk := await file.read(CHUNK):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"That file is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
                    )
                handle.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        job = manager.submit(mode=mode, filename=filename, source_path=tmp_path, size=written)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.exception("Upload failed")
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc
    finally:
        await file.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return JSONResponse(status_code=201, content=job.snapshot(base))


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    return job.snapshot(_base_url(request))


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not manager.cancel(job_id):
        return {"cancelled": False, "job": job.snapshot(_base_url(request))}
    return {"cancelled": True, "job": job.snapshot(_base_url(request))}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    if not manager.delete(job_id):
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"deleted": True}


# --------------------------------------------------------------------------- #
# Audio delivery (with HTTP range support so the <audio> scrubber works)
# --------------------------------------------------------------------------- #


@app.api_route("/api/jobs/{job_id}/audio/{slug}", methods=["GET", "HEAD"])
async def stream_stem(job_id: str, slug: str, request: Request) -> Response:
    path, _ = manager.stem_path(job_id, slug)
    if path is None:
        raise HTTPException(status_code=404, detail="Stem not found.")
    return _ranged_file(path, request, media_type="audio/wav")


@app.get("/api/jobs/{job_id}/download/{slug}")
async def download_stem(job_id: str, slug: str, request: Request) -> Response:
    job = manager.get(job_id)
    path, filename = manager.stem_path(job_id, slug)
    if job is None or path is None:
        raise HTTPException(status_code=404, detail="Stem not found.")

    stem = next((s for s in job.stems if s["slug"] == slug), None)
    label = stem["name"] if stem else slug.title()
    base = Path(job.original_name).stem
    download_name = f"{base} - {label} (24-bit).wav"

    ascii_fallback = download_name.encode("ascii", "ignore").decode() or "stem.wav"
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(download_name)}"
    )
    return _ranged_file(
        path,
        request,
        media_type="audio/wav",
        extra_headers={"Content-Disposition": disposition},
    )


@app.get("/api/jobs/{job_id}/download-all")
async def download_all(job_id: str, request: Request) -> Response:
    """One-click ZIP with every 24-bit WAV stem plus a manifest.

    The archive is stored next to the stems and rebuilt only when a stem file
    is newer, so repeat downloads are instant even for a six-piece split.
    """
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    if job.status != DONE:
        raise HTTPException(
            status_code=409,
            detail=f"Stems are not ready yet (job is {job.status}).",
        )
    if not job.stems:
        raise HTTPException(status_code=409, detail="This job has no stems to download.")

    try:
        archive = await run_in_threadpool(_stem_archive, job)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the client
        logger.exception("Could not build the stem archive for %s", job_id)
        raise HTTPException(status_code=500, detail=f"Could not build the ZIP: {exc}") from exc

    download_name = archive.name
    ascii_fallback = download_name.encode("ascii", "ignore").decode() or "stems.zip"
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(download_name)}"
    )
    return _ranged_file(
        archive,
        request,
        media_type="application/zip",
        extra_headers={"Content-Disposition": disposition},
    )


def _stem_archive(job) -> Path:
    """Build (or reuse) `work/<id>/<track> - N stems (24-bit).zip`."""
    base = sanitize_stem_filename(Path(job.original_name).stem) or "stems"
    count = len(job.stems)
    noun = "stem" if count == 1 else "stems"
    archive = job.dir / f"{base} - {count} {noun} (24-bit).zip"

    existing = [Path(stem["path"]) for stem in job.stems]
    root = job.dir.resolve()
    escaped = [path for path in existing if root not in path.resolve().parents]
    if escaped:
        raise PermissionError("Refusing to archive a stem that lives outside the job folder.")
    missing = [path for path in existing if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Some stem files are no longer on disk (a job expires after a few hours). "
            "Re-run the separation to get a fresh ZIP."
        )
    newest = max(path.stat().st_mtime for path in existing)
    if archive.exists() and archive.stat().st_mtime >= newest:
        return archive

    partial = archive.with_name(archive.name + ".part")
    used: set[str] = set()
    lines: list[str] = []
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        for stem in job.stems:
            path = Path(stem["path"])
            label = stem.get("name") or stem.get("slug") or path.stem
            arcname = f"{base} - {label} (24-bit).wav"
            index = 2
            while arcname.lower() in used:
                arcname = f"{base} - {label} ({index}) (24-bit).wav"
                index += 1
            used.add(arcname.lower())
            bundle.write(path, arcname=arcname)
            depth = stem.get("bit_depth") or 24
            rate = (stem.get("sample_rate") or 44100) / 1000
            lines.append(
                f"  {arcname:<44} {depth}-bit / {rate:.1f} kHz / "
                f"{stem.get('channels', 2)} ch   {path.stat().st_size / 1048576:.1f} MB"
            )
        bundle.writestr("README.txt", _archive_manifest(job, lines))
    partial.replace(archive)
    return archive


# --------------------------------------------------------------------------- #
# Mixdown — bounce exactly what the studio is playing
# --------------------------------------------------------------------------- #

BOUNCE_DIR = "bounces"
MAX_BOUNCES = 6


@app.post("/api/jobs/{job_id}/mixdown", status_code=201)
async def create_mixdown(job_id: str, request: Request, payload: dict = Body(...)) -> JSONResponse:
    """Render the fader / mute / solo state to a single 24-bit WAV.

    The browser sends the resolved linear gain per stem (mute and solo already
    folded in), so the backend is a pure summing bus and never has to guess.
    """
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    if job.status != DONE or not job.stems:
        raise HTTPException(status_code=409, detail=f"Stems are not ready yet (job is {job.status}).")

    payload = payload if isinstance(payload, dict) else {}
    tracks = _mixdown_tracks(job, payload.get("stems"))
    if not tracks:
        raise HTTPException(status_code=400, detail="Every stem is muted — unmute one to bounce a mix.")

    try:
        result = await run_in_threadpool(_render_mixdown, job, tracks, payload.get("name"))
    except audio.AudioError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    base = _base_url(request)
    name = quote(result["filename"])
    return JSONResponse(
        status_code=201,
        content={
            **result,
            "bit_depth": 24,
            "stems": [track["label"] for track in tracks],
            "download_url": f"{base}/api/jobs/{job.id}/mixdown/{name}?download=1",
            "stream_url": f"{base}/api/jobs/{job.id}/mixdown/{name}",
        },
    )


@app.api_route("/api/jobs/{job_id}/mixdown/{filename}", methods=["GET", "HEAD"])
async def stream_mixdown(job_id: str, filename: str, request: Request) -> Response:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found (it may have expired).")
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Bad mixdown name.")

    folder = (job.dir / BOUNCE_DIR).resolve()
    path = (folder / filename).resolve()
    if folder not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="Mixdown not found (it may have been pruned).")

    headers = None
    if request.query_params.get("download") in {"1", "true", "yes"}:
        ascii_fallback = filename.encode("ascii", "ignore").decode() or "mixdown.wav"
        headers = {
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        }
    return _ranged_file(path, request, media_type="audio/wav", extra_headers=headers)


def _mixdown_tracks(job, requested) -> list[dict]:
    """Resolve requested stems into {slug, label, path, gain}, dropping silence."""
    by_slug = {stem["slug"]: stem for stem in job.stems}
    if not isinstance(requested, list) or not requested:
        return [
            {"slug": stem["slug"], "label": stem["name"], "path": Path(stem["path"]), "gain": 1.0}
            for stem in job.stems
        ]

    tracks: list[dict] = []
    for item in requested:
        if not isinstance(item, dict):
            continue
        stem = by_slug.get(str(item.get("slug") or ""))
        if stem is None:
            continue
        try:
            gain = float(item.get("gain", 1.0))
        except (TypeError, ValueError):
            gain = 1.0
        if not math.isfinite(gain):
            gain = 1.0
        gain = max(0.0, min(4.0, gain))
        if gain <= 0.0005:  # muted, or a solo elsewhere silenced it
            continue
        tracks.append(
            {"slug": stem["slug"], "label": stem["name"], "path": Path(stem["path"]), "gain": gain}
        )
    return tracks


def _render_mixdown(job, tracks: list[dict], name: str | None) -> dict:
    folder = job.dir / BOUNCE_DIR
    folder.mkdir(parents=True, exist_ok=True)

    track = sanitize_stem_filename(Path(job.original_name).stem) or "track"
    explicit = name.strip() if isinstance(name, str) and name.strip() else ""
    if explicit:
        base = sanitize_stem_filename(explicit) or "mix"
    else:
        base = track if "mix" in track.lower().split() else f"{track} mix"
    stamp = time.strftime("%H%M%S")
    target = folder / f"{base} {stamp}.wav"
    index = 2
    while target.exists():
        target = folder / f"{base} {stamp}-{index}.wav"
        index += 1

    reference = job.stems[0] if job.stems else {}
    audio.mixdown(
        [(track["path"], track["gain"]) for track in tracks],
        target,
        sample_rate=int(reference.get("sample_rate") or 44100),
        channels=int(reference.get("channels") or 2),
        title=f"{base} — {len(tracks)} stem mixdown",
    )
    _prune_bounces(folder)

    info = audio.probe(target)
    return {
        "filename": target.name,
        "bytes": target.stat().st_size,
        "duration": round(float(info.get("duration") or 0), 3),
        "sample_rate": info.get("sample_rate") or 0,
        "stems_used": len(tracks),
    }


def _prune_bounces(folder: Path) -> None:
    """Keep only the newest bounces so a long session cannot fill the disk."""
    try:
        files = sorted(folder.glob("*.wav"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in files[MAX_BOUNCES:]:
        try:
            stale.unlink()
        except OSError:
            logger.debug("Could not prune bounce %s", stale, exc_info=True)


def _archive_manifest(job, lines: list[str]) -> str:
    depth = next((stem.get("bit_depth") for stem in job.stems if stem.get("bit_depth")), 24)
    rate = next((stem.get("sample_rate") for stem in job.stems if stem.get("sample_rate")), 44100)
    minutes, seconds = divmod(int(round(job.duration or 0)), 60)
    try:
        cfg = get_mode(job.mode)
        mode_line = f"{cfg['name']} — {cfg['model_label']}"
        model_line = cfg["model"]
    except ValueError:  # pragma: no cover - mode was validated on submit
        mode_line, model_line = job.mode, "unknown"
    return "\n".join(
        [
            "HRC Studio Stem — studio stem extraction",
            "Vollstek · github.com/vollstek-ops/hrc-studio-stem",
            "",
            f"Source      : {job.original_name}",
            f"Mode        : {mode_line}",
            f"Model       : {model_line}",
            f"Length      : {minutes}:{seconds:02d}",
            f"Format      : {depth}-bit PCM WAV / {rate / 1000:.1f} kHz / lossless",
            f"Exported    : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
            "",
            "Stems:",
            *lines,
            "",
            "Each stem is the untouched model output at the original mix level, so",
            "summing all the files back together reconstructs the source track.",
            "",
        ]
    )


def _ranged_file(
    path: Path,
    request: Request,
    *,
    media_type: str = "application/octet-stream",
    extra_headers: dict | None = None,
) -> Response:
    """Serve a file, honouring a single `bytes=` range (needed for seeking)."""
    size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=600",
        "Content-Type": media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }
    if extra_headers:
        headers.update(extra_headers)

    range_header = request.headers.get("range")
    if not range_header:
        headers["Content-Length"] = str(size)
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers)
        return StreamingResponse(
            _file_iter(path, 0, size - 1),
            status_code=200,
            headers=headers,
            media_type=headers["Content-Type"],
        )

    match = RANGE_RE.match(range_header.strip())
    if not match:
        raise HTTPException(status_code=400, detail="Unsupported Range header.")

    start_raw, end_raw = match.groups()
    if start_raw == "" and end_raw == "":
        raise HTTPException(status_code=400, detail="Unsupported Range header.")

    if start_raw == "":
        # Suffix range: last N bytes.
        length = min(int(end_raw), size)
        start, end = size - length, size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
        end = min(end, size - 1)

    if start >= size or start > end:
        raise HTTPException(
            status_code=416,
            detail="Requested range not satisfiable.",
            headers={"Content-Range": f"bytes */{size}"},
        )

    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    if request.method == "HEAD":
        return Response(status_code=206, headers=headers)
    return StreamingResponse(
        _file_iter(path, start, end),
        status_code=206,
        headers=headers,
        media_type=headers["Content-Type"],
    )


def _file_iter(path: Path, start: int, end: int):
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            data = handle.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def _base_url(request: Request) -> str:
    """Absolute prefix so URLs work when mounted under a proxy path."""
    return str(request.base_url).rstrip("/")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("STEM_HOST", "127.0.0.1")
    port = int(os.environ.get("STEM_PORT", "8000"))
    reload_enabled = os.environ.get("STEM_RELOAD", "").lower() in {"1", "true", "yes"}
    uvicorn.run("app:app", host=host, port=port, reload=reload_enabled, log_level="info")
