"""
The batch worker: `python -m app.worker`. Pulls documents off the Redis queue and
runs them through the pipeline. Run as many as you have cores to spare - each
document already fans its field reads across FIELD_OCR_WORKERS threads, so a couple
of workers saturate a typical machine.

    python -m app.worker            # run until stopped
    python -m app.worker --burst    # drain the queue, then exit

RQ's default worker forks a child per job, which Windows cannot do, so there it
falls back to SimpleWorker (runs each job in the worker process itself). A job
that hangs is then only cut off by a timer thread, so prefer the default worker
(Linux, the Docker image) where it matters.

The scheduler is on because retry intervals are implemented by it: without it a
retried job with a delay would never be requeued.
"""

import argparse
import os

from rq import SimpleWorker, Worker

from app.job_queue import get_queue
from app.runtime import configure_logging, configure_tesseract


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--burst", action="store_true", help="exit once the queue is empty")
    args = parser.parse_args()

    configure_tesseract()
    configure_logging()

    queue = get_queue()
    worker_class = Worker if hasattr(os, "fork") else SimpleWorker
    worker_class([queue], connection=queue.connection).work(burst=args.burst, with_scheduler=True)


if __name__ == "__main__":
    main()
