"""Audio plumbing: probing, lossless 24-bit decoding and waveform peaks.

`audio-separator` writes its WAV output using the *input file's* subtype
(`common_separator.final_process` reads `input_subtype` / `input_bit_depth`).
So the reliable way to get true 24-bit stems is to hand it a 24-bit input:
we decode every upload to PCM_24 WAV first, then verify the stems afterwards
and re-write them as 24-bit if anything else slipped through.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

FFMPEG = os_ffmpeg = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

TARGET_SAMPLE_RATE = 44100
TARGET_SUBTYPE = "PCM_24"


class AudioError(RuntimeError):
    """Raised when an input cannot be decoded or an output cannot be written."""


def _run(cmd: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise AudioError(f"Command timed out after {timeout}s: {cmd[0]}") from exc


def have_ffmpeg() -> bool:
    return FFMPEG is not None


def require_ffmpeg() -> None:
    if FFMPEG is None:
        raise AudioError(
            "ffmpeg was not found on PATH. It is required to read MP3/M4A/FLAC and to "
            "produce 24-bit WAV. Install it with `brew install ffmpeg` (macOS) or "
            "`sudo apt install ffmpeg` (Debian/Ubuntu)."
        )


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


def probe(path: str | Path) -> dict:
    """Return {duration, sample_rate, channels, codec, bit_depth} for a file."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise AudioError("The file is empty or missing.")

    if FFPROBE is not None:
        proc = _run(
            [
                FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,channels,codec_name,bits_per_raw_sample,bits_per_sample"
                ":format=duration,format_name",
                "-of",
                "json",
                str(path),
            ],
            timeout=60,
        )
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout or "{}")
                stream = (data.get("streams") or [{}])[0]
                fmt = data.get("format") or {}
                duration = float(fmt.get("duration") or 0.0)
                bits = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0
                info = {
                    "duration": duration,
                    "sample_rate": int(stream.get("sample_rate") or 0),
                    "channels": int(stream.get("channels") or 0),
                    "codec": stream.get("codec_name") or "",
                    "bit_depth": int(bits) if str(bits).isdigit() else 0,
                    "container": fmt.get("format_name") or "",
                }
                if info["duration"] > 0 and info["sample_rate"] > 0:
                    return info
            except (ValueError, TypeError, IndexError, json.JSONDecodeError):
                pass

    # Fallback: soundfile understands WAV/FLAC/OGG natively.
    try:
        import soundfile as sf

        with sf.SoundFile(str(path)) as handle:
            frames = len(handle)
            rate = handle.samplerate
            return {
                "duration": frames / rate if rate else 0.0,
                "sample_rate": rate,
                "channels": handle.channels,
                "codec": (handle.subtype or "").lower(),
                "bit_depth": _subtype_bits(handle.subtype),
                "container": handle.format or "",
            }
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller
        raise AudioError(f"Could not read audio metadata: {exc}") from exc


def _subtype_bits(subtype: str | None) -> int:
    if not subtype:
        return 0
    for bits in (8, 16, 24, 32, 64):
        if str(bits) in subtype:
            return bits
    return 0


def bits_of(subtype: str | None) -> int:
    """Public: bit depth implied by a soundfile subtype such as 'PCM_24'."""
    return _subtype_bits(subtype)


# --------------------------------------------------------------------------- #
# Decoding to 24-bit
# --------------------------------------------------------------------------- #


