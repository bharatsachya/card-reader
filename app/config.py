"""
Central configuration, read from environment variables.

Why this file exists at all:
Nothing about *which* model we talk to should be baked into application logic.
Today this points at a local stub / Ollama; tomorrow it points at llama.cpp on
an AWS box serving a quantized Qwen3-VL-2B. That switch must be an env-var
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
        # A CPU-hosted 2B model is slow. Generous default, tunable per deploy.
        self.model_timeout_seconds: int = _env_int("MODEL_TIMEOUT_SECONDS", 180)
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

        # --- Backpressure / resource limits ---------------------------------
        # These exist to make the server survive a hostile or careless upload.
        # Every one of them rejects work BEFORE memory is spent on it.

        # Largest single file we will accept, in bytes. Enforced while
        # streaming, so an oversized file is cut off mid-upload rather than
        # being received in full and then rejected.
        self.max_upload_bytes: int = _env_int("MAX_UPLOAD_BYTES", 15 * 1024 * 1024)

        # Most files in one request. Caps the worst-case disk spool for a
        # single request at max_files * max_upload_bytes (here ~750 MB).
        self.max_files_per_request: int = _env_int("MAX_FILES_PER_REQUEST", 50)

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

    @property
    def auth_enabled(self) -> bool:
        """Auth is on only when BOTH halves are configured."""
        return bool(self.clerk_issuer and self.clerk_publishable_key)

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
        }


# A single shared instance imported by the rest of the app.
settings = Settings()
