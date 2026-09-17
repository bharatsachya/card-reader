/* ============================================================================
   Card Reader — frontend

   Plain ES2020, no framework, no build step.

   The page is a conversation: each upload becomes a user turn, and the
   assistant answers with a status line that fills in, then the leads table,
   then a download button. The API underneath is unchanged — POST /api/jobs,
   poll GET /api/jobs/{id} — so the chat shape is purely how results are
   PRESENTED, not a different protocol.

   SECURITY NOTE, and it matters more here than in a normal CRUD app: every
   value in the table came from a language model reading an image someone
   uploaded — untrusted input twice over. A card printed with
   <img src=x onerror=alert(1)> would be faithfully "extracted" and, if rows
   were built with innerHTML, executed. Every model-derived value therefore
   reaches the DOM through textContent, which writes text and never parses
   markup.
   ========================================================================= */

'use strict';

/* Mirrors MAX_UPLOAD_BYTES / MAX_FILES_PER_REQUEST on the server. Client-side
   checks are a courtesy — instant feedback, no wasted upload — NEVER a
   control: anyone can bypass them with curl, which is exactly why the server
   enforces the same limits independently. */
const MAX_FILE_BYTES = 15 * 1024 * 1024;
const MAX_FILES = 50;
const POLL_INTERVAL_MS = 1000;
const MAX_THUMBS = 8;

const COLUMNS = ['first_name', 'last_name', 'title', 'company',
                 'location', 'phone', 'email'];
const HEADINGS = ['First', 'Last', 'Title', 'Company', 'Location', 'Phone', 'Email'];
const MONO = new Set(['phone', 'email']);

const el = (id) => document.getElementById(id);

const thread    = el('thread');
const blank     = el('blank');
const composer  = el('composer');
const fileInput = el('file-input');
const promptEl  = el('prompt');
const chips     = el('chips');
const countEl   = el('count');
const sendBtn   = el('send');
const hint      = el('hint');
const mark      = el('mark');
const gate      = el('gate');
const appRoot   = el('app');

/* Set during boot. Every API call goes through this rather than fetch(), so
   the session token is attached in exactly one place. */
let api = (path, options) => fetch(path, options);

let chosen = [];          // File objects staged for upload
let pollTimer = null;
let currentJobId = null;
let objectUrls = [];      // thumbnail blob URLs, revoked on teardown

/* ---------- small helpers ---------------------------------------------- */

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function setHint(message, isTrouble = false) {
  hint.textContent = message;
  hint.classList.toggle('is-trouble', isTrouble);
}

/** Working state lives on one mark in the top bar, not on three spinners. */
function setWorking(working) {
  mark.classList.toggle('is-working', working);
}

/* ---------- model badge ------------------------------------------------- */

/* Reads /health on load so the page always says which backend this build talks
   to. Saves the "why are my results odd?" → "oh, wrong model" round trip. */
async function loadModelBadge() {
  const dot = el('model-dot');
  const name = el('model-name');
  try {
    const response = await api('/health');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    name.textContent = data.config.model_name;
    dot.classList.add('is-up');
  } catch {
    name.textContent = 'server unreachable';
    dot.classList.add('is-down');
  }
}

/* ---------- choosing files ---------------------------------------------- */

function addFiles(incoming) {
  const skipped = [];

  for (const file of incoming) {
    if (chosen.length >= MAX_FILES) {
      skipped.push(`${file.name} (over the ${MAX_FILES}-file limit)`);
      continue;
    }
    /* Dropping a folder yields entries with no type; this also filters out a
       stray .txt or .pdf swept up with the photos. */
    if (!file.type.startsWith('image/')) {
      skipped.push(`${file.name} (not an image)`);
      continue;
    }
    if (file.size > MAX_FILE_BYTES) {
      skipped.push(`${file.name} (over ${formatBytes(MAX_FILE_BYTES)})`);
      continue;
    }
    const duplicate = chosen.some((f) => f.name === file.name && f.size === file.size);
    if (!duplicate) chosen.push(file);
  }

  renderChips();

  if (skipped.length) {
    setHint(`Skipped ${skipped.length}: ${skipped.join(', ')}`, true);
  } else if (chosen.length) {
    setHint('Ready when you are.');
  }
}

