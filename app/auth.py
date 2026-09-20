"""
Clerk session verification.

Mirrors what the interview kit's `packages/auth/src/index.ts` does with `jose`,
in Python with PyJWT.

THE CENTRAL RULE, and it is worth stating out loud because it is the whole
reason this file exists rather than a one-line header check:

    The API verifies the token ITSELF. It never trusts a header because the
    frontend promises to have set one.

An API that believes `X-User-Id` because its own frontend sets it is not
authenticated; it is authenticated only to people who use the frontend. Anyone
with curl can send any header they like. So the browser sends Clerk's signed
session JWT, and we check that signature against Clerk's published keys.

HOW THAT CHECK WORKS, in four steps:
  1. Clerk signs each session token with a private key only Clerk holds.
  2. The matching PUBLIC key is published at the instance's JWKS endpoint,
     `<issuer>/.well-known/jwks.json`.
  3. We fetch that public key (cached) and verify the signature. A token
     someone forged will not verify, because they cannot sign with Clerk's
     private key.
  4. We also check `exp` (not expired) and `iss` (issued by OUR instance, not
     by some other Clerk tenant an attacker controls).

NOTE WHAT IS NOT NEEDED: the Clerk SECRET key. Verification uses public keys
only. This process never holds a Clerk credential that could be used to act on
the account -- which is a genuinely nice property, and the reason CLERK_SECRET_KEY
appears nowhere in this codebase.
"""

import ssl
from dataclasses import dataclass
from typing import Optional

import certifi
import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

from app.config import settings


@dataclass(frozen=True)
class User:
    """The authenticated caller. `id` is Clerk's stable user id (`sub`)."""

    id: str
    email: Optional[str] = None


