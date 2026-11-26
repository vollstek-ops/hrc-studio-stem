"""The separation runner: one upload in, N lossless 24-bit stems out."""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

# Must be set before torch is imported anywhere in the process: on MPS a few
# ops (complex STFT helpers) have no Metal kernel, and without this flag the run
# dies instead of quietly falling back to CPU for that one op.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

from . import audio, progress
from .config import (
    DEFAULT_QUALITY,
    MODEL_DIR,
    WORK_DIR,
    canonical_stem_name,
    get_mode,
    get_quality,
    slugify_stem,
)

logger = logging.getLogger("stemkit.separator")

_STEM_IN_NAME = re.compile(r"_\(([^)]+)\)_")

# Model instances are expensive to build (they hold weights in RAM), so each
# mode/quality pair keeps one warm instance between jobs. Studio and Fast use
# different chunking, so they cannot share an instance.
_separator_cache: dict[tuple[str, str], object] = {}
_cache_lock = threading.Lock()

StageFn = Callable[[str, float | None], None]
CancelFn = Callable[[], bool]

# Reported once per process so the UI can show which accelerator is live.
_device_report: dict | None = None


def device_report(refresh: bool = False) -> dict:
    """What PyTorch will actually use: 'mps', 'cuda' or 'cpu'."""
    global _device_report
    if _device_report is not None and not refresh:
        return _device_report

    report = {
        "device": "cpu",
        "torch": None,
        "torch_version": None,
        "mps_built": False,
        "mps_available": False,
        "cuda_available": False,
        "autocast": False,
        "processor": platform.uname().processor,
        "machine": platform.uname().machine,
        "fallback_env": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", ""),
    }
    try:
        import torch

        report["torch"] = torch.__version__
        report["mps_built"] = bool(torch.backends.mps.is_built())
        report["mps_available"] = bool(torch.backends.mps.is_available())
        report["cuda_available"] = bool(torch.cuda.is_available())
        if report["cuda_available"]:
            report["device"] = "cuda"
            report["torch_version"] = torch.version.cuda
        elif report["mps_available"]:
            report["device"] = "mps"
            # Prove the device really works instead of trusting the flag.
            try:
                probe = torch.ones(8, device="mps") * 2
                report["mps_selftest"] = float(probe.sum().item()) == 16.0
            except Exception as exc:  # noqa: BLE001 - a broken MPS is worth knowing
                report["mps_selftest"] = False
                report["device"] = "cpu"
                report["error"] = str(exc)[:200]
        report["autocast"] = report["device"] in {"mps", "cuda"}
    except Exception as exc:  # noqa: BLE001 - torch is optional at import time
        report["error"] = str(exc)[:200]

    _device_report = report
    return report


def _log_tail(exc: BaseException, limit: int = 3) -> str:
    lines = [str(exc).strip()] if str(exc).strip() else []
    cause = exc.__cause__ or exc.__context__
    while cause is not None and len(lines) < limit:
        text = str(cause).strip()
        if text and text not in lines:
            lines.append(text)
        cause = cause.__cause__ or cause.__context__
    return " — ".join(lines) or exc.__class__.__name__


