"""Fetch audio from a link (YouTube and anything else yt-dlp understands).

The download is deliberately dumb: grab the best audio stream, then hand it to
`audio.decode_to_wav24` so the separator still receives the same 24-bit PCM WAV
it gets from an upload. No post-processing tricks, no transcoding chain beyond
one ffmpeg pass.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlsplit

from . import audio, progress

logger = logging.getLogger("stemkit.youtube")

MAX_URL_MINUTES = float(os.environ.get("STEM_MAX_URL_MINUTES", "20"))
MAX_URL_BYTES = int(os.environ.get("STEM_MAX_URL_MB", "500")) * 1024 * 1024

_UNSAFE = re.compile(r"[^\w\s.\-()\[\]]")
_PRIVATE_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost"}


class FetchError(RuntimeError):
    """A link could not be turned into audio."""


class FetchCancelled(RuntimeError):
    """The download was cancelled by the user."""


def available() -> bool:
    """True when yt-dlp can be imported or found on PATH."""
    try:
        import yt_dlp  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - optional dependency
        return shutil.which("yt-dlp") is not None


def validate_url(url: str) -> str:
    """Reject anything that is not a public http(s) link."""
    value = (url or "").strip()
    if not value:
        raise ValueError("Paste a YouTube link first.")
    if len(value) > 2048:
        raise ValueError("That link is too long.")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Only http(s) links are supported.")
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        raise ValueError("That link does not have a public hostname.")
    if (
        host in _PRIVATE_HOSTS
        or host.startswith(("127.", "10.", "192.168.", "169.254.", "0."))
        or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)
        or host.endswith(".local")
    ):
        raise ValueError("That address is on this machine's network — paste a public link.")
    return value


def safe_title(title: str, fallback: str = "youtube audio") -> str:
    cleaned = _UNSAFE.sub(" ", title or "").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:140] or fallback).strip()


def download(
    url: str,
    dest_dir: str | Path,
    *,
    on_progress: "callable | None" = None,
    is_cancelled: "callable | None" = None,
) -> dict:
    """Download `url` and return {path, title, duration, uploader, webpage_url}."""
    try:
        import yt_dlp
    except Exception as exc:  # noqa: BLE001 - optional dependency
        raise FetchError(
            "yt-dlp is not installed, so links cannot be fetched. "
            "Run `pip install yt-dlp` and restart the app."
        ) from exc

    if not audio.have_ffmpeg():
        raise FetchError("ffmpeg is required to convert the downloaded audio.")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = dest_dir / ".download"
    if raw_dir.exists():
        shutil.rmtree(raw_dir, ignore_errors=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    def guard() -> None:
        if is_cancelled is not None and is_cancelled():
            raise FetchCancelled("Cancelled by user")

    def hook(status: dict) -> None:
        guard()
        if on_progress is None:
            return
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            done = status.get("downloaded_bytes") or 0
            on_progress(min(0.98, done / total) if total else 0.4)
        elif status.get("status") == "finished":
            on_progress(0.99)

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(raw_dir / "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "consoletitle": False,
        "progress_hooks": [hook],
        "retries": 2,
        "socket_timeout": 30,
        "max_filesize": MAX_URL_BYTES,
        "continuedl": True,
        "overwrites": True,
    }

    guard()
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except FetchCancelled:
        shutil.rmtree(raw_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI verbatim
        shutil.rmtree(raw_dir, ignore_errors=True)
        raise FetchError(_human_error(exc)) from exc

    if isinstance(info, dict) and info.get("_type") == "playlist":  # defensive
        entries = info.get("entries") or []
        info = entries[0] if entries else {}

    duration = float(info.get("duration") or 0)
    if duration and duration > MAX_URL_MINUTES * 60:
        shutil.rmtree(raw_dir, ignore_errors=True)
        raise FetchError(
            f"That link is {int(duration // 60)}:{int(duration % 60):02d} long — "
            f"the limit is {int(MAX_URL_MINUTES)}:00. Try a shorter clip."
        )
    if duration and duration < 1.5:
        shutil.rmtree(raw_dir, ignore_errors=True)
        raise FetchError("That audio is too short to separate.")

    downloads = [item for item in raw_dir.iterdir() if item.is_file()]
    if not downloads:
        raise FetchError("The download finished but produced no audio file.")
    raw_path = max(downloads, key=lambda item: item.stat().st_size)
    if raw_path.stat().st_size > MAX_URL_BYTES:
        shutil.rmtree(raw_dir, ignore_errors=True)
        raise FetchError(f"That download is larger than the {MAX_URL_BYTES // (1024 * 1024)} MB limit.")

    guard()
    title = safe_title(info.get("title") or raw_path.stem)
    target = dest_dir / f"{title}.wav"
    index = 2
    while target.exists():
        target = dest_dir / f"{title} ({index}).wav"
        index += 1

    try:
        audio.decode_to_wav24(raw_path, target)
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"Could not decode the downloaded audio: {exc}") from exc
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)

    logger.info("fetched %s → %s (%.1fs)", url, target.name, duration)
    return {
        "path": target,
        "title": info.get("title") or title,
        "duration": duration,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "webpage_url": info.get("webpage_url") or url,
        "bytes": target.stat().st_size,
    }


def _human_error(exc: BaseException) -> str:
    """Turn yt-dlp's wall of text into something worth showing a user."""
    text = str(exc).strip().splitlines()
    message = next((line for line in text if line.strip()), "Download failed.")
    message = re.sub(r"^ERROR:\s*", "", message.strip())
    lowered = message.lower()
    if "private video" in lowered or "sign in" in lowered:
        return "That video is private — try a public link."
    if "age" in lowered and "confirm" in lowered:
        return "That video is age-restricted, so it cannot be fetched without signing in."
    if "unavailable" in lowered or "not available" in lowered:
        return "That video is unavailable (removed, blocked, or region-locked)."
    if "unsupported url" in lowered:
        return "That site is not supported by yt-dlp."
    if "timed out" in lowered or "timeout" in lowered:
        return "The download timed out — check the connection and try again."
    return message[:300]
