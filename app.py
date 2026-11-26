"""STEM — studio-quality audio stem extractor.

FastAPI service + drag-and-drop web UI around `audio-separator`.
    python app.py            # http://127.0.0.1:8000
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from stemkit import __version__, audio
from stemkit.config import (
    ACCEPTED_EXTENSIONS,
    DEFAULT_MODE,
    MAX_UPLOAD_BYTES,
    MODEL_DIR,
    public_modes,
)
from stemkit.jobs import manager, safe_filename
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
        "STEM %s ready · models in %s · uploads limited to %d MB",
        __version__,
        MODEL_DIR,
        MAX_UPLOAD_BYTES // (1024 * 1024),
    )
    try:
        yield
    finally:
        manager.stop()
        drop_cache()


app = FastAPI(title="STEM — Audio Stem Extractor", version=__version__, lifespan=lifespan)
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
    return {"jobs": [job.snapshot(base) for job in manager.list(limit=max(1, min(limit, 100)))]}


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
