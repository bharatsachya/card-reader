"""
Central configuration, read from environment variables.

Why this file exists at all:
Nothing about *which* model we talk to should be baked into application logic.
Today this points at a local stub / Ollama; tomorrow it points at llama.cpp on
an EC2 box serving Qwen2.5-VL-3B via Ollama. That switch must be an env-var
change, not a code change. Every model-related knob lives here and here only.
"""

import os
import pathlib


def _load_dotenv() -> None:
    """
    Load KEY=VALUE lines from a .env file into the environment.

    Deliberately hand-rolled rather than pulling in python-dotenv: it is ten
    lines, it has no surprises, and a reader can see exactly what it does.

    REAL environment variables always win. A value already exported in the
    shell (or injected by systemd, Docker or an ECS task definition) is never
    overwritten by the file, so production config cannot be silently clobbered
    by a stray .env left on the box.
    """
    path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # setdefault, not assignment: the real environment takes precedence.
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


_load_dotenv()


def _env_str(name: str, default: str) -> str:
    """Read a string env var, falling back to a default."""
    value = os.getenv(name)
    # An env var set to an empty string ("MODEL_URL=") should behave as unset,
    # otherwise a stray line in a .env file silently breaks the app.
    return value if value else default


def _env_bool(name: str, default: bool) -> bool:
    """
    Read a boolean env var. Everything is a string in an environment, and
    `bool("false")` is True, which is a genuinely common production bug.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Read an int env var, ignoring values that aren't valid integers."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    """Application settings, resolved once at import time."""

    def __init__(self) -> None:
        # --- Model endpoint -------------------------------------------------
        # An OpenAI-compatible chat-completions URL. Ollama, llama.cpp's
        # llama-server, vLLM and OpenAI itself all speak this same shape, which
        # is exactly why we target it: one client works against all of them.
        self.model_url: str = _env_str(
            "MODEL_URL", "http://localhost:11434/v1/chat/completions"
        )
        # The model identifier the server expects in the JSON body.
        self.model_name: str = _env_str("MODEL_NAME", "qwen2.5vl:3b")
        # Some servers (vLLM, OpenAI) require an Authorization header; Ollama
        # and llama.cpp ignore it. Optional, so remote deploys need no code edit.
        self.model_api_key: str = _env_str("MODEL_API_KEY", "")
        # A CPU-hosted 3B VLM is slow: MEASURED at 136-172s per card on the
        # target box (m7i-flex.large, 2 vCPU, no GPU). 180s left almost no
        # headroom above the worst measured card, and a timeout mid-card
        # throws away every second already spent on it.
        self.model_timeout_seconds: int = _env_int("MODEL_TIMEOUT_SECONDS", 600)
        # How many times to try one card before giving up. Covers transient
        # blips (a model still loading, a reset socket) without turning a
        # genuinely-dead backend into an hours-long batch.
        self.model_max_attempts: int = _env_int("MODEL_MAX_ATTEMPTS", 3)
        # First backoff wait; each subsequent attempt doubles it.
        self.model_retry_base_seconds: float = float(
            _env_int("MODEL_RETRY_BASE_SECONDS", 1)
        )

        # --- Image preprocessing -------------------------------------------
        # Longest edge, in pixels, that we send to the model. See README for
        # why this number matters so much for a VLM.
        self.max_image_edge: int = _env_int("MAX_IMAGE_EDGE", 1024)

        # --- Post-processing ------------------------------------------------
        # Default region for phone numbers written without a country code.
        self.default_phone_region: str = _env_str("DEFAULT_PHONE_REGION", "IN")

        # --- Authentication (Clerk) -----------------------------------------
        # Verification needs only PUBLIC values: the issuer URL, from which the
        # JWKS endpoint is derived. The Clerk SECRET key is deliberately absent
        # from this codebase -- see app/auth.py.
        #
        # Auth turns itself OFF when no issuer is configured, so the project
        # still runs for someone with no Clerk account, and so `curl` against a
        # dev box needs no token. That is a convenience for development, and it
        # is why a real deployment must set CLERK_ISSUER.
        self.clerk_issuer: str = _env_str("CLERK_ISSUER", "").rstrip("/")
        # Shipped to the browser, which is what publishable keys are for.
        self.clerk_publishable_key: str = _env_str("CLERK_PUBLISHABLE_KEY", "")

        # AUTH_MODE: "clerk" verifies real session tokens; "local" runs every
        # request as a single fixed user.
        #
        # WHY AN EXPLICIT MODE RATHER THAN INFERRING FROM THE KEYS. Auth used
        # to switch itself off whenever CLERK_ISSUER was blank. That is a
        # helpful default and a dangerous one: a typo'd variable name, a
        # secret that failed to mount, an env file that did not load -- and a
        # production deployment silently serves every user's leads to every
        # visitor, with a completely healthy /health and nothing in the logs.
        # Security that disables itself when misconfigured is not a safe
        # default; it is a silent one.
        #
        # So the mode is stated, and stating "clerk" without the keys is a
        # hard startup failure (see _validate below) rather than a quiet
        # downgrade. Inference is kept only when AUTH_MODE is unset, so
        # existing setups and `curl localhost:8000` keep working.
        self.auth_mode: str = _env_str(
            "AUTH_MODE",
            "clerk" if (self.clerk_issuer and self.clerk_publishable_key) else "local",
        ).lower()

        self._validate()

    def _validate(self) -> None:
        """Refuse to start in a configuration that is quietly wrong."""
        if self.auth_mode not in {"clerk", "local"}:
            raise ValueError(
                f"unknown AUTH_MODE {self.auth_mode!r}; expected 'clerk' or 'local'"
            )
        if self.auth_mode == "clerk" and not (
            self.clerk_issuer and self.clerk_publishable_key
        ):
            raise ValueError(
                "AUTH_MODE=clerk requires both CLERK_ISSUER and "
                "CLERK_PUBLISHABLE_KEY. Refusing to start rather than falling "
                "back to unauthenticated access."
            )

        # --- Backpressure / resource limits ---------------------------------
        # These exist to make the server survive a hostile or careless upload.
        # Every one of them rejects work BEFORE memory is spent on it.

        # Largest single file we will accept, in bytes. Enforced while
        # streaming, so an oversized file is cut off mid-upload rather than
        # being received in full and then rejected.
        self.max_upload_bytes: int = _env_int("MAX_UPLOAD_BYTES", 15 * 1024 * 1024)

        # Most files in one request.
        #
        # LOWERED FROM 50 TO 20, AND THE REASON IS THE MEASURED CARD TIME.
        # At 136-172s per card with max_concurrency=1, the arithmetic is:
        #
        #     50 cards  ->  113 to 143 minutes   (up to 2h23m)
        #     20 cards  ->   45 to  57 minutes
        #     10 cards  ->   23 to  29 minutes
        #
        # 50 was chosen when a card was assumed to take 10-30s, which made a
        # batch ~15 minutes. Against the real number it commits a user to over
        # two hours in a single request, and three things get worse together
        # across that window: the browser tab must stay open to see progress,
        # a process restart loses whatever has not finished, and the estimate
        # shown at minute five is extrapolated from one card.
        #
        # 20 keeps the worst case under an hour -- a "leave it running over
        # lunch" window rather than an afternoon -- and bounds the disk spool
        # to 20 x 15 MB = 300 MB. It is not a technical limit: someone with 60
        # cards sends three batches, each of which can fail independently
        # instead of all sixty failing together. Raise it via the env var if a
        # faster backend makes the arithmetic different.
        self.max_files_per_request: int = _env_int("MAX_FILES_PER_REQUEST", 20)

        # How many cards may be in the model/decode pipeline simultaneously.
        # THIS is the setting that bounds peak memory: image decode costs
        # ~100 MB RSS per image in flight, so peak = max_concurrency * 100 MB
        # no matter how many files were uploaded.
        #
        # Default 1 because the target is a CPU-hosted llama.cpp instance,
        # which serves one request at a time; sending more just queues them
        # while memory climbs. Raise it only if the backend is a GPU server
        # with real batching (vLLM).
        self.max_concurrency: int = _env_int("MAX_CONCURRENCY", 1)

        # Largest image we will DECODE, in pixels. Pillow's own guard against
        # "decompression bombs": a 2 KB PNG can legitimately declare itself as
        # 50000x50000, which would allocate ~7.5 GB on decode and kill the
        # process. Pillow's default is ~89 million px; a 48 MP phone photo is
        # ~48 million, so this leaves generous headroom while blocking bombs.
        self.max_image_pixels: int = _env_int("MAX_IMAGE_PIXELS", 89_478_485)

        # How many finished jobs to keep in memory. The in-memory store would
        # otherwise grow without bound: a server left running for a week
        # accumulates every job and every lead ever processed until it is
        # killed by the OOM reaper. A real database makes this a retention
        # policy instead of a leak, which is one more reason the seam exists.
        self.max_jobs_retained: int = _env_int("MAX_JOBS_RETAINED", 50)

        # Where uploaded bytes are spooled while they wait for the worker.
        # Empty string = use the system temp dir.
        self.upload_dir: str = _env_str("UPLOAD_DIR", "")

        # --- Storage --------------------------------------------------------
        # Which LeadStore implementation build_store() returns.
        #
        #   sqlite  - persists to DB_PATH. Survives restarts, and is the only
        #             value under which `uvicorn --workers N` is correct.
        #   memory  - the original dict. Loses everything on restart. Kept
        #             because it makes tests hermetic (no file, no cleanup)
        #             and because it is a one-variable escape hatch if the
        #             database is ever the thing that is broken.
        #
        # Defaults to sqlite: an app that silently forgets a 25-minute batch
        # on restart is the wrong default, and "memory" should be something
        # you opt into deliberately.
        self.store_backend: str = _env_str("STORE_BACKEND", "sqlite").lower()

        # Where the SQLite file lives. A relative path is resolved against the
        # process's working directory, which in the container is /app -- so
        # the default lands inside the mounted volume at /app/data and
        # survives redeploys. Point it somewhere absolute on a host where the
        # working directory is not guaranteed (systemd, App Service).
        self.db_path: str = _env_str("DB_PATH", "./data/leads.db")

        # On startup, mark jobs still saying "queued"/"running" as failed.
        #
        # Persistence introduces a failure mode memory never had: the worker
        # is an asyncio task inside one process, so when that process dies the
        # task dies -- but the row survives, permanently claiming to be
        # running, and the UI polls a progress bar that can never complete.
        #
        # MUST BE FALSE FOR MORE THAN ONE REPLICA. With several processes
        # against one database, a replica starting later -- a rolling deploy,
        # an autoscale event -- would see another replica's genuinely live job
        # and kill it. True is correct for exactly one replica, which is what
        # this app should run as while a job is a local asyncio task.
        self.reclaim_stale_jobs: bool = _env_bool("RECLAIM_STALE_JOBS", True)

        # --- Image retention -------------------------------------------------
        # Where the normalised card images live. Content-addressed, so the
        # filename IS the sha256 of the bytes and identical uploads collapse
        # to one file. Sits beside the database so a single mounted volume
        # covers all persistent state.
        self.image_dir: str = _env_str("IMAGE_DIR", "./data/images")

        # How long a card image is kept before the startup sweep deletes it.
        # The extracted LEAD is kept forever -- it is a few hundred bytes and
        # it is the thing of value. Only the image expires.
        #
        # MEASURED COST ON THIS HARDWARE: a normalised card is 50-260 KB
        # (~150 KB typical). At 170 s/card with max_concurrency=1 the box
        # cannot physically exceed ~508 cards/day, so 30 days is bounded at
        # roughly 2.3 GB, worst case 3.9 GB.
        #
        # *** REVISIT THIS THE DAY MODEL_URL POINTS AT A GPU. *** That ceiling
        # is a function of how slow inference is. At 2 s/card the same 30-day
        # window allows ~85x the throughput, and this setting quietly becomes
        # a ~330 GB disk commitment. The two variables are coupled, and this
        # comment is the only thing that links them.
        self.image_retention_days: int = _env_int("IMAGE_RETENTION_DAYS", 30)

    @property
    def auth_enabled(self) -> bool:
        """
        Whether requests must carry a verified Clerk token.

        Reads from the mode, not from whether keys happen to be present, so
        that a missing key is a startup error rather than an open door.
        """
        return self.auth_mode == "clerk"

    @property
    def clerk_jwks_url(self) -> str:
        """Clerk publishes its public signing keys at this well-known path."""
        return f"{self.clerk_issuer}/.well-known/jwks.json"

    def as_public_dict(self) -> dict:
        """
        Settings that are safe to expose over HTTP (i.e. no secrets).

        Handy in /health: when something misbehaves, the first question is
        always "what is this process actually pointed at?"
        """
        return {
            "model_url": self.model_url,
            "model_name": self.model_name,
            "model_timeout_seconds": self.model_timeout_seconds,
            "max_image_edge": self.max_image_edge,
            "default_phone_region": self.default_phone_region,
            "model_api_key_set": bool(self.model_api_key),
            "max_upload_bytes": self.max_upload_bytes,
            "max_files_per_request": self.max_files_per_request,
            "max_concurrency": self.max_concurrency,
            "model_max_attempts": self.model_max_attempts,
            "auth_enabled": self.auth_enabled,
            "auth_mode": self.auth_mode,
            # Reported because "where did my jobs go after the restart?" is
            # answered instantly by seeing store_backend=memory here.
            "store_backend": self.store_backend,
            "image_retention_days": self.image_retention_days,
            "db_path": self.db_path if self.store_backend == "sqlite" else None,
        }


# A single shared instance imported by the rest of the app.
settings = Settings()
