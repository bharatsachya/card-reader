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
import os
import pathlib
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from app.config import settings
from app.schema import LEAD_FIELDS, Lead


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

    # --- Counters, when the store can produce them without loading leads ---
    #
    # WHY THESE EXIST. `summary()` needs three numbers: how many cards are
    # done, how many succeeded, how many failed. The in-memory store derives
    # them by walking `self.leads`, which is free because the list is already
    # in RAM.
    #
    # A database store cannot afford that. GET /api/jobs lists every job the
    # user has ever run, and deriving the counters the same way would mean
    # SELECTing every lead of every job -- thousands of rows, every one of
    # them discarded after being counted -- to render a sidebar showing
    # "14 cards, 12 ok". SQL counts that with one indexed aggregate and
    # returns three integers per job.
    #
    # So the store MAY supply them. None means "not supplied, derive from the
    # leads list", which keeps the in-memory path byte-for-byte unchanged and
    # means no caller has to know which store it is talking to.
    processed_count: Optional[int] = None
    succeeded_count: Optional[int] = None
    failed_count: Optional[int] = None

    @property
    def processed(self) -> int:
        if self.processed_count is not None:
            return self.processed_count
        return len(self.leads)

    @property
    def succeeded(self) -> int:
        if self.succeeded_count is not None:
            return self.succeeded_count
        return sum(1 for lead in self.leads if lead.status == "ok")

    @property
    def failed(self) -> int:
        if self.failed_count is not None:
            return self.failed_count
        return sum(1 for lead in self.leads if lead.status != "ok")

    def summary(self) -> dict:
        """Progress without the payload -- used for cheap polling."""
        return {
            "job_id": self.id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
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
        """
        That user's jobs, newest first.

        CONTRACT: the returned Jobs are SUMMARIES. `job.leads` may be empty
        even for a finished job -- a database store fills the counters instead,
        because loading every lead of every job to render a history list is
        work thrown away. Callers that need the rows must ask get_job().
        Stated here rather than left to be discovered, because the in-memory
        store happens to over-deliver and would hide a caller that got it
        wrong until the day the backend changed.
        """

    @abstractmethod
    async def all_leads(self, user_id: str) -> list[Lead]:
        """Every lead that user owns (used by the 'export everything' path)."""

    @abstractmethod
    async def get_lead(self, lead_id: str, user_id: str) -> Optional[Lead]:
        """
        One lead, but ONLY if `user_id` owns the job it belongs to.

        THE OWNERSHIP ARGUMENT IS MANDATORY, AND THAT IS THE WHOLE POINT.
        The obvious signature is get_lead(lead_id) with the caller checking
        ownership afterwards -- and that is exactly how authorisation bugs
        happen, because "afterwards" is a step a future caller can forget, and
        forgetting it is invisible in review. Making the owner part of the
        lookup means there is no way to express "fetch this lead" without also
        saying whose it is: a missing check becomes a missing ARGUMENT, which
        fails loudly, rather than a data leak that fails silently.

        Returns None both for "no such lead" and "not yours" -- the caller
        cannot tell them apart, so neither can an attacker probing ids.
        """

    @abstractmethod
    async def expire_images(self, older_than: str) -> int:
        """
        Clear image_sha256 on every lead created before `older_than` (ISO-8601).

        The LEAD is never deleted. Extracted text is a few hundred bytes and is
        the thing the user came for; the image is the bulky part and the part
        that is personal data. Retention deletes the photograph and keeps the
        record -- so history stays complete and the disk stays bounded.
        """

    @abstractmethod
    async def referenced_image_hashes(self) -> set[str]:
        """
        Every image digest still referenced by a lead. The sweep's keep-list.

        Returns a set rather than a list because the caller tests membership
        once per file on disk; a list would make the sweep quadratic.
        """

    # --- Lifecycle. Concrete no-ops, so a store with nothing to set up (the
    # --- in-memory one) inherits them and stays a three-line class.

    async def initialize(self) -> None:
        """
        Prepare the backing store. Called once at application startup.

        Separate from __init__ because creating tables is I/O and __init__
        cannot await. Calling it is OPTIONAL -- every method below also
        ensures readiness on its own -- but calling it at startup means a bad
        DB_PATH crashes the container at boot, where the platform's health
        probe catches it, instead of surfacing as a 500 on the first upload
        twenty minutes later.
        """

    async def reclaim_stale_jobs(self) -> int:
        """
        Fail any job left mid-flight by a previous process. Returns the count.

        THIS EXISTS BECAUSE PERSISTENCE CREATES A BUG THAT MEMORY DID NOT.
        A job's worker is an asyncio task inside one process. When that process
        dies mid-batch the task dies with it -- but with a database the row
        survives, still saying "running", and nothing will ever move it. The
        browser polls it forever and the UI shows a progress bar that can never
        finish. The in-memory store never had this problem because the crash
        took the job row with it.
        """
        return 0


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
                # Same stamping as the SQLite store, so both backends leave a
                # Lead in an identical state and the image route behaves the
                # same under either. A divergence here would only surface
                # after switching backends -- the worst time to find it.
                lead.id = lead.id or uuid.uuid4().hex[:16]
                job.leads.append(lead)

    async def get_lead(self, lead_id: str, user_id: str) -> Optional[Lead]:
        async with self._lock:
            for job in self._jobs.values():
                # Ownership is tested FIRST, so a lead inside someone else's
                # job is never even compared against -- the same shape as the
                # SQL implementation's JOIN.
                if job.user_id != user_id:
                    continue
                for lead in job.leads:
                    if lead.id == lead_id:
                        return lead
        return None

    async def expire_images(self, older_than: str) -> int:
        # This store keeps no per-lead timestamp and does not survive a
        # restart, so nothing in it can be older than the current process.
        # Zero is the honest answer rather than a fabricated one.
        return 0

    async def referenced_image_hashes(self) -> set[str]:
        async with self._lock:
            return {
                lead.image_sha256
                for job in self._jobs.values()
                for lead in job.leads
                if lead.image_sha256
            }

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


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

# WHY THE SCHEMA LOOKS LIKE THIS
#
# TEXT PRIMARY KEYS, NOT AUTOINCREMENT INTEGERS. Job ids are already random
# hex, and lead ids are too. An integer key would make /api/leads/5/image
# trivially enumerable -- walk 1, 2, 3 and you have harvested every business
# card image on the server. The API returns 404 rather than 403 for someone
# else's job precisely so ids cannot be probed; handing out sequential ids
# would give that away for free. Random ids make enumeration useless even if
# an ownership check is ever missed, which is defence in depth rather than a
# single check standing between a stranger and other people's data.
#
# TIMESTAMPS AS ISO-8601 TEXT. SQLite has no date type; the choices are TEXT,
# REAL (Julian day) or INTEGER (epoch). ISO-8601 UTC text sorts correctly as a
# string -- "2026-09-17T10:00:00+00:00" < "2026-09-17T11:00:00+00:00"
# lexicographically as well as chronologically -- so ORDER BY works with no
# conversion, it is readable when you open the file with the sqlite3 CLI, and
# it is the exact string the API already returns. No parse/format round trip
# anywhere.
#
# ON DELETE CASCADE. Deleting a job must take its leads with it. Without the
# cascade, deleting a job leaves orphaned lead rows that belong to nobody --
# they would still be returned by all_leads(), which joins through jobs, but
# they would be invisible to every other query and would accumulate forever.
# Note that SQLite only ENFORCES this if `PRAGMA foreign_keys = ON` is set on
# the connection -- see _connect() below, and note it is off by default.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT    PRIMARY KEY,
    user_id     TEXT    NOT NULL,
    total       INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    error       TEXT,
    created_at  TEXT    NOT NULL,
    finished_at TEXT
);