def _get_separator(mode: str, quality: str = DEFAULT_QUALITY, extra_models_dir: Path | None = None):
    """Build (or reuse) an audio-separator Separator for a mode/quality pair."""
    import audio_separator
    from audio_separator.separator import Separator

    cfg = get_mode(mode)
    profile = get_quality(quality)
    cache_key = (mode, profile["id"])
    with _cache_lock:
        cached = _separator_cache.get(cache_key)
        if cached is not None:
            return cached

        ModelDir = extra_models_dir or MODEL_DIR
        ModelDir.mkdir(parents=True, exist_ok=True)

        device = device_report()
        separator_kwargs = dict(
            log_level=logging.INFO,
            model_file_dir=str(ModelDir),
            output_format="WAV",
            output_dir=str(WORK_DIR),
            normalization_threshold=1.0,  # never attenuate: preserve mix levels
            amplification_threshold=0.0,
            sample_rate=44100,
            use_soundfile=True,  # keeps memory flat on long tracks
            # Explicit chunking: these are the knobs that decide speed.
            mdx_params=dict(profile["mdx_params"]),
            vr_params=dict(profile["vr_params"]),
            mdxc_params=dict(profile["mdxc_params"]),
            demucs_params=dict(profile["demucs_params"]),
        )
        # Autocast (fp16 matmuls) is only meaningful on an accelerator.
        separator_kwargs["use_autocast"] = bool(device["autocast"])

        separator = Separator(**separator_kwargs)
        chosen = getattr(getattr(separator, "torch_device", None), "type", "cpu")

        # audio-separator only turns MPS on when platform.uname().processor is
        # literally "arm"; on any other Apple Silicon setup it silently runs on
        # CPU. Verify, and take the GPU when the hardware clearly has one.
        if device["device"] == "mps" and chosen != "mps":
            try:
                import torch

                separator.torch_device = torch.device("mps")
                separator.torch_device_mps = separator.torch_device
                logger.warning(
                    "audio-separator picked %s although MPS is available; forcing MPS.", chosen
                )
                chosen = "mps"
            except Exception:  # noqa: BLE001 - staying on CPU is still correct
                logger.exception("Could not switch torch to MPS; continuing on CPU.")

        logger.info(
            "audio-separator %s | mode=%s | quality=%s | device=%s | model=%s",
            getattr(audio_separator, "__version__", "?"),
            mode,
            profile["id"],
            chosen,
            cfg["model"],
        )

        separator.load_model(model_filename=cfg["model"])
        logger.info(
            "inference ready · device=%s · autocast=%s · batch=%s · roformer_overlap=%s · demucs_shifts=%s",
            chosen,
            separator_kwargs["use_autocast"],
            profile["batch_size"],
            profile["mdxc_params"].get("overlap"),
            profile["demucs_params"].get("shifts"),
        )
        _separator_cache[cache_key] = separator
        return separator


def drop_cache(mode: str | None = None) -> None:
    """Release warm models (used when memory pressure matters)."""
    with _cache_lock:
        if mode is None:
            _separator_cache.clear()
            return
        for key in [key for key in _separator_cache if key[0] == mode]:
            _separator_cache.pop(key, None)


def warm_cache(mode: str, quality: str = DEFAULT_QUALITY) -> None:
    """Load a model up front so the first job does not pay for it."""
    _get_separator(mode, quality)


def _point_at(separator, out_dir: Path) -> None:
    """Point a Separator (and its loaded architecture instance) at a folder."""
    out_dir.mkdir(parents=True, exist_ok=True)
    separator.output_dir = str(out_dir)
    instance = getattr(separator, "model_instance", None)
    if instance is not None:
        instance.output_dir = str(out_dir)


def _unique_dest(directory: Path, slug: str) -> Path:
    dest = directory / f"{slug}.wav"
    index = 2
    while dest.exists():
        dest = directory / f"{slug}-{index}.wav"
        index += 1
    return dest


