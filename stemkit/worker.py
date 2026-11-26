"""Run a single separation job in a dedicated process.

The parent app spawns `python -m stemkit.worker <job_id>` for every job. The
worker owns the model and the GPU for the lifetime of one job, reports progress
by writing `job.json` (the parent watches that file), and exits when finished.

Why a process: torch/MPS can deadlock inside a synchronous Metal copy (observed
as `istft -> -[MTLCommandBuffer waitUntilCompleted]` never returning, with 0%
CPU). A hung thread cannot be cancelled or recovered in-process — but a hung
*process* can be killed, and the parent can retry the job in a fresh GPU
context. One job per process also means every job starts with a clean Metal
allocator instead of inheriting fragmentation from earlier runs.
"""

from __future__ import annotations

import logging
import os
import sys

from . import jobs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m stemkit.worker <job_id>", file=sys.stderr)
        return 2

    job_id = argv[0]
    # The worker runs with no console: log to the job's worker.log (the parent
    # redirects stdout/stderr there) and make sure INFO records flow — the
    # stage/chunk handler depends on them.
    logging.basicConfig(
        level=os.environ.get("STEM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Load the job from disk, but do not start the queue worker or the janitor:
    # this process executes exactly one job and exits.
    jobs.manager.start(run_worker=False, run_janitor=False)
    job = jobs.manager.get(job_id)
    if job is None:
        print(f"job {job_id} not found", file=sys.stderr)
        return 1

    jobs.manager.execute_job(job)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
