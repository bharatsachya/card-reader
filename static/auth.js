/* ============================================================================
   Card Reader — authentication

   Clerk without React and without a build step. Clerk publishes
   @clerk/clerk-js, a plain browser bundle that exposes a global `Clerk`
   object, so the whole integration is: load a script, call load(), read
   Clerk.user, and attach a token to each API call.

   WHAT THE BROWSER IS AND IS NOT TRUSTED WITH:
   This file decides what the USER SEES — sign-in screen or app. It does not
   decide what the user may DO. Every data route on the server independently
   verifies the token's signature against Clerk's public keys (app/auth.py).
   Deleting this file entirely would change nothing about what the API allows;
   it would just make the app unusable. That separation is the point: client
   code is a convenience, server code is the control.
   ========================================================================= */

'use strict';

/**
 * Clerk's frontend API host is encoded inside the publishable key.
 *
 * A key looks like `pk_test_<base64>`, where the base64 decodes to the host
 * with a trailing "$" — e.g. "bursting-turkey-6056.clerk.accounts.dev$".
 * Clerk's own loader does exactly this, and deriving it means the host is
 * never configured twice and so can never disagree with the key.
 */
function frontendApiHost(publishableKey) {
  const encoded = publishableKey.replace(/^pk_(test|live)_/, '');
  // atob needs correct padding; a key's base64 often arrives without it.
  const padded = encoded + '='.repeat((4 - (encoded.length % 4)) % 4);
  return atob(padded).replace(/\$$/, '');
}

function loadScript(src, publishableKey) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.async = true;
    script.crossOrigin = 'anonymous';
    // Clerk reads its key from this data attribute at load time.
    script.dataset.clerkPublishableKey = publishableKey;
    script.src = src;
    script.addEventListener('load', resolve);
    script.addEventListener('error', () =>
      reject(new Error('could not load Clerk — check your connection')));
    document.head.append(script);
  });
}

/**
 * Start auth and return an object the rest of the app uses.
 *
 * Always resolves. If auth is disabled on the server, or Clerk cannot be
 * reached, it degrades to an unauthenticated mode rather than leaving the user
 * staring at a blank page — the server is still the thing enforcing access, so
 * a degraded client is not a security problem.
 */
async function initAuth() {
  let config;
  try {
    const response = await fetch('/api/auth-config');
    config = await response.json();
  } catch {
    return { enabled: false, getToken: async () => null, user: null };
  }

  // Kept so the page can compare it against the token it holds.
  const expectedIssuer = config.issuer || '';

  if (!config.auth_enabled || !config.publishable_key) {
    // Auth switched off server-side: run the app with no sign-in at all.
    return { enabled: false, getToken: async () => null, user: null };
  }

  const host = frontendApiHost(config.publishable_key);
  await loadScript(
    `https://${host}/npm/@clerk/clerk-js@5/dist/clerk.browser.js`,
    config.publishable_key,
  );

  await window.Clerk.load();

  return {
    enabled: true,
    clerk: window.Clerk,
    /* Clerk's hosted Account Portal lives on the same subdomain with
       ".clerk" removed: bursting-turkey-6056.clerk.accounts.dev becomes
       bursting-turkey-6056.accounts.dev. Used only as a last resort, when
       neither the embedded form nor the modal works. */
    accountsHost: host.replace('.clerk.accounts.dev', '.accounts.dev'),
    host,
    expectedIssuer,
    get user() { return window.Clerk.user; },
    /**
     * Whether this browser is signed in.
     *
     * Checks the SESSION as well as the user, not just the user. Clerk can
     * hold a live session whose `user` has not been populated yet — a slow
     * user fetch, or a browser blocking the cross-site storage Clerk would
     * normally read it from. Gating on `user` alone therefore shows the
     * sign-in screen to someone who is already signed in, and no amount of
     * signing in again fixes it, because the session was never the problem.
     *
     * If a session exists, the server is the thing that decides whether its
     * token is valid — so trusting it here costs nothing: a bad session simply
     * 401s on the first API call and the gate comes back.
     */
    get signedIn() {
      return Boolean(window.Clerk.user || window.Clerk.session);
    },
    /**
     * A short-lived session JWT for the Authorization header.
     *
     * Fetched per request rather than cached: Clerk rotates these on a ~60s
     * cycle, so a token held for the length of a 20-minute extraction job
     * would expire mid-poll and start 401-ing. getToken() returns the current
     * one and refreshes it when needed.
     */
    getToken: async () => {
      try {
        return window.Clerk.session ? await window.Clerk.session.getToken() : null;
      } catch {
        return null;
      }
    },
  };
}

/**
 * fetch() with the session token attached.
 *
 * Every call to the API goes through this. A 401 means the session died while
 * the page was open — the honest response is to put the sign-in screen back
 * rather than let the UI silently show stale data.
 */
function makeApi(auth, onUnauthenticated) {
  return async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const token = await auth.getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    /* Carried in both modes so one wrapper serves both. The server ignores it
       entirely when AUTH_MODE=clerk -- honouring a client-supplied identity
       there would be a trivial bypass of the signature check. */
    if (window.cardReaderClientId) {
      headers.set('X-Client-Id', window.cardReaderClientId);
    }

    const response = await fetch(path, { ...options, headers });

    if (response.status === 401 && auth.enabled) {
      onUnauthenticated();
      throw new Error('Your session ended. Please sign in again.');
    }
    return response;
  };
}

/**
 * Read a JWT's payload WITHOUT verifying it. For display only.
 *
 * This is safe precisely because nothing is trusted from it: it feeds a
 * diagnostic line on screen, never an access decision. The server does the
 * real verification against Clerk's public keys, and that is the only check
 * that counts. Decoding a token client-side to decide what a user may do
 * would be the classic mistake — anyone can edit the payload of an unverified
 * token.
 */
function peekClaims(token) {
  try {
    const payload = token.split('.')[1];
    const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
    return JSON.parse(json);
  } catch {
    return null;
  }
}

window.CardReaderAuth = { initAuth, makeApi, peekClaims };
