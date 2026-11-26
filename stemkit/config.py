"""Configuration: separation modes, model filenames and on-disk layout."""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent

# Model weights are cached here so a re-run never re-downloads a checkpoint.
MODEL_DIR = Path(os.environ.get("STEM_MODEL_DIR", ROOT / "models")).expanduser().resolve()

# Every job gets its own folder: work/<job_id>/{input,stems}
WORK_DIR = Path(os.environ.get("STEM_WORK_DIR", ROOT / "work")).expanduser().resolve()

# Uploads above this are rejected before touching the disk-heavy path.
MAX_UPLOAD_BYTES = int(os.environ.get("STEM_MAX_UPLOAD_MB", "500")) * 1024 * 1024

# Jobs older than this are swept away (with their audio) by the janitor.
JOB_TTL_HOURS = float(os.environ.get("STEM_JOB_TTL_HOURS", "12"))

# Force the inference device: "auto" (default) picks MPS/CUDA when present,
# "cpu" never touches the GPU. Set STEM_DEVICE=cpu to work around GPU hangs.
STEM_DEVICE = os.environ.get("STEM_DEVICE", "auto").lower()

# Only one separation runs at a time: these models are RAM hungry and
# PyTorch is already using every core it is given.
MAX_CONCURRENT_JOBS = 1

ACCEPTED_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".aiff",
    ".aif",
    ".wma",
    ".alac",
    ".mp4",
    ".webm",
    ".mkv",
}

# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
# `audio-separator` itself only takes a --model_filename; the 2stem/6stem split
# is the vocabulary of this app, and every mode maps onto one checkpoint plus
# the stem labels that checkpoint actually produces.

MODES: dict[str, dict] = {
    "2stem": {
        "id": "2stem",
        "name": "2 Stem",
        "tagline": "Vocals + Instrumental",
        "description": (
            "BS-RoFormer (SDR 12.98), the strongest vocal/instrumental model available. "
            "Best choice for karaoke, remixes and vocal edits."
        ),
        "model": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "model_label": "BS-RoFormer ep317",
        "stems": ["Vocals", "Instrumental"],
        "bit_depth": 24,
        "default": True,
    },
    "6stem": {
        "id": "6stem",
        "name": "6 Stem",
        "tagline": "Vocals · Drums · Bass · Guitar · Piano · Other",
        "description": (
            "Hybrid Transformer Demucs 6-source. Full band split for remixing, "
            "sampling and stem mastering."
        ),
        "model": "htdemucs_6s.yaml",
        "model_label": "HTDemucs 6-source",
        "stems": ["Vocals", "Drums", "Bass", "Guitar", "Piano", "Other"],
        "bit_depth": 24,
        "default": False,
    },
}

DEFAULT_MODE = next(m for m, cfg in MODES.items() if cfg["default"])


def get_mode(mode: str) -> dict:
    """Return a mode config, raising a clear error for an unknown mode."""
    try:
        return MODES[mode]
    except KeyError:
        valid = ", ".join(sorted(MODES))
        raise ValueError(f"Unknown mode {mode!r}. Choose one of: {valid}") from None


def public_modes() -> list[dict]:
    """Mode metadata safe to serialise to the browser."""
    return [
        {
            "id": cfg["id"],
            "name": cfg["name"],
            "tagline": cfg["tagline"],
            "description": cfg["description"],
            "model": cfg["model"],
            "model_label": cfg["model_label"],
            "stems": cfg["stems"],
            "bit_depth": cfg["bit_depth"],
            "default": cfg["default"],
        }
        for cfg in MODES.values()
    ]



# --------------------------------------------------------------------------- #
# Quality profiles (speed vs. separation quality)
# --------------------------------------------------------------------------- #
# `audio-separator` exposes one config dict per architecture. Only the dict for
# the architecture a mode actually uses changes anything:
#   * 2stem  -> BS-RoFormer -> `mdxc_params` (overlap = prediction windows per
#               chunk: 8 is the model default, 4 halves the inference passes)
#   * 6stem  -> HTDemucs    -> `demucs_params` (shifts = averaged predictions,
#               overlap = window overlap)
# `mdx_params` / `vr_params` are passed through for the MDX and VR families so
# the presets stay correct if a mode ever points at those architectures.

