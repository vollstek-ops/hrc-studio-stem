"""One-shot maintenance: free memory, delete rebuildable files, prune expired jobs.

Everything this module touches is a cache or a temporary by-product:
  * warm model instances (RAM, rebuilt on the next job)
  * cached stem ZIP archives and their .part files (rebuilt on download)
  * staging copies (`input.s24.wav`), rewritten WAVs (`.rewrite.wav`) and
    yt-dlp `.download` folders left behind by crashes
  * job folders past their TTL

Deliverables are never touched: stems, bounces and source audio of live jobs
stay exactly where they are, and folders of queued/running jobs are skipped.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from . import separator
from .config import JOB_TTL_HOURS, WORK_DIR
from .jobs import TERMINAL, manager

logger = logging.getLogger("stemkit.maintenance")

TEMP_PATTERNS = ("*.zip", "*.zip.part", "input.s24.wav", "*.rewrite.wav")
TEMP_DIRS = (".download",)


def clear_caches(protected_dirs: list[Path] | None = None, ttl_hours: float | None = None) -> dict:
    """Clear every cache the app owns and report what was freed."""
    protected = {Path(item).resolve() for item in (protected_dirs or [])}
    report = {
        "freed_bytes": 0,
        "removed_files": 0,
        "removed_dirs": 0,
        "models_unloaded": 0,
        "expired_jobs": [],
        "skipped_running": 0,
    }

    # 1. Warm models: the biggest RAM consumer after the OS.
    report["models_unloaded"] = separator.cache_stats()
    separator.drop_cache()

    # 2. Rebuildable files inside job folders (never inside a running one).
    root = WORK_DIR
    if root.exists():
        for job_dir in list(root.iterdir()):
            if not job_dir.is_dir():
                continue
            try:
                if job_dir.resolve() in protected:
                    report["skipped_running"] += 1
                    continue
                # A job can finish (or be swept by the janitor) while we walk
                # its folder; a vanished directory is a fine outcome, not an
                # error, so every step tolerates it.
                for pattern in TEMP_PATTERNS:
                    for path in job_dir.rglob(pattern):
                        try:
                            if path.is_file():
                                report["freed_bytes"] += path.stat().st_size
                                path.unlink()
                                report["removed_files"] += 1
                        except OSError:  # noqa: PERF203 - lost the race, fine
                            continue
                for name in TEMP_DIRS:
                    for path in job_dir.rglob(name):
                        if path.is_dir():
                            before = _dir_size(path)
                            shutil.rmtree(path, ignore_errors=True)
                            report["freed_bytes"] += before
                            report["removed_dirs"] += 1
            except OSError:  # noqa: BLE001 - the folder disappeared mid-sweep
                continue

    # 3. Jobs past their TTL (the janitor does this hourly; make it instant).
    #    sweep_now() owns the rule; here we only measure what it will free.
    cutoff = time.time() - (ttl_hours if ttl_hours is not None else JOB_TTL_HOURS) * 3600
    for job in manager.list(limit=500):
        if job.status in TERMINAL and (job.finished_at or job.created_at) < cutoff:
            if job.dir.resolve() in protected:
                report["skipped_running"] += 1
                continue
            report["freed_bytes"] += _dir_size(job.dir)
    report["expired_jobs"] = manager.sweep_now()
    if report["expired_jobs"]:
        report["removed_files"] += len(report["expired_jobs"])

    report["freed_kb"] = round(report["freed_bytes"] / 1024, 1)
    report["freed_mb"] = round(report["freed_bytes"] / 1048576, 1)
    logger.info(
        "cache clear: %s MB freed, %s files, %s models unloaded, %s expired jobs",
        report["freed_mb"],
        report["removed_files"],
        report["models_unloaded"],
        len(report["expired_jobs"]),
    )
    return report


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:  # noqa: PERF203 - a file vanishing mid-walk is fine
            continue
    return total


__all__ = ["clear_caches"]