-- The history query is "this user's jobs, newest first", which is exactly
-- (user_id, created_at DESC). With both columns in the index in that order,
-- SQLite seeks straight to the user's block and walks it backwards -- no
-- table scan, and no sort step at all, because the index is already in the
-- requested order. An index on user_id alone would still need to sort every
-- matching row afterwards.
CREATE INDEX IF NOT EXISTS idx_jobs_user_created
    ON jobs (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS leads (
    id              TEXT    PRIMARY KEY,
    job_id          TEXT    NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    first_name      TEXT,
    last_name       TEXT,
    title           TEXT,
    company         TEXT,
    location        TEXT,
    phone           TEXT,
    email           TEXT,
    source_filename TEXT    NOT NULL DEFAULT '',
    -- sha256 of the retained normalised JPEG; also its filename on disk.
    -- Nullable, and NOT a foreign key to anything: the image is expected to
    -- outlive nothing and to be deleted by retention while the lead stays.
    image_sha256    TEXT,
    status          TEXT    NOT NULL,
    error           TEXT,
    raw_output      TEXT,
    created_at      TEXT    NOT NULL
);

-- Serves both "give me this job's leads" and the counting aggregate that
-- list_jobs() uses. Without it, rendering the history sidebar is a full scan
-- of the leads table per job.
CREATE INDEX IF NOT EXISTS idx_leads_job ON leads (job_id);
"""

# The lead columns, in one place, so the INSERT, the SELECT and the row->Lead
# mapping can never drift apart. Same discipline as LEAD_FIELDS in schema.py.
_LEAD_COLUMNS: tuple[str, ...] = ("id",) + LEAD_FIELDS + (
    "source_filename",
    "image_sha256",
    "status",
    "error",
    "raw_output",
)


class SqliteLeadStore(LeadStore):
    """
    SQLite-backed store. Survives restarts, and is safe across processes.

    WHY SQLITE AND NOT POSTGRES. The workload is one write per card -- and a
    card takes ~170 seconds. That is roughly one INSERT every three minutes,
    against a read of a few rows per second from polling. A network database
    would add a container, a connection pool, a password to manage and a
    second thing that can be down, to serve a write rate a phone could handle.
    SQLite is a file: no daemon, no port, no credentials, and `cp leads.db`
    is a complete backup. If the write rate ever justifies Postgres, the seam
    in build_store() is where it lands, and nothing else changes.

    WHY A CONNECTION PER OPERATION, NOT ONE SHARED CONNECTION. An aiosqlite
    connection owns a background thread and is bound to the event loop that
    created it, so a long-lived one shared at module scope breaks the moment a
    second event loop exists -- which is exactly what pytest does, one loop per
    test. Opening per call costs microseconds against an already-created file
    and sidesteps the entire class of problem. At one write per three minutes
    that cost is not worth optimising away.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ready = False
        # Guards first-run schema creation. Without it, N coroutines arriving
        # at once on a cold start would each try to create the tables; the
        # IF NOT EXISTS clauses make that harmless but the lock makes it
        # deliberate rather than lucky.
        self._ready_lock = asyncio.Lock()

    # --- connection handling -----------------------------------------------

    async def _ensure_ready(self) -> None:
        """Create the directory, the file and the tables. Runs once."""
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:      # another coroutine won the race while we waited
                return

            # The parent directory must exist before SQLite will create the
            # file; sqlite3 reports a missing directory as the famously
            # unhelpful "unable to open database file", identical to the
            # message for a permissions problem.
            parent = pathlib.Path(self._db_path).expanduser().resolve().parent
            os.makedirs(parent, exist_ok=True)

            async with aiosqlite.connect(self._db_path) as conn:
                # --- Pragmas that are stored IN THE FILE, so once is enough ---
                #
                # WAL (write-ahead logging) is the one that matters. In the
                # default rollback-journal mode a writer takes an exclusive
                # lock on the whole database, so every poll -- one per second,
                # per open tab -- blocks while a lead is being written, and
                # every write blocks behind the polls. In WAL mode writers
                # append to a side file and readers keep reading the main one,
                # so readers never block the writer and the writer never
                # blocks readers. That is precisely this app's access pattern:
                # constant small reads, occasional writes.
                await conn.execute("PRAGMA journal_mode = WAL")
                # FULL fsyncs on every commit; NORMAL fsyncs at WAL
                # checkpoints. NORMAL risks losing the last few commits if the
                # MACHINE loses power (an application crash is still safe --
                # the WAL is intact). For extracted business cards that is an
                # acceptable trade for not fsyncing on every row.
                await conn.execute("PRAGMA synchronous = NORMAL")
                await conn.executescript(_SCHEMA)
                await self._migrate(conn)
                await conn.commit()

            self._ready = True

    @staticmethod
    async def _migrate(conn: aiosqlite.Connection) -> None:
        """
        Add columns missing from a database created by an earlier version.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already
        exists -- including adding a column to it. Without this, anyone who
        ran the previous version gets a schema frozen at whatever shape it had
        then, and every query mentioning a new column fails with "no such
        column" on a database that looks perfectly healthy.

        Deliberately additive only. ALTER TABLE ADD COLUMN is the one schema
        change SQLite performs instantly and without rewriting the table, and
        it cannot lose data. Anything beyond it -- renaming, dropping,
        changing a type -- means a real migration tool, and earning that
        dependency needs a reason this project does not yet have.
        """
        async with conn.execute("PRAGMA table_info(leads)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for column, definition in (("image_sha256", "TEXT"),):
            if column not in existing:
                await conn.execute(
                    f"ALTER TABLE leads ADD COLUMN {column} {definition}"
                )

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """A ready connection with the per-connection pragmas applied."""
        await self._ensure_ready()
        async with aiosqlite.connect(self._db_path) as conn:
            # PER-CONNECTION, and both are easy to forget.
            #
            # foreign_keys is OFF by default in SQLite, for backwards
            # compatibility going back twenty years. Declaring REFERENCES in
            # the schema does nothing at all without this line -- the cascade
            # silently never fires and you find out when orphaned rows appear.
            await conn.execute("PRAGMA foreign_keys = ON")
            # When another connection holds the write lock, SQLite's default
            # is to fail INSTANTLY with "database is locked". busy_timeout
            # makes it retry for five seconds instead. This is what makes
            # `--workers N` viable: two processes writing a lead at the same
            # moment now queue for milliseconds rather than one of them losing
            # a card to an exception.
            await conn.execute("PRAGMA busy_timeout = 5000")
            # Rows addressable by column name rather than position, so adding
            # a column in workstream 2 cannot silently shift every index.
            conn.row_factory = aiosqlite.Row
            yield conn

    # --- row mapping --------------------------------------------------------

    @staticmethod
    def _row_to_lead(row: aiosqlite.Row) -> Lead:
        return Lead(**{column: row[column] for column in _LEAD_COLUMNS})

    @staticmethod
    def _row_to_job(row: aiosqlite.Row, *, with_counts: bool) -> Job:
        job = Job(
            id=row["id"],
            total=row["total"],
            user_id=row["user_id"],
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )
        if with_counts:
            job.processed_count = row["processed"]
            job.succeeded_count = row["succeeded"]
            job.failed_count = row["failed"]
        return job

    # --- LeadStore -----------------------------------------------------------

    async def initialize(self) -> None:
        await self._ensure_ready()

    async def create_job(self, total: int, user_id: str = "local") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], total=total, user_id=user_id)
        async with self._connect() as conn:
            await conn.execute(
                "INSERT INTO jobs (id, user_id, total, status, error, "
                "created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.user_id, job.total, job.status, job.error,
                 job.created_at, job.finished_at),
            )
            await conn.commit()
        return job

    async def get_job(self, job_id: str) -> Optional[Job]:
        async with self._connect() as conn:
            async with conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            job = self._row_to_job(row, with_counts=False)

            # ORDER BY rowid, not by a timestamp. Every SQLite table has an
            # implicit monotonically-increasing rowid, so insertion order is
            # recoverable exactly -- which matters because leads are appended
            # as cards finish and the table must match the order the user
            # watched them appear in. Two cards finishing inside the same
            # clock tick would make a created_at sort non-deterministic.
            async with conn.execute(
                "SELECT * FROM leads WHERE job_id = ? ORDER BY rowid", (job_id,)
            ) as cursor:
                job.leads = [self._row_to_lead(r) for r in await cursor.fetchall()]
        return job

    async def set_status(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> None:
        async with self._connect() as conn:
            # COALESCE(?, error) means "set it only if a value was passed",
            # matching the in-memory store's `if error is not None`. Passing
            # the parameter unconditionally would blank an existing error
            # every time the status changed.
            await conn.execute(
                "UPDATE jobs SET status = ?, error = COALESCE(?, error), "
                "finished_at = ? WHERE id = ?",
                (
                    status,
                    error,
                    _now() if status in {"done", "failed"} else None,
                    job_id,
                ),
            )
            await conn.commit()

    async def append_lead(self, job_id: str, lead: Lead) -> None:
        # Stamped onto the caller's object, not just into the row, so that
        # both stores leave the Lead in the same state afterwards and a caller
        # can use lead.id without a round trip. Assigning only when absent
        # keeps the method safe to re-run on an already-identified lead.
        lead.id = lead.id or uuid.uuid4().hex[:16]
        columns = ", ".join(("job_id", *_LEAD_COLUMNS, "created_at"))
        placeholders = ", ".join("?" * (len(_LEAD_COLUMNS) + 2))
        values = (
            job_id,
            *(getattr(lead, column) for column in _LEAD_COLUMNS),
            _now(),
        )
        async with self._connect() as conn:
            await conn.execute(
                f"INSERT INTO leads ({columns}) VALUES ({placeholders})", values
            )
            await conn.commit()

    async def list_jobs(self, user_id: str) -> list[Job]:
        # The counters come from ONE aggregate over the leads index rather
        # than from loading every lead of every job and counting in Python.
        # For a user with 40 jobs of 30 cards, that is three integers per job
        # instead of 1,200 fully-hydrated rows thrown away after being counted.
        query = """
            SELECT j.*,
                   COALESCE(c.processed, 0)  AS processed,
                   COALESCE(c.succeeded, 0)  AS succeeded,
                   COALESCE(c.failed, 0)     AS failed
            FROM jobs j
            LEFT JOIN (
                SELECT job_id,
                       COUNT(*)                                        AS processed,
                       SUM(CASE WHEN status  = 'ok' THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS failed
                FROM leads
                GROUP BY job_id
            ) c ON c.job_id = j.id
            WHERE j.user_id = ?
            ORDER BY j.created_at DESC
        """
        async with self._connect() as conn:
            async with conn.execute(query, (user_id,)) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_job(row, with_counts=True) for row in rows]

    async def all_leads(self, user_id: str) -> list[Lead]:
        # The JOIN is the authorisation. Ownership lives on the job, so leads
        # are reachable only through a job this user owns -- there is no query
        # path that returns a lead without proving the owner first.
        query = """
            SELECT leads.* FROM leads
            JOIN jobs ON jobs.id = leads.job_id
            WHERE jobs.user_id = ?
            ORDER BY jobs.created_at, leads.rowid
        """
        async with self._connect() as conn:
            async with conn.execute(query, (user_id,)) as cursor:
                return [self._row_to_lead(row) for row in await cursor.fetchall()]

    async def get_lead(self, lead_id: str, user_id: str) -> Optional[Lead]:
        # The JOIN *is* the authorisation: no row satisfies this query unless
        # the requesting user owns the job the lead hangs off. Both conditions
        # resolve in one round trip, so there is no window in which the lead
        # has been fetched but ownership has not yet been checked.
        query = """
            SELECT leads.* FROM leads
            JOIN jobs ON jobs.id = leads.job_id
            WHERE leads.id = ? AND jobs.user_id = ?
        """
        async with self._connect() as conn:
            async with conn.execute(query, (lead_id, user_id)) as cursor:
                row = await cursor.fetchone()
        return self._row_to_lead(row) if row else None

    async def expire_images(self, older_than: str) -> int:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "UPDATE leads SET image_sha256 = NULL "
                "WHERE image_sha256 IS NOT NULL AND created_at < ?",
                (older_than,),
            )
            await conn.commit()
            return cursor.rowcount or 0

    async def referenced_image_hashes(self) -> set[str]:
        async with self._connect() as conn:
            async with conn.execute(
                "SELECT DISTINCT image_sha256 FROM leads "
                "WHERE image_sha256 IS NOT NULL"
            ) as cursor:
                return {row[0] for row in await cursor.fetchall()}

    async def reclaim_stale_jobs(self) -> int:
        """Fail jobs whose worker died with a previous process. See the ABC."""
        async with self._connect() as conn:
            cursor = await conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? "
                "WHERE status IN ('queued', 'running')",
                (_now(), "interrupted by restart"),
            )
            await conn.commit()
            return cursor.rowcount or 0


def build_store() -> LeadStore:
    """
    Factory: the single place that decides which implementation is used.

    This is the whole payoff of the seam. Adding SQLite touched this function
    and nothing else -- no route, no worker, no frontend line changed, because
    none of them ever learned that storage was a dictionary.
    """
    backend = settings.store_backend
    if backend == "memory":
        return InMemoryLeadStore()
    if backend == "sqlite":
        return SqliteLeadStore(settings.db_path)
    # An unknown value is a typo in the environment, and guessing which one
    # they meant is how you end up silently running the wrong backend in
    # production. Fail at import, loudly, with the valid values listed.
    raise ValueError(
        f"unknown STORE_BACKEND {backend!r}; expected 'sqlite' or 'memory'"
    )


# The process-wide instance. Imported by routes and the worker.
store: LeadStore = build_store()