QUALITIES: dict[str, dict] = {
    "studio": {
        "id": "studio",
        "name": "Studio",
        "tagline": "Reference separation",
        "description": (
            "Full overlap, single-chunk batches, all Demucs shifts. Slowest and "
            "cleanest — use it for the take you keep."
        ),
        "batch_size": 1,
        "mdx_params": {"hop_length": 1024, "segment_size": 256, "overlap": 0.25, "batch_size": 1},
        "vr_params": {"batch_size": 1, "window_size": 512},
        "mdxc_params": {"segment_size": 256, "batch_size": 1, "overlap": 8},
        "demucs_params": {"segment_size": "Default", "shifts": 2, "overlap": 0.25},
        "default": True,
    },
    "fast": {
        "id": "fast",
        "name": "Fast",
        "tagline": "Rehearsal speed",
        "description": (
            "Batched chunks on the GPU and fewer overlapping windows. Rougher "
            "edges on bleed and reverb tails — best for practising the part."
        ),
        "batch_size": 4,
        "mdx_params": {"hop_length": 1024, "segment_size": 256, "overlap": 0.25, "batch_size": 4},
        "vr_params": {"batch_size": 4, "window_size": 512},
        "mdxc_params": {"segment_size": 256, "batch_size": 4, "overlap": 4},
        "demucs_params": {"segment_size": "Default", "shifts": 1, "overlap": 0.1},
        "default": False,
    },
}

DEFAULT_QUALITY = next(item for item, cfg in QUALITIES.items() if cfg["default"])


def get_quality(quality: str) -> dict:
    """Return a quality profile, raising a clear error for an unknown one."""
    try:
        return QUALITIES[quality]
    except KeyError:
        valid = ", ".join(sorted(QUALITIES))
        raise ValueError(f"Unknown quality {quality!r}. Choose one of: {valid}") from None


def public_qualities() -> list[dict]:
    return [
        {
            "id": cfg["id"],
            "name": cfg["name"],
            "tagline": cfg["tagline"],
            "description": cfg["description"],
            "batch_size": cfg["batch_size"],
            "default": cfg["default"],
        }
        for cfg in QUALITIES.values()
    ]

# --------------------------------------------------------------------------- #
# Stem presentation
# --------------------------------------------------------------------------- #

STEM_SLUGS = {
    "vocals": "vocals",
    "instrumental": "instrumental",
    "drums": "drums",
    "bass": "bass",
    "guitar": "guitar",
    "piano": "piano",
    "other": "other",
}

# Accent colours used by the waveform cards in the UI.
STEM_COLORS = {
    "vocals": "#6ee7ff",
    "instrumental": "#a78bfa",
    "drums": "#f472b6",
    "bass": "#fbbf24",
    "guitar": "#34d399",
    "piano": "#f87171",
    "other": "#94a3b8",
}


# Model outputs are not always spelled the way we present them; map the common
# aliases onto the canonical stem names so a stem is always labelled by what it
# actually is (Vocals, Guitar, ...) rather than by the model's file naming.
STEM_ALIASES = {
    "no-vocals": "instrumental",
    "accompaniment": "instrumental",
    "accompaniment-track": "instrumental",
    "music": "instrumental",
    "no-drums": "instrumental",
    "no-bass": "instrumental",
    "no-other": "instrumental",
    "vocals-instrumental": "instrumental",
    "electric-guitar": "guitar",
    "rhythm-guitar": "guitar",
    "lead-guitar": "guitar",
    "acoustic-guitar": "guitar",
    "keys": "piano",
    "synth": "piano",
    "strings": "other",
    "percussion": "drums",
}


def canonical_stem_name(raw_name: str, mode: str) -> str | None:
    """Resolve a model's stem label onto the mode's canonical stem name.

    Returns None when the output is not one of the mode's declared stems, so
    unexpected extras keep their model-provided name instead of being renamed.
    """
    slug = slugify_stem(raw_name)
    slug = STEM_ALIASES.get(slug, slug)
    for wanted in get_mode(mode)["stems"]:
        if slugify_stem(wanted) == slug:
            return wanted
    return None


def slugify_stem(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "stem"
