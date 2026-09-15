"""
HTTP client for the model's OpenAI-compatible /v1/chat/completions endpoint.

This is the ONLY module that knows an HTTP call to a model exists. Everything
else in the app deals in plain Python values. That boundary is what makes the
rest of the code testable without a running model, and what makes swapping
Ollama for llama.cpp a config change.
"""

import httpx

from app.config import settings
from app.prompt import build_messages


class ModelError(Exception):
    """
    The model call failed: no server, timeout, HTTP error, or unusable body.

    A distinct exception type matters: callers must be able to distinguish
    "the model was unreachable" (infrastructure, likely affects every card)
    from "the model replied with junk" (content, likely affects one card).
    They get different statuses and different messages in the UI.
    """


def _build_payload(image_data_url: str) -> dict:
    """Assemble the request body. Kept separate so it can be unit-tested."""
    return {
        "model": settings.model_name,
        "messages": build_messages(image_data_url),
        # temperature 0 = greedy decoding: always take the highest-probability
        # token. The same card yields the same JSON every run. This is an
        # extraction task with exactly one correct answer, so sampling
        # "creativity" is pure downside -- it would make bugs unreproducible
        # and results inconsistent between runs of the same batch.
        "temperature": 0,
        # Cap the reply length. A well-behaved answer is ~100 tokens; this
        # bounds the damage if the model ignores the prompt and starts
        # rambling, which would otherwise burn the full timeout.
        "max_tokens": 512,
        "stream": False,
    }


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    # Sent only when configured. Ollama and llama.cpp ignore it; vLLM and
    # hosted endpoints require it. Being optional means the remote deployment
    # needs no code change.
    if settings.model_api_key:
        headers["Authorization"] = f"Bearer {settings.model_api_key}"
    return headers


def _extract_text(body: dict) -> str:
    """
    Pull the assistant's text out of an OpenAI-shaped response.

    Written defensively: this is a response from a third-party server we do not
    control, so we never assume the keys exist. A KeyError/IndexError here
    would surface as an opaque 500; a ModelError surfaces as a flagged row.
    """
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"unexpected response shape from model: {exc}") from exc
    if not isinstance(content, str):
        raise ModelError(f"model returned non-text content: {type(content).__name__}")
    return content


async def complete(image_data_url: str, client: httpx.AsyncClient | None = None) -> str:
    """
    Send one image to the model and return its raw text reply.

    Note what this function does NOT do: it does not parse JSON, and it does
    not know what a Lead is. Its only job is "bytes in, model's words out".
    Parsing is a separate concern with separate failure modes.

    `client` is injectable so a batch can reuse one connection pool instead of
    opening a new TCP+TLS connection per card.
    """
    payload = _build_payload(image_data_url)
    timeout = settings.model_timeout_seconds

    # Either use the caller's client, or open a short-lived one we own.
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        response = await client.post(
            settings.model_url, json=payload, headers=_headers(), timeout=timeout
        )
    except httpx.TimeoutException as exc:
        raise ModelError(
            f"model timed out after {timeout}s at {settings.model_url}"
        ) from exc
    except httpx.RequestError as exc:
        # Covers connection refused, DNS failure, connection reset -- i.e.
        # "there is no model server there". The most common failure in dev.
        raise ModelError(
            f"could not reach model at {settings.model_url}: {exc}"
        ) from exc
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code != 200:
        # Truncate: an HTML error page from a proxy could be megabytes.
        raise ModelError(
            f"model returned HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise ModelError(f"model response was not JSON: {response.text[:300]}") from exc

    return _extract_text(body)
