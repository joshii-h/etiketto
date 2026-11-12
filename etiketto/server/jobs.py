"""Print jobs of the server: a queue per printer, one job at a time each.

A job is accepted (and checked) when it is asked for and printed when its
turn comes, by a worker thread of its printer (backend and address). It
can be canceled while it waits, and between two labels while it prints:
the label being printed is finished. Finished jobs are kept for a while,
so their state can still be asked for.

Messages are made when the job is asked for, in the language of that
request: a job keeps what happened (the exception), not a text.
"""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

QUEUED, PRINTING, DONE, FAILED, CANCELED = "queued", "printing", "done", "failed", "canceled"
WAITING = "waiting"  # its turn came, but another print holds the label file
FINISHED = frozenset({DONE, FAILED, CANCELED})
MAX_PENDING = 100  # jobs waiting or printing, on all printers together
MAX_PER_OWNER = 20  # of them for one token
# what waiting jobs may hold in memory (labels, images, data rows), about
MAX_PENDING_BYTES = 256 * 1024 * 1024
MAX_QUEUES = 64  # printers with a worker at once
IDLE_SECONDS = 60.0  # a worker without jobs ends after this long
MAX_KEPT = 1000  # finished jobs kept to be asked for
KEEP_SECONDS = 24 * 3600


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Failure:
    code: str
    message: Callable[[], str]  # translated when called


@dataclass
class PrintJob:
    """A print request on its way. `run` prints (in the worker thread) and
    keeps `printed` up to date; it returns a remark (or None) and raises
    to fail the job."""

    printer: str  # backend id
    host: str
    label: str  # name in the labels folder, or "" for a label sent as content
    total: int  # labels to print
    # dropped when the job finishes: it holds the request (label, rows)
    run: Callable[[PrintJob], Callable[[], str | None] | None] | None
    id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    state: str = QUEUED
    printed: int = 0
    rows_printed: int = 0
    failure: Failure | None = None
    remark: Callable[[], str | None] | None = None
    warnings: list = field(default_factory=list)  # the check's warnings (RenderWarning)
    owner: str = ""  # the name of the token that asked for it
    cost: int = 0  # bytes the request holds while the job waits, about
    key: str = ""  # the printer's address, normalised: one queue per printer
    printer_name: str = ""  # its name in printers.toml, if any
    created: str = field(default_factory=_now)
    started: str = ""
    finished: str = ""
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "printer": self.printer,
            "printer_name": self.printer_name or None,
            "host": self.host,
            "owner": self.owner or None,
            "label": self.label or None,
            "printed": self.printed,
            "total": self.total,
            "rows_printed": self.rows_printed,
            "cancel_requested": self.stop.is_set(),
            "error": (
                {"code": self.failure.code, "message": self.failure.message()}
                if self.failure
                else None
            ),
            "remark": self.remark() if self.remark else None,
            "created": self.created,
            "started": self.started or None,
            "finished": self.finished or None,
        }

    def finish(self, state: str) -> None:
        self.state = state
        self.finished = _now()
        self.finished_at = time.monotonic()
        self.run = None
        self.done.set()
        code = self.failure.code if self.failure else ""
        log.info(
            "job %s by %s %s: %d of %d %s",
            self.id,
            self.owner,
            state,
            self.printed,
            self.total,
            code,
        )


class QueueFull(Exception):
    """No room for the job: too many jobs, too much data waiting, or too
    many printers busy at once."""


class JobQueue:
    """The jobs of all printers: a queue and a worker thread for each
    printer, made when its first job comes."""

    def __init__(self, fail: Callable[[PrintJob, Exception], None]):
        self._fail = fail  # turns the exception of a failed job into its Failure
        self._lock = threading.Lock()
        self._jobs: dict[str, PrintJob] = {}
        self._queues: dict[tuple[str, str], queue.Queue[PrintJob | None]] = {}
        self._workers: list[threading.Thread] = []
        self._closed = False

    def submit(self, job: PrintJob) -> PrintJob:
        with self._lock:
            self._admit(job)
            self._forget_old()
            key = (job.printer, job.key or job.host)
            if key not in self._queues:
                self._start(key)
            self._jobs[job.id] = job
            self._queues[key].put(job)
        return job

    def _admit(self, job: PrintJob) -> None:
        pending = [j for j in self._jobs.values() if j.state not in FINISHED]
        own = sum(1 for j in pending if j.owner == job.owner)
        cost = sum(j.cost for j in pending) + job.cost
        if self._closed or len(pending) >= MAX_PENDING or own >= MAX_PER_OWNER:
            raise QueueFull
        if cost > MAX_PENDING_BYTES:
            raise QueueFull

    def _start(self, key: tuple[str, str]) -> None:
        """A queue and a worker for a printer that has none (any more)."""
        self._workers = [w for w in self._workers if w.is_alive()]
        if len(self._queues) >= MAX_QUEUES:
            raise QueueFull
        waiting: queue.Queue[PrintJob | None] = queue.Queue()
        worker = threading.Thread(
            target=self._work, args=(key, waiting), daemon=True, name="print-job"
        )
        try:
            worker.start()
        except RuntimeError:  # no thread to be had (TasksMax, memory)
            raise QueueFull from None
        self._queues[key] = waiting
        self._workers.append(worker)

    def get(self, job_id: str) -> PrintJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job: PrintJob) -> None:
        """A waiting job is canceled at once; a printing one stops after
        the label being printed."""
        with self._lock:
            job.stop.set()
            if job.state == QUEUED:
                job.finish(CANCELED)

    def close(self, timeout: float = 30.0) -> None:
        """Cancel what waits, let what prints stop after its label (and
        save its counter), end the workers."""
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                job.stop.set()
                if job.state == QUEUED:
                    job.finish(CANCELED)
            for waiting in self._queues.values():
                waiting.put(None)
        deadline = time.monotonic() + timeout
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))

    def _pending(self) -> int:
        return sum(1 for job in self._jobs.values() if job.state not in FINISHED)

    def _forget_old(self) -> None:
        now = time.monotonic()
        finished = sorted(
            (job for job in self._jobs.values() if job.state in FINISHED),
            key=lambda job: job.finished_at,
        )
        excess = len(finished) - MAX_KEPT
        for n, job in enumerate(finished):
            if n < excess or now - job.finished_at > KEEP_SECONDS:
                del self._jobs[job.id]

    def _work(self, key: tuple[str, str], waiting: queue.Queue[PrintJob | None]) -> None:
        """Print the printer's jobs one after the other; without a job for
        IDLE_SECONDS the worker ends and its queue goes (a server asked for
        many addresses keeps no thread for each)."""
        while True:
            try:
                job = waiting.get(timeout=IDLE_SECONDS)
            except queue.Empty:
                with self._lock:
                    if waiting.empty():
                        if self._queues.get(key) is waiting:
                            del self._queues[key]
                        return
                continue
            if job is None:
                return
            with self._lock:
                if job.state != QUEUED:  # canceled while it waited
                    continue
                job.state = PRINTING
                job.started = _now()
            self._run(job)

    def _run(self, job: PrintJob) -> None:
        try:
            assert job.run is not None
            job.remark = job.run(job)
        except Exception as e:  # noqa: BLE001 - every problem fails the job, not the worker
            self._fail(job, e)
            job.finish(FAILED)
            return
        job.finish(CANCELED if job.stop.is_set() and job.printed < job.total else DONE)
