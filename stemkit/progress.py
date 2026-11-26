"""Live progress for `audio-separator`, which exposes no callbacks.

The MDXC (RoFormer) and Demucs code paths both drive a `tqdm` bar per chunk.
We swap that class for a subclass that forwards each tick to whichever sink is
registered for the current job, which turns an opaque blocking call into real
per-chunk progress. A logging handler picks up the stage transitions
(model download, inference, stem writing).

Everything here is process-global on purpose: jobs are serialised by the job
manager, so exactly one run owns the sink at a time.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Callable

ProgressSink = Callable[[float], None]

_lock = threading.RLock()
_sink: ProgressSink | None = None
_cancel_check: Callable[[], None] | None = None
_installed = False

# When a long file is separated in segments, the per-segment tqdm runs 0 -> 1
# once per segment. The chunk context maps that onto the whole file so the UI
# bar moves forwards across every segment instead of restarting each time.
_chunk_index = 1
_chunk_count = 1


class CancelledError(RuntimeError):
    """Raised inside the inference loop when a job is cancelled."""


_CHUNK_RE = re.compile(r"processing chunk (\d+)/(\d+)")


class StageHandler(logging.Handler):
    """Turns `audio-separator`'s own log lines into coarse stage updates."""

    def __init__(self, on_stage: Callable[..., None]):
        super().__init__(level=logging.INFO)
        self._on_stage = on_stage
        self._last = ""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            message = record.getMessage().lower()
        except Exception:  # noqa: BLE001 - a broken log line must never kill a job
            return

        chunk = _CHUNK_RE.search(message)
        if chunk:
            index, count = int(chunk.group(1)), int(chunk.group(2))
            set_chunk_context(index, count)
            overall = (index - 1) / count if count > 1 else 0.0
            try:
                self._on_stage("Separating", 0.14 + 0.66 * overall)
            except Exception:  # noqa: BLE001
                pass
            return

        stage = None
        if "downloading" in message or "model file" in message:
            stage = "Downloading model"
        elif "loading model" in message or "initialisation complete" in message:
            stage = "Loading model"
        elif "separating" in message or "running model inference" in message or "demix" in message:
            stage = "Separating"
        elif "saving" in message and "stem" in message:
            stage = "Writing stems"
        if stage and stage != self._last:
            self._last = stage
            try:
                self._on_stage(stage)
            except Exception:  # noqa: BLE001
                pass


def _make_progress_tqdm(base_class):
    class ProgressTqdm(base_class):  # type: ignore[misc, valid-type]
        def update(self, n=1):  # noqa: D102
            checker = _cancel_check
            if checker is not None:
                checker()  # raises CancelledError to abort between chunks
            result = super().update(n)
            total = getattr(self, "total", None)
            sink = _sink
            if total and sink is not None:
                sink(max(0.0, min(1.0, float(self.n) / float(total))))
            return result

    return ProgressTqdm


def install() -> None:
    """Patch the tqdm classes used by every architecture. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        try:
            import tqdm as tqdm_module
            from tqdm import tqdm as base_tqdm
        except Exception:  # noqa: BLE001
            return

        patched = _make_progress_tqdm(base_tqdm)

        # Modules that did `from tqdm import tqdm` hold their own reference.
        for module_name in (
            "audio_separator.separator.architectures.mdxc_separator",
            "audio_separator.separator.architectures.mdx_separator",
            "audio_separator.separator.architectures.vr_separator",
            "audio_separator.separator.architectures.demucs_separator",
            "audio_separator.separator.separator",
        ):
            try:
                module = __import__(module_name, fromlist=["tqdm"])
                if hasattr(module, "tqdm"):
                    module.tqdm = patched
            except Exception:  # noqa: BLE001 - module layout may change
                continue

        # Demucs calls `tqdm.tqdm(...)` through the module object.
        try:
            class _Module:
                tqdm = patched
                __getattr__ = staticmethod(lambda name: getattr(tqdm_module, name))  # type: ignore[assignment]

            import audio_separator.separator.uvr_lib_v5.demucs.apply as demucs_apply

            demucs_apply.tqdm = _Module()
        except Exception:  # noqa: BLE001
            pass

        _installed = True


def set_chunk_context(index: int, count: int) -> None:
    """Tell the sink which segment is running (1-based) out of how many."""
    global _chunk_index, _chunk_count
    with _lock:
        _chunk_index = max(1, index)
        _chunk_count = max(1, count)


def reset_chunk_context() -> None:
    set_chunk_context(1, 1)


def set_sink(sink: ProgressSink | None) -> None:
    global _sink
    with _lock:
        _sink = sink


def clear_sink() -> None:
    set_sink(None)


def set_cancel_check(check: Callable[[], None] | None) -> None:
    """Register a callable that raises to abort the run between chunks."""
    global _cancel_check
    with _lock:
        _cancel_check = check


def attach_stage_handler(on_stage: Callable[[str], None]) -> logging.Handler:
    handler = StageHandler(on_stage)
    logging.getLogger("audio_separator").addHandler(handler)
    return handler


def detach_stage_handler(handler: logging.Handler) -> None:
    logging.getLogger("audio_separator").removeHandler(handler)


__all__ = [
    "CancelledError",
    "attach_stage_handler",
    "clear_sink",
    "detach_stage_handler",
    "install",
    "reset_chunk_context",
    "set_cancel_check",
    "set_chunk_context",
    "set_sink",
]