def decode_to_wav24(src: str | Path, dest: str | Path) -> Path:
    """Decode any supported input into an interleaved 24-bit PCM WAV.

    No resampling is done unless the file is outside [8k, 192k] Hz or has more
    than 8 channels, in which case ffmpeg normalises it to 44.1 kHz stereo.
    """
    require_ffmpeg()
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    info = probe(src)
    cmd = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-c:a",
        "pcm_s24le",
        "-vn",
    ]
    rate = info.get("sample_rate") or 0
    channels = info.get("channels") or 0
    if rate < 8000 or rate > 192000 or channels > 8 or channels == 0:
        cmd += ["-ar", str(TARGET_SAMPLE_RATE), "-ac", "2"]
    cmd.append(str(dest))

    proc = _run(cmd)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size <= 44:
        stderr = (proc.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else "unknown ffmpeg error"
        raise AudioError(f"ffmpeg could not decode this file: {detail}")
    return dest


def ensure_wav24(path: str | Path) -> bool:
    """Guarantee a WAV file is 24-bit PCM. Returns True if it had to be re-written."""
    import soundfile as sf

    path = Path(path)
    with sf.SoundFile(str(path)) as handle:
        subtype = handle.subtype
        if subtype == TARGET_SUBTYPE:
            return False
        data = handle.read(dtype="float32", always_2d=True)
        rate = handle.samplerate

    tmp = path.with_suffix(".rewrite.wav")
    sf.write(str(tmp), data, rate, subtype=TARGET_SUBTYPE, format="WAV")
    tmp.replace(path)
    return True


def subtype_of(path: str | Path) -> str:
    import soundfile as sf

    with sf.SoundFile(str(path)) as handle:
        return handle.subtype or ""


def mixdown(
    sources: list[tuple[str | Path, float]],
    out_path: str | Path,
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
    channels: int = 2,
    title: str = "",
) -> Path:
    """Sum stems with a per-stem linear gain into one 24-bit PCM WAV.

    `amix` runs with `normalize=0`, so the bounce is a true summing bus: faders
    at unity reproduce the original mix, exactly like a DAW bounce.
    """
    require_ffmpeg()
    if not sources:
        raise AudioError("There is nothing to mix down.")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error"]
    for path, _gain in sources:
        cmd += ["-i", str(path)]

    chains: list[str] = []
    for index, (_path, gain) in enumerate(sources):
        gain = max(0.0, min(4.0, float(gain)))
        if abs(gain - 1.0) < 1e-4:
            chains.append(f"[{index}:a]anull[a{index}]")
        else:
            chains.append(f"[{index}:a]volume={gain:.4f}[a{index}]")

    if len(sources) == 1:
        graph = chains[0].replace("[a0]", "[mix]")
    else:
        joined = "".join(f"[a{index}]" for index in range(len(sources)))
        chains.append(
            f"{joined}amix=inputs={len(sources)}:duration=longest:normalize=0"
            ":dropout_transition=0[mix]"
        )
        graph = ";".join(chains)

    cmd += [
        "-filter_complex", graph,
        "-map", "[mix]",
        "-c:a", "pcm_s24le",
        "-ar", str(int(sample_rate)),
        "-ac", str(int(channels)),
    ]
    if title:
        cmd += ["-metadata", f"title={title}", "-metadata", "comment=HRC Studio Stem mixdown"]
    cmd.append(str(out_path))

    proc = _run(cmd, timeout=1800)
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        detail = (proc.stderr or proc.stdout or "ffmpeg error").strip()[:400]
        raise AudioError(f"Mixdown failed: {detail}")
    return out_path


# --------------------------------------------------------------------------- #
# Waveform peaks for the UI
# --------------------------------------------------------------------------- #


def peaks(path: str | Path, buckets: int = 480) -> list[float]:
    """Return `buckets` normalised absolute peaks for drawing a waveform."""
    import numpy as np
    import soundfile as sf

    path = Path(path)
    with sf.SoundFile(str(path)) as handle:
        frames = len(handle)
        if frames <= 0:
            return [0.0] * buckets
        # Read at most ~4M frames: plenty for a 480px waveform, tiny on memory.
        step = max(1, frames // 4_000_000)
        handle.seek(0)
        data = handle.read(frames=frames, dtype="float32", always_2d=True)[::step]
        if handle.channels > 1:
            data = data.mean(axis=1)

    if data.size == 0:
        return [0.0] * buckets
    data = np.abs(data)
    edges = np.linspace(0, data.size, buckets + 1, dtype=np.int64)
    out: list[float] = []
    for start, end in zip(edges[:-1], edges[1:]):
        if end <= start:
            out.append(0.0)
            continue
        out.append(float(data[start:end].max()))
    peak = max(out) or 1.0
    return [min(1.0, value / peak) for value in out]


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #


def human_size(num_bytes: int | float) -> str:
    if not num_bytes:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = min(int(math.log(num_bytes, 1024)), len(units) - 1)
    value = num_bytes / (1024**index)
    return f"{value:.1f} {units[index]}" if index else f"{int(value)} {units[index]}"


def human_duration(seconds: float | int | None) -> str:
    if not seconds or seconds <= 0:
        return "0:00"
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def db_from_peak(peak: float | None) -> str:
    if not peak or peak <= 0:
        return "-inf dB"
    return f"{20 * math.log10(min(1.0, peak)):.1f} dB"
