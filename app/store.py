"""
Storage for jobs and the leads they produce.

THE DATABASE SEAM (a brief requirement: "in memory for now, but structure the
code so swapping in a database later is easy").

The technique is dependency inversion. `LeadStore` below is an abstract base
class describing WHAT storage must do; `InMemoryLeadStore` is one
implementation. Routes and workers depend only on the abstract type -- they
call `store.append_lead(...)` and have no idea whether that writes to a dict or
issues an INSERT.

To add Postgres later you write `PostgresLeadStore(LeadStore)`, implement the
same handful of methods, and add one line to `build_store()`. No route, no
worker and no test changes. If instead we had sprinkled a module-level
`JOBS = {}` through the routes, that swap would mean touching every file that
reads it -- which is precisely the mistake this file exists to prevent.

Design choices that make the swap realistic rather than theoretical:
  * Methods are ASYNC even though the in-memory one never awaits anything.
    A real database driver (asyncpg, databases) is async; if the interface
    were sync, every caller would need rewriting to `await` later. Paying
    that cost now makes the future change additive.
  * The interface is COARSE-GRAINED ("append this lead") rather than exposing
    the underlying dict. Callers cannot reach behind the abstraction, so the
    implementation is genuinely free to change.
  * Jobs carry their own lead list, so a job's results are one lookup -- the
    natural shape for both a dict and a `WHERE job_id = ...` query.
"""

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.schema import Lead


def _now() -> str:
    """UTC ISO-8601 timestamp. UTC everywhere avoids timezone bugs later."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    """
    One bulk upload and its progress.

    This is what the frontend polls. It is deliberately cheap to serialise:
    the client asks "how far along are you?" every second or so, and the
    answer must not be expensive to produce.
    """

    id: str
    total: int                          # how many files were accepted
    # Who owns this job. Ownership is enforced on every read (see get_job in
    # main.py): without it, guessing a job id would expose someone else's
    # leads. A field on the row is also exactly how this works once the store
    # is a database -- it becomes `WHERE user_id = ?`.
    user_id: str = "local"
    status: str = "queued"              # queued | running | done | failed
    leads: list[Lead] = field(default_factory=list)
    error: Optional[str] = None         # set only when status == "failed"
    created_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None

    @property
    def processed(self) -> int:
        return len(self.leads)

    def summary(self) -> dict:
        """Progress without the payload -- used for cheap polling."""
        return {
            "job_id": self.id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "succeeded": sum(1 for lead in self.leads if lead.status == "ok"),
            "failed": sum(1 for lead in self.leads if lead.status != "ok"),
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }

    def to_dict(self) -> dict:
        """Full state including every lead extracted so far."""
        return {**self.summary(), "leads": [lead.to_dict() for lead in self.leads]}

    def owned_by(self, user_id: str) -> bool:
        return self.user_id == user_id


class LeadStore(ABC):
    """
    The storage interface. Implement this to back the app with anything.

    Every method is async so that a real database implementation is a drop-in.
    """

    @abstractmethod
    async def create_job(self, total: int, user_id: str = "local") -> Job:
        """Register a new job and return it."""

    @abstractmethod
    async def get_job(self, job_id: str) -> Optional[Job]:
        """Fetch one job, or None if the id is unknown."""

    @abstractmethod
    async def set_status(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> None:
        """Move a job between queued/running/done/failed."""

    @abstractmethod
    async def append_lead(self, job_id: str, lead: Lead) -> None:
        """
        Record one extracted lead against a job.

        Called once per card as it completes, NOT once at the end -- that is
        what lets the UI fill in progressively, and what stops a crash at
        card 900 from discarding the first 899.
        """

    @abstractmethod
    async def list_jobs(self, user_id: str) -> list[Job]:
        """That user's jobs, newest first."""

    @abstractmethod
    async def all_leads(self, user_id: str) -> list[Lead]:
        """Every lead that user owns (used by the 'export everything' path)."""


class InMemoryLeadStore(LeadStore):
    """
    Dict-backed store. Fine for a single process; loses everything on restart.

    The asyncio.Lock is not paranoia. The worker appends leads while the
    frontend polls, both on the same event loop. Individual dict operations are
    atomic, but a READ-MODIFY-WRITE spanning an await is not -- the lock keeps
    those sequences consistent and, more importantly, documents that this class
    is shared mutable state.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create_job(self, total: int, user_id: str = "local") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], total=total, user_id=user_id)
        async with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        return job

    def _prune_locked(self) -> None:
        """
        Drop the oldest jobs once we exceed the retention cap.

        Must be called with the lock already held -- hence the name. Python
        dicts preserve insertion order, so the oldest entries are simply the
        first keys. Running jobs are never pruned: evicting a job while its
        worker is still appending leads would make the UI's poll 404 halfway
        through a batch.
        """
        limit = settings.max_jobs_retained
        if len(self._jobs) <= limit:
            return
        removable = [
            job_id for job_id, job in self._jobs.items()
            if job.status in {"done", "failed"}
        ]
        for job_id in removable[: len(self._jobs) - limit]:
            del self._jobs[job_id]

    async def get_job(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def set_status(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            if error is not None:
                job.error = error
            if status in {"done", "failed"}:
                job.finished_at = _now()

    async def append_lead(self, job_id: str, lead: Lead) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.leads.append(lead)

    async def list_jobs(self, user_id: str) -> list[Job]:
        async with self._lock:
            return sorted(
                (j for j in self._jobs.values() if j.user_id == user_id),
                key=lambda j: j.created_at,
                reverse=True,
            )

    async def all_leads(self, user_id: str) -> list[Lead]:
        async with self._lock:
            return [
                lead
                for job in self._jobs.values()
                if job.user_id == user_id
                for lead in job.leads
            ]


def build_store() -> LeadStore:
    """
    Factory: the single place that decides which implementation is used.

    Swapping in Postgres later means adding one branch here, e.g.
        if settings.store_backend == "postgres":
            return PostgresLeadStore(settings.database_url)
    and nothing else in the codebase changes.
    """
    return InMemoryLeadStore()


# The process-wide instance. Imported by routes and the worker.
store: LeadStore = build_store()