def separate_file(
    input_path: str | Path,
    mode: str,
    job_dir: str | Path | None = None,
    *,
    quality: str = DEFAULT_QUALITY,
    on_stage: StageFn | None = None,
    is_cancelled: CancelFn | None = None,
    stem_labels: list[str] | None = None,
) -> dict:
    """Separate one file into stems.

    Returns a manifest dict:
        {mode, model, stems: [{name, slug, path, bytes, duration, bit_depth, ...}],
         duration, elapsed}

    `quality` selects the chunking profile (`studio` or `fast`).
    `on_stage(name, fraction)` is called on stage transitions and on every
    inference progress tick. `is_cancelled()` is polled between chunks.
    """
    cfg = get_mode(mode)
    profile = get_quality(quality)
    input_path = Path(input_path)
    if not input_path.exists():
        raise audio.AudioError(f"Input file not found: {input_path}")

    job_dir = Path(job_dir) if job_dir else WORK_DIR / "adhoc"
    stems_dir = job_dir / "stems"
    if stems_dir.exists():
        shutil.rmtree(stems_dir, ignore_errors=True)
    stems_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()

    def stage(name: str, fraction: float | None = None) -> None:
        if on_stage is not None:
            try:
                on_stage(name, fraction)
            except Exception:  # noqa: BLE001
                pass

    def check_cancel() -> None:
        if is_cancelled is not None and is_cancelled():
            raise progress.CancelledError("Cancelled by user")

    # -- 1. Decode to 24-bit so the model writes PCM_24 stems ----------------
    stage("Decoding to 24-bit", 0.04)
    check_cancel()
    source_info = audio.probe(input_path)
    staged_input = job_dir / "input.s24.wav"
    audio.decode_to_wav24(input_path, staged_input)

    # -- 2. Load model (cached after the first job) --------------------------
    stage("Loading model", 0.10)
    check_cancel()
    separator = _get_separator(mode, profile["id"])

    # -- 3. Inference --------------------------------------------------------
    stage("Separating", 0.14)
    progress.install()
    progress.set_sink(lambda fraction: stage("Separating", 0.14 + 0.66 * fraction))
    progress.set_cancel_check(check_cancel)
    handler = progress.attach_stage_handler(lambda name: stage(name, None))
    try:
        # audio-separator delegates writing to the architecture instance, which
        # holds its own output_dir, so both have to be pointed at this job.
        _point_at(separator, stems_dir)
        raw_outputs = separator.separate(str(staged_input))
    except progress.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise RuntimeError(
            f"Separation failed for mode {mode!r} with model {cfg['model']}: {_log_tail(exc)}"
        ) from exc
    finally:
        progress.clear_sink()
        progress.set_cancel_check(None)
        progress.detach_stage_handler(handler)

    progress_pct = 0.80
    stage("Writing stems", progress_pct)
    check_cancel()

    raw_outputs = raw_outputs if isinstance(raw_outputs, (list, tuple)) else [raw_outputs]
    wanted = stem_labels or cfg["stems"]

    rename_targets = [w.lower() for w in wanted]
    stems: list[dict] = []
    for raw in raw_outputs:
        raw_path = Path(raw)
        if not raw_path.is_absolute():
            raw_path = stems_dir / raw_path.name
        if not raw_path.exists():
            # Fall back to the directory the model actually reported.
            alternative = stems_dir / Path(raw).name
            if alternative.exists():
                raw_path = alternative
            elif (WORK_DIR / Path(raw).name).exists():
                raw_path = WORK_DIR / Path(raw).name
        if not raw_path.exists():
            logger.warning("Stem missing after separation: %s", raw)
            continue

        match = _STEM_IN_NAME.search(raw_path.stem)
        name = match.group(1) if match else raw_path.stem.split("_")[-1]
        # Label the stem by what it is, not by how the model spelled it:
        # "no_vocals", "Accompaniment" and "Instrumental" all become the mode's
        # canonical name, and the file on disk is named after it.
        name = canonical_stem_name(name, mode) or name.strip()
        slug = slugify_stem(name)

        dest = _unique_dest(stems_dir, name)
        if dest != raw_path:
            shutil.move(str(raw_path), str(dest))

        # Safety net: the model inherits the input subtype, but verify anyway.
        try:
            audio.ensure_wav24(dest)
        except Exception as exc:  # noqa: BLE001 - keep the stem, log loudly
            logger.warning("Could not verify 24-bit subtype for %s: %s", dest.name, exc)

        info = audio.probe(dest)
        peak_list = audio.peaks(dest, buckets=480)
        stems.append(
            {
                "name": name,
                "slug": slug,
                "filename": dest.name,
                "path": str(dest),
                "bytes": dest.stat().st_size,
                "duration": round(float(info.get("duration") or 0), 3),
                "sample_rate": info.get("sample_rate") or 0,
                "channels": info.get("channels") or 0,
                "bit_depth": audio.bits_of(audio.subtype_of(dest)) or cfg["bit_depth"],
                "subtype": audio.subtype_of(dest),
                "peaks": peak_list,
                "is_requested": slug in rename_targets,
            }
        )

    if not stems:
        raise RuntimeError(
            "audio-separator reported success but produced no readable stems. "
            "Check that ffmpeg is installed and the input actually contains audio."
        )

    # Sort to match the requested stem order (Vocals first for 2stem, etc).
    order = {slugify_stem(name): index for index, name in enumerate(wanted)}
    stems.sort(key=lambda item: order.get(item["slug"], 99))

    # The staging copy was only needed as model input; stems are the deliverable.
    try:
        staged_input.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - non-fatal
        pass

    stage("Finalising", 0.96)
    elapsed = time.monotonic() - started
    stage("Complete", 1.0)

    return {
        "mode": mode,
        "mode_name": cfg["name"],
        "quality": profile["id"],
        "quality_name": profile["name"],
        "model": cfg["model"],
        "model_label": cfg["model_label"],
        "stems": stems,
        "duration": round(float(source_info.get("duration") or 0), 3),
        "sample_rate": source_info.get("sample_rate") or 0,
        "elapsed": round(elapsed, 2),
        "bit_depth": cfg["bit_depth"],
        "lossless": True,
    }