function renderChips() {
  chips.replaceChildren();

  chosen.forEach((file, index) => {
    const li = document.createElement('li');
    li.className = 'chip';

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = file.name;          // user-supplied → textContent

    const size = document.createElement('span');
    size.className = 'size';
    size.textContent = formatBytes(file.size);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Remove ${file.name}`);
    remove.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      chosen.splice(index, 1);
      renderChips();
    });

    li.append(name, size, remove);
    chips.append(li);
  });

  const any = chosen.length > 0;
  chips.hidden = !any;
  sendBtn.disabled = !any;
  countEl.textContent = any ? plural(chosen.length, 'card') : '';
  el('prompt-text').innerHTML = any
    ? 'Add more, or <strong>choose files</strong>'
    : 'Drop business cards here, or <strong>choose files</strong>';
  if (!any) setHint('JPG, PNG, HEIC or WebP · up to 50 files, 15 MB each');
}

/* ---------- drag and drop ------------------------------------------------ */

/* Both handlers must preventDefault, or the browser's default action takes
   over and NAVIGATES AWAY to the dropped file — the commonest drag-and-drop
   bug. Listening on the document (not just the composer) means a drop anywhere
   on the page works, which is what people actually do. */
['dragenter', 'dragover'].forEach((type) => {
  document.addEventListener(type, (event) => {
    event.preventDefault();
    composer.classList.add('is-dragging');
  });
});

['dragleave', 'drop'].forEach((type) => {
  document.addEventListener(type, (event) => {
    event.preventDefault();
    /* dragleave fires constantly while moving between child elements; only
       clear the state when the cursor actually leaves the window. */
    if (type === 'dragleave' && event.relatedTarget) return;
    composer.classList.remove('is-dragging');
  });
});

document.addEventListener('drop', (event) => {
  if (event.dataTransfer?.files?.length) addFiles(Array.from(event.dataTransfer.files));
});

fileInput.addEventListener('change', () => {
  addFiles(Array.from(fileInput.files));
  fileInput.value = '';     // so re-picking the same file still fires change
});

promptEl.addEventListener('focus', () => composer.classList.add('is-focused'), true);
promptEl.addEventListener('blur', () => composer.classList.remove('is-focused'), true);

/* ---------- turns -------------------------------------------------------- */

function addUserTurn(files) {
  blank.hidden = true;

  const turn = document.createElement('section');
  turn.className = 'turn turn--user';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const totalBytes = files.reduce((sum, f) => sum + f.size, 0);
  bubble.textContent = `${plural(files.length, 'card')} · ${formatBytes(totalBytes)}`;
  turn.append(bubble);

  /* Thumbnails of what was actually sent. Capped, because 50 of them would
     bury the reply that follows. */
  const strip = document.createElement('div');
  strip.className = 'thumbs';
  files.slice(0, MAX_THUMBS).forEach((file) => {
    const url = URL.createObjectURL(file);
    objectUrls.push(url);
    const img = document.createElement('img');
    img.src = url;
    img.alt = '';                 // decorative: the count above carries meaning
    img.loading = 'lazy';
    strip.append(img);
  });
  if (files.length > MAX_THUMBS) {
    const more = document.createElement('span');
    more.className = 'more';
    more.textContent = `+${files.length - MAX_THUMBS}`;
    strip.append(more);
  }
  turn.append(strip);

  thread.append(turn);
  turn.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

/** Builds the assistant's reply and returns handles for updating it in place. */
function addAssistantTurn() {
  const turn = document.createElement('section');
  turn.className = 'turn';

  const who = document.createElement('span');
  who.className = 'turn__who';
  who.textContent = 'Card Reader';

  const card = document.createElement('div');
  card.className = 'card reply';

  const status = document.createElement('div');
  status.className = 'reply__status';
  const statusText = document.createElement('span');
  statusText.className = 'shimmer-text';
  statusText.textContent = 'Uploading…';
  status.append(statusText);

  const meter = document.createElement('div');
  meter.className = 'meter';
  const fill = document.createElement('div');
  fill.className = 'meter__fill';
  meter.append(fill);

  const note = document.createElement('p');
  note.className = 'reply__note';

  const actions = document.createElement('div');
  actions.className = 'reply__actions';

  card.append(status, meter, note, actions);
  // Returned below so finishReply can remove it once there is nothing to report.
  turn.append(who, card);
  thread.append(turn);
  turn.scrollIntoView({ behavior: 'smooth', block: 'end' });

  return { card, statusText, fill, note, actions, meter };
}

/* ---------- sending ------------------------------------------------------ */

sendBtn.addEventListener('click', async () => {
  if (!chosen.length) return;

  const files = chosen;
  chosen = [];
  renderChips();
  sendBtn.disabled = true;

  addUserTurn(files);
  const reply = addAssistantTurn();
  setWorking(true);

  const form = new FormData();
  /* The field name must be "files" for every file — that repetition is what
     FastAPI's `files: list[UploadFile]` binds to. */
  for (const file of files) form.append('files', file);

  let data;
  try {
    const response = await api('/api/jobs', { method: 'POST', body: form });
    data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  } catch (error) {
    failReply(reply, `Upload failed: ${error.message}`);
    return;
  }

  if (data.rejected?.length) {
    reply.note.textContent = `Skipped: ${data.rejected.join('; ')}`;
    reply.note.classList.add('is-trouble');
  }

  startPolling(data.job_id, data.accepted, reply);
});

function failReply(reply, message) {
  setWorking(false);
  reply.statusText.classList.remove('shimmer-text');
  reply.statusText.textContent = message;
  reply.card.classList.add('frame-trouble');
  reply.card.classList.remove('card');
  sendBtn.disabled = chosen.length === 0;
}

/* ---------- polling ------------------------------------------------------ */

/* setTimeout after each response rather than setInterval: setInterval fires on
   a fixed clock whether or not the previous request returned, so a slow server
   makes requests pile up on each other. Scheduling the next poll only AFTER
   the current one resolves keeps exactly one request in flight, always. */
function startPolling(jobId, total, reply) {
  currentJobId = jobId;
  reply.statusText.textContent = `Reading ${plural(total, 'card')}…`;
  poll(reply);
}

async function poll(reply) {
  if (!currentJobId) return;

  try {
    const response = await api(`/api/jobs/${currentJobId}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const job = await response.json();

    renderTable(reply, job.leads);
    const pct = job.total ? Math.round((job.processed / job.total) * 100) : 0;
    reply.fill.style.width = `${pct}%`;

    if (job.status === 'done' || job.status === 'failed') {
      finishReply(reply, job);
      return;
    }

    reply.statusText.textContent =
      `Reading card ${Math.min(job.processed + 1, job.total)} of ${job.total}…`;
  } catch (error) {
    /* Keep polling: a transient blip should not abandon a job that is still
       running perfectly well on the server. */
    reply.note.textContent = `Lost contact with the server (${error.message}). Retrying…`;
    reply.note.classList.add('is-trouble');
  }

  pollTimer = setTimeout(() => poll(reply), POLL_INTERVAL_MS);
}

function finishReply(reply, job) {
  clearTimeout(pollTimer);
  pollTimer = null;
  setWorking(false);
  sendBtn.disabled = chosen.length === 0;

  reply.statusText.classList.remove('shimmer-text');
  reply.meter.classList.add('is-done');

  if (job.status === 'failed') {
    reply.statusText.textContent = 'The job stopped early.';
    reply.note.textContent = job.error || '';
    reply.note.classList.add('is-trouble');
  } else {
    reply.statusText.textContent =
      `Read ${plural(job.succeeded, 'card')}${job.failed ? `, flagged ${job.failed}` : ''}.`;
    reply.note.classList.toggle('is-trouble', false);
    reply.note.textContent = job.failed
      ? 'Flagged rows are marked on the left. They are in the download too, with the reason.'
      : '';
  }

  if (job.processed > 0) {
    const download = document.createElement('button');
    download.type = 'button';
    download.className = 'btn btn--primary';
    download.textContent = 'Download .xlsx';
    /* A download is the one call that cannot go through api(): a plain
       navigation carries no Authorization header, so the server would 401.
       We fetch it WITH the header, then hand the browser a blob URL. The blob
       is revoked immediately afterwards so the file is not held in memory. */
    download.addEventListener('click', () => downloadXlsx(job.job_id, false));

    const clean = document.createElement('button');
    clean.type = 'button';
    clean.className = 'btn';
    clean.textContent = 'Successful rows only';
    clean.addEventListener('click', () => downloadXlsx(job.job_id, true));

    reply.actions.replaceChildren(download);
    if (job.failed > 0) reply.actions.append(clean);
  }
}

/* ---------- downloading -------------------------------------------------- */

async function downloadXlsx(jobId, onlySuccessful) {
  const query = onlySuccessful ? '?only_successful=true' : '';
  try {
    const response = await api(`/api/jobs/${jobId}/export.xlsx${query}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    /* Take the filename the server chose, rather than inventing one here --
       it is already timestamped so repeated downloads do not overwrite. */
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match ? match[1] : 'leads.xlsx';

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    setHint(`Download failed: ${error.message}`, true);
  }
}

/* ---------- the table ---------------------------------------------------- */

function renderTable(reply, leads) {
  if (!leads.length) return;

  let wrap = reply.card.querySelector('.tablewrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'tablewrap';
    const table = document.createElement('table');
    table.className = 'leads';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    [...HEADINGS, 'Source'].forEach((label) => {
      const th = document.createElement('th');
      th.scope = 'col';
      th.textContent = label;
      headRow.append(th);
    });
    thead.append(headRow);
    table.append(thead, document.createElement('tbody'));
    wrap.append(table);
    /* Before the actions, so the buttons stay at the bottom of the reply. */
    reply.card.insertBefore(wrap, reply.actions);
  }

  const tbody = wrap.querySelector('tbody');
  /* Rebuilding the whole tbody each poll is fine at these sizes (tens to low
     hundreds of rows) and removes a class of diffing bugs. For thousands of
     rows the fix is to append only what is new. */
  tbody.replaceChildren();

  for (const lead of leads) {
    const tr = document.createElement('tr');
    if (lead.status === 'model_error' || lead.status === 'input_error') {
      tr.classList.add('is-failed');
    } else if (lead.status !== 'ok') {
      tr.classList.add('is-flagged');
    }

    COLUMNS.forEach((column, index) => {
      const td = document.createElement('td');
      const value = lead[column];

      if (value) {
        td.textContent = value;        // model output → textContent. See header.
        // The column name rides along so phone and email can be treated
        // differently: one must not wrap, the other must.
        if (MONO.has(column)) td.className = `mono ${column}`;
      } else {
        td.textContent = '—';
        td.className = 'missing';
      }

      /* The reason sits under the first cell, so a flagged row explains itself
         instead of just being blank. */
      if (index === 0 && lead.status !== 'ok' && lead.error) {
        const reason = document.createElement('small');
        reason.className = 'reason';
        reason.textContent = lead.error;
        td.append(reason);
      }

      tr.append(td);
    });

    const source = document.createElement('td');
    source.className = 'source';
    source.textContent = lead.source_filename;
    source.title = lead.source_filename;
    tr.append(source);

    tbody.append(tr);
  }
}

/* Blob URLs for thumbnails hold the whole file in memory until revoked. */
window.addEventListener('pagehide', () => {
  objectUrls.forEach((url) => URL.revokeObjectURL(url));
});

/* ---------- the sign-in screen ------------------------------------------- */

/**
 * Put a usable sign-in in front of the user, whatever Clerk manages to do.
 *
 * Three levels, in order of preference:
 *   1. Clerk's embedded form, mounted into the page.
 *   2. Clerk's modal, opened by our own button — works even when the embedded
 *      component does not render.
 *   3. A link to Clerk's hosted Account Portal, which needs no local rendering
 *      at all and only fails if Clerk itself is unreachable.
 *
 * The reason for the ladder: mountSignIn() can return WITHOUT THROWING and
 * still render nothing (a blocked request, a component bundle that failed to
 * lazy-load, an instance that is not configured for embedded components). A
 * try/catch cannot see that. So instead of trusting it, we check whether it
 * actually put anything on the page and fall back if it did not.
 */
async function presentSignIn(auth) {
  const container = el('clerk-signin');
  const actions = el('gate-actions');
  const fallback = el('gate-fallback');

  const revealButtons = (message, isTrouble) => {
    actions.hidden = false;
    if (message) {
      fallback.textContent = message;
      fallback.classList.toggle('is-trouble', Boolean(isTrouble));
    }
  };

  el('gate-signin').addEventListener('click', () => openClerk(auth, 'signIn'));
  el('gate-signup').addEventListener('click', () => openClerk(auth, 'signUp'));

  try {
    auth.clerk.mountSignIn(container);
  } catch (error) {
    revealButtons(`Embedded form unavailable (${error.message}). Use the buttons above.`, true);
    await fillDiagnostics(auth);
    return;
  }

  /* Give the component a beat to render, then verify it actually did.
     Mounting is asynchronous inside Clerk, so checking immediately would
     always report empty. */
  await new Promise((resolve) => setTimeout(resolve, 1500));

  await fillDiagnostics(auth);

  if (container.childElementCount === 0) {
    revealButtons(
      'The embedded sign-in form did not render, so here are buttons that ' +
      'open Clerk directly. If neither works, check the browser console for ' +
      'a Clerk error.',
      false,
    );
  }
}

/**
 * Notice that the user signed in, however they did it.
 *
 * Clerk.addListener() fires when the session changes, and for a sign-in that
 * happens inside this page it is enough. But sign-in frequently happens
 * SOMEWHERE ELSE: a popup, a second tab, or a redirect to Clerk's hosted
 * portal. In those cases this page's listener may never fire, so the gate sits
 * there while the user is — from Clerk's point of view — perfectly signed in.
 * That is indistinguishable from "the app is broken".
 *
 * So the listener is kept as the fast path and backed by two things that do
 * not depend on it: a poll, and the window regaining focus (which is exactly
 * when the user returns from a popup or another tab).
 *
 * Polling a local object costs nothing — it is a property read, not a network
 * call. Clerk keeps the session object up to date itself.
 */
function watchForSignIn(auth) {
  let done = false;

  const proceed = () => {
    if (done || !auth.signedIn) return;
    done = true;
    /* A reload re-runs boot() against a live session, rather than trying to
       hot-swap a page that was built for the signed-out case. */
    window.location.reload();
  };

  try {
    auth.clerk.addListener(proceed);
  } catch { /* the poll below covers us */ }

  window.addEventListener('focus', proceed);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) proceed();
  });

  const timer = setInterval(() => {
    if (done) { clearInterval(timer); return; }
    proceed();
  }, 1000);

  /* A manual escape hatch, in case every automatic route fails. It costs one
     button and removes the possibility of a dead end. */
  const manual = el('gate-continue');
  manual.hidden = false;
  manual.addEventListener('click', () => {
    if (auth.signedIn) window.location.reload();
    else {
      const fallback = el('gate-fallback');
      fallback.textContent = 'Still no session on this page. Open the '
        + 'diagnostics below, or clear the session and sign in again.';
      fallback.classList.add('is-trouble');
    }
  });
}

/**
 * Explain, on the page, why the gate is still up.
 *
 * The failure this is really hunting for: a session belonging to a DIFFERENT
 * Clerk instance than the one the server verifies against. That presents as an
 * unexplained loop — sign in, land back on the sign-in screen, repeat — because
 * the browser genuinely has a valid session and the server genuinely rejects
 * its token, and neither side is wrong on its own terms. Comparing the token's
 * `iss` claim with the issuer the server expects names it immediately.
 */
async function fillDiagnostics(auth) {
  const body = el('diag-body');
  const rows = [];
  const add = (label, value, state) => rows.push([label, value, state]);

  add('Clerk loaded', auth.clerk ? `yes (v${auth.clerk.version || '?'})` : 'no',
      auth.clerk ? 'ok' : 'bad');
  add('Clerk user', auth.clerk?.user ? auth.clerk.user.id : 'none',
      auth.clerk?.user ? 'ok' : null);
  add('Clerk session', auth.clerk?.session ? auth.clerk.session.id : 'none',
      auth.clerk?.session ? 'ok' : null);
  add('Browser instance', auth.host, 'ok');
  add('Server expects', (auth.expectedIssuer || '').replace('https://', ''), 'ok');

  /* If there is a token, compare who issued it with who the server trusts. */
  const token = await auth.getToken();
  if (token) {
    const claims = window.CardReaderAuth.peekClaims(token);
    const issuer = (claims?.iss || '').replace('https://', '');
    const expected = (auth.expectedIssuer || '').replace('https://', '');
    const matches = issuer && expected && issuer === expected;
    add('Token issued by', issuer || 'unreadable', matches ? 'ok' : 'bad');
    if (!matches) {
      add('Problem',
          'This session belongs to a different Clerk instance than the server '
          + 'accepts. Clear it below, then sign in again.', 'bad');
    }
  } else {
    add('Token', 'none issued', null);
  }

  /* What the server actually says right now. */
  try {
    const response = await fetch('/api/jobs', {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    add('Server response', `HTTP ${response.status}`, response.ok ? 'ok' : 'bad');
  } catch (error) {
    add('Server response', error.message, 'bad');
  }

  body.replaceChildren();
  for (const [label, value, state] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = value;
    if (state) dd.className = state;
    body.append(dt, dd);
  }

  /* Signing out clears a stale session so the next attempt starts clean —
     the fix for the mismatch above, and harmless otherwise. */
  el('diag-signout').addEventListener('click', async () => {
    try { await auth.clerk.signOut(); } catch { /* nothing to sign out of */ }
    window.location.reload();
  });
}

/** Open Clerk's modal, or fall back to its hosted page. */
function openClerk(auth, which) {
  const fallback = el('gate-fallback');
  try {
    if (which === 'signUp' && auth.clerk.openSignUp) {
      auth.clerk.openSignUp({});
      return;
    }
    if (auth.clerk.openSignIn) {
      auth.clerk.openSignIn({});
      return;
    }
    throw new Error('Clerk exposes no modal on this build');
  } catch (error) {
    /* Last resort: Clerk's hosted Account Portal. `redirect_url` brings the
       user back here once they are signed in. */
    const host = auth.accountsHost;
    if (!host) {
      fallback.textContent = `Could not open sign-in: ${error.message}`;
      fallback.classList.add('is-trouble');
      return;
    }
    const back = encodeURIComponent(window.location.origin);
    const path = which === 'signUp' ? 'sign-up' : 'sign-in';
    window.location.href = `https://${host}/${path}?redirect_url=${back}`;
  }
}

/* ---------- boot --------------------------------------------------------- */

/* Nothing renders until we know whether there is a user. Showing the app and
   then yanking it away when auth resolves is worse than a beat of nothing. */
async function boot() {
  /* auth.js is not served at all when auth is disabled, so its absence is a
     normal state rather than an error. */
  const auth = window.CardReaderAuth
    ? await window.CardReaderAuth.initAuth()
    : { enabled: false, getToken: async () => null, user: null, signedIn: false };

  const showGate = () => {
    if (!gate) return;          // no gate in the document when auth is off
    gate.hidden = false;
    appRoot.hidden = true;
  };

  api = window.CardReaderAuth
    ? window.CardReaderAuth.makeApi(auth, showGate)
    : (path, options) => fetch(path, options);

  /* A one-line, credential-free view of where auth got to. Printing this
     beats asking someone to paste a network request: those carry live session
     tokens and cookies, and this carries neither. */
  if (auth.enabled) {
    console.info('[card-reader] auth', {
      signedIn: auth.signedIn,
      hasUser: Boolean(auth.clerk?.user),
      hasSession: Boolean(auth.clerk?.session),
      issuerHost: auth.host,
    });
  }

  if (!auth.enabled) {
    // Auth disabled server-side: the gate was never sent, so just show the app.
    if (gate) gate.hidden = true;
    appRoot.hidden = false;
  } else if (!auth.signedIn) {
    showGate();
    await presentSignIn(auth);
    watchForSignIn(auth);
    return;   // the app stays inert until there is a user
  } else {
    gate.hidden = true;
    appRoot.hidden = false;
    try {
      auth.clerk.mountUserButton(el('user-button'));
    } catch { /* the avatar is a nicety; its absence must not break the app */ }
  }

  loadModelBadge();
  renderChips();
}

boot();