class AuthError(Exception):
    """
    Why a request has no user.

    Two codes rather than one, because the UI has two different things to do.
    SESSION_EXPIRED means the credentials were ours and simply ran out: send
    the user back to sign in and return them to the page they were on.
    UNAUTHENTICATED means there was nothing usable to check -- no header, a
    foreign issuer, a bad signature -- and the honest response is the sign-in
    page with no promise of coming back.

    Distinguishing the two does leak that a token was well-formed and expired.
    That is not a secret worth keeping: the client already holds the token and
    can read its own `exp`. Everything on the other side of the line -- a bad
    signature, an unknown key id, a wrong issuer -- collapses into the one
    generic code, so probing with forged tokens reveals nothing about which
    half of the forgery failed.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# PyJWKClient fetches the JWKS and caches the keys, so we are not making an
# outbound HTTPS request on every single API call. It refetches when it sees a
# key id it does not know, which is what makes Clerk's key rotation transparent.
_jwk_client: PyJWKClient | None = None


def _ssl_context() -> ssl.SSLContext:
    """
    An SSL context that can actually verify Clerk's certificate.

    Python does NOT use the operating system's certificate store. On macOS a
    python.org install ships with no CA bundle at all until you run
    "Install Certificates.command", and minimal Linux containers often have
    none either. The symptom is nasty because it is misleading: fetching the
    JWKS fails with CERTIFICATE_VERIFY_FAILED, which surfaces as
    "could not resolve signing key" -- so every token looks invalid, including
    genuine ones, and it reads like an auth bug rather than a TLS one.

    certifi ships Mozilla's CA bundle as a Python package (httpx already
    depends on it, so this costs us nothing), and pointing at it explicitly
    makes verification behave identically on a laptop and in a container.

    Note what we are NOT doing: disabling verification. Turning off
    certificate checking to make an SSL error go away would mean anyone who
    can intercept the connection could serve their own JWKS -- their own
    public keys -- and every token they forged would verify. That would
    silently convert this file from a security control into decoration.
    """
    return ssl.create_default_context(cafile=certifi.where())


def _jwks() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(
            settings.clerk_jwks_url,
            cache_keys=True,
            ssl_context=_ssl_context(),
            timeout=15,
        )
    return _jwk_client


def verify_token(token: str) -> User:
    """
    Verify a Clerk session JWT and return the user it identifies.

    Raises AuthError for anything that does not check out. Never returns a
    partially-trusted result.
    """
    try:
        signing_key = _jwks().get_signing_key_from_jwt(token)
    except Exception as exc:  # network failure, unknown kid, malformed token
        raise AuthError("UNAUTHENTICATED", f"could not resolve signing key: {exc}")

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            # Clerk session tokens carry no `aud` claim by default, so asking
            # PyJWT to check one would reject every valid token.
            options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
            # Tolerance for clock skew between this server and Clerk's. Without
            # it, a server whose clock is a few seconds fast rejects tokens
            # that were issued a moment ago.
            leeway=30,
        )
    except jwt.ExpiredSignatureError:
        raise AuthError("SESSION_EXPIRED", "session has expired")
    except jwt.InvalidTokenError as exc:
        # Everything else collapses into one generic code -- see AuthError.
        raise AuthError("UNAUTHENTICATED", f"invalid session token: {exc}")

    subject = claims.get("sub")
    if not subject:
        raise AuthError("UNAUTHENTICATED", "token carries no subject")

    return User(id=subject, email=claims.get("email"))


# A browser-supplied identity, used ONLY when AUTH_MODE=local.
#
# WHAT THIS IS: a random id the browser generates once and keeps in
# localStorage, so two people opening the public demo URL do not see each
# other's sessions, and a fresh browser (or cleared site data) starts empty.
#
# WHAT THIS IS EMPHATICALLY NOT: authentication. The header is supplied by the
# client and nothing verifies it, so anyone can send any value with curl and
# read that id's data. It separates honest users; it stops nobody. Real
# isolation is AUTH_MODE=clerk, where the token is signed by Clerk and checked
# against their public keys.
#
# It is therefore accepted ONLY in local mode. In clerk mode the header is
# ignored entirely -- otherwise it would be a trivial way to bypass the very
# verification that mode exists to perform.
_CLIENT_ID_HEADER = "X-Client-Id"
_CLIENT_ID_MAX = 64


def _local_user(request: Request) -> User:
    """The identity used when AUTH_MODE=local: per browser, or a shared default."""
    raw = (request.headers.get(_CLIENT_ID_HEADER) or "").strip()

    # Constrained to hex and bounded in length. The value becomes a user_id and
    # reaches SQL as a bound parameter, so injection is not the risk -- but an
    # unbounded, arbitrary-bytes string would still let a caller write junk
    # into the database and into log lines, and there is no reason to accept
    # anything the client is not supposed to be sending.
    if raw and len(raw) <= _CLIENT_ID_MAX and all(c in "0123456789abcdef" for c in raw):
        return User(id=f"anon-{raw}", email=None)

    # No usable header: the original single shared user. Keeps `curl` against a
    # dev box working with no ceremony.
    return User(id="local", email=None)


def _bearer_token(request: Request) -> Optional[str]:
    """Pull the token out of `Authorization: Bearer <token>`."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def require_user(request: Request) -> User:
    """
    FastAPI dependency: the caller must be signed in.

    Used as `user: User = Depends(require_user)` on every route that touches
    a user's data. Returning 401 with the code in the body lets the frontend
    tell "sign in again" from "sign in".

    When auth is disabled (no CLERK_ISSUER configured) this returns a fixed
    local user instead, so the app still runs standalone -- which is what keeps
    `curl localhost:8000` working for development and keeps the project
    runnable by someone who has no Clerk account.
    """
    if not settings.auth_enabled:
        return _local_user(request)

    token = _bearer_token(request)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "no session token"},
        )

    try:
        return verify_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=401, detail={"code": exc.code, "message": exc.message}
        )


# Convenience alias so routes read as `user: User = Depends(CurrentUser)`.
CurrentUser = require_user
