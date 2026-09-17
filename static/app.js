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
const MAX_FILES = 20;   // mirrors MAX_FILES_PER_REQUEST; see app/config.py
const POLL_INTERVAL_MS = 1000;
/* Used once a job has gone several ticks with no card landing. See pollDelay(). */
const SLOW_POLL_INTERVAL_MS = 5000;
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
let lastProcessed = -1;   // drives the adaptive poll interval
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
  if (!any) setHint('JPG, PNG, HEIC or WebP · up to 20 files, 15 MB each');
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

  /* The estimate lives on its own line under the meter. aria-live="polite"
     so a screen-reader user is told the remaining time as it changes, but is
     never interrupted mid-sentence to hear it. */
  const eta = document.createElement('p');
  eta.className = 'reply__eta';
  eta.setAttribute('aria-live', 'polite');

  const note = document.createElement('p');
  note.className = 'reply__note';

  const actions = document.createElement('div');
  actions.className = 'reply__actions';

  card.append(status, meter, eta, note, actions);
  // Returned below so finishReply can remove it once there is nothing to report.
  turn.append(who, card);
  thread.append(turn);
  turn.scrollIntoView({ behavior: 'smooth', block: 'end' });

  return { card, statusText, fill, note, actions, meter, eta };
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
  /* Join the open session if there is one. When there is not — first load, or
     straight after a page refresh — the server opens one and tells us which,
     so the UI never has to guess and a bare curl still works. */
  if (currentSessionId) form.append('session_id', currentSessionId);

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

  liveJobId = data.job_id;
  liveReply = reply;
  /* The server is the authority on which session this landed in: it may have
     opened a new one, or declined an id that was not ours. */
  currentSessionId = data.session_id || currentSessionId;
  openSessionId = currentSessionId;
  /* Refreshed now, not when the job finishes, so the session appears in the
     sidebar the moment work starts -- a history panel that only shows
     completed work is useless during the hour you most want to look at it. */
  loadSessions();

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

/* ---------- honest progress ---------------------------------------------- */

/**
 * "about 2 h 40 min", from seconds.
 *
 * Rounded coarsely ON PURPOSE. "2 h 41 min 12 s" claims a precision this
 * estimate does not have -- it is derived from a handful of samples of a model
 * whose per-card time varies with how much text is on the card. A number that
 * looks exact invites people to trust it to the minute and then feel misled.
 */
function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  if (seconds < 90)   return 'under a minute';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60)   return `about ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round((minutes % 60) / 15) * 15;   // quarter-hour buckets
  if (rest === 0 || rest === 60) return `about ${hours + (rest === 60 ? 1 : 0)} h`;
  return `about ${hours} h ${rest} min`;
}

/**
 * How much longer this job has, measured from ITS OWN observed pace.
 *
 * WHY IT IS MEASURED AND NOT CONFIGURED. The brief is explicit that stub-server
 * timings must never reach a user-facing estimate, and the reason generalises:
 * a constant baked in from a developer's machine is wrong on every other
 * machine, and wrong in the direction that matters -- a 2B model on shared CPU
 * can take 4x what the same model takes on an idle box, and the same card
 * varies with how densely it is printed. So the only honest source is this
 * job's own elapsed time divided by the cards it has actually finished.
 *
 * BEFORE THE FIRST CARD FINISHES THERE IS NO ESTIMATE, and we say so rather
 * than showing a number. A fabricated first guess is worse than silence: it
 * anchors the user, and when the real pace turns out to be triple it, the
 * feature has actively lied to them.
 *
 * THE MEAN, NOT THE LAST CARD. One slow card -- a retry, a dense card, another
 * request stealing the model's single inference slot -- would otherwise make
 * the estimate leap around every few minutes, which reads as broken even when
 * each individual number is defensible.
 */
function estimateRemaining(job) {
  const remaining = job.total - job.processed;
  if (remaining <= 0) return '';
  if (!job.processed) {
    /* Say what is happening instead of showing a spinner with no content.
       At >240s per card the first card alone is several minutes, and silence
       for that long is indistinguishable from a hang. */
    return 'Working out how long this will take — the first card sets the pace.';
  }

  const startedAt = new Date(job.created_at).getTime();
  if (Number.isNaN(startedAt)) return '';
  const elapsedSeconds = (Date.now() - startedAt) / 1000;
  const perCard = elapsedSeconds / job.processed;

  return `${formatDuration(perCard * remaining)} left · `
       + `${Math.round(perCard)}s per card so far`;
}

/* ---------- polling ------------------------------------------------------ */

/* setTimeout after each response rather than setInterval: setInterval fires on
   a fixed clock whether or not the previous request returned, so a slow server
   makes requests pile up on each other. Scheduling the next poll only AFTER
   the current one resolves keeps exactly one request in flight, always. */
/* How long the job has looked unchanged, in poll ticks. Reset whenever a card
   lands, so the interval snaps back to responsive the moment there is news. */
let idlePolls = 0;

/**
 * How long to wait before the next poll.
 *
 * A fixed one-second interval was right when a card took a few seconds. At
 * 260s per card it means ~260 requests to observe a single change, each one
 * returning the ENTIRE growing lead set -- by card 40 that is a 40-row payload
 * fetched 260 times to learn nothing. Backing off to 5s once nothing has moved
 * cuts that by ~80% while still showing a completed card within five seconds
 * of it landing, which is imperceptible against a four-minute card.
 *
 * Deliberately NOT derived from the measured per-card time: that would make
 * the UI stop checking for minutes at a stretch, so a job that finished early
 * -- or died -- would sit there looking busy. Five seconds is the floor on how
 * stale the page is allowed to be, regardless of how slow the model is.
 */
function pollDelay() {
  return idlePolls >= 5 ? SLOW_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
}

function startPolling(jobId, total, reply) {
  currentJobId = jobId;
  idlePolls = 0;
  reply.statusText.textContent = `Reading ${plural(total, 'card')}…`;
  poll(reply);
}

async function poll(reply) {
  if (!currentJobId) return;

  try {
    const response = await api(`/api/jobs/${currentJobId}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const job = await response.json();

    idlePolls = job.processed === lastProcessed ? idlePolls + 1 : 0;
    lastProcessed = job.processed;

    renderTable(reply, job.leads);
    const pct = job.total ? Math.round((job.processed / job.total) * 100) : 0;
    reply.fill.style.width = `${pct}%`;

    if (job.status === 'done' || job.status === 'failed') {
      finishReply(reply, job);
      return;
    }

    reply.statusText.textContent =
      `Reading card ${Math.min(job.processed + 1, job.total)} of ${job.total}…`;
    reply.eta.textContent = estimateRemaining(job);
  } catch (error) {
    /* Keep polling: a transient blip should not abandon a job that is still
       running perfectly well on the server. */
    reply.note.textContent = `Lost contact with the server (${error.message}). Retrying…`;
    reply.note.classList.add('is-trouble');
  }

  pollTimer = setTimeout(() => poll(reply), pollDelay());
}

/**
 * Stop polling and present the finished job.
 *
 * SPLIT IN TWO ON PURPOSE. Opening a run from the history sidebar needs the
 * presentation half and must NOT have the teardown half: a history view that
 * called this wholesale would clear pollTimer and silently kill a job that is
 * still running in another turn. The bug would look like "long batches
 * randomly stop updating", and nothing in the sidebar code would point at it.
 */
function finishReply(reply, job) {
  clearTimeout(pollTimer);
  pollTimer = null;
  liveJobId = null;
  liveReply = null;
  setWorking(false);
  sendBtn.disabled = chosen.length === 0;

  paintReplyOutcome(reply, job);
  /* The session's card and success counts just changed, and so did the
     spreadsheet behind the download button. */
  loadSessions();
  if (openSessionId) {
    api(`/api/sessions/${encodeURIComponent(openSessionId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((session) => { if (session) addSessionDownload(session); })
      .catch(() => { /* the per-job download below still works */ });
  }
}

/** The presentational half: safe to call for any job, live or historical. */
function paintReplyOutcome(reply, job) {
  reply.statusText.classList.remove('shimmer-text');
  /* A finished job has nothing left to estimate; leaving the last figure on
     screen would read as "still 40 minutes to go" next to a completed table. */
  if (reply.eta) reply.eta.textContent = '';
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

  /* The set the lightbox arrows walk through is whatever this table shows,
     so opening a card from a history view navigates that run, not the live one. */
  reply.card.__leads = leads;

  const tbody = wrap.querySelector('tbody');
  /* Rebuilding the whole tbody each poll is fine at these sizes (tens to low
     hundreds of rows) and removes a class of diffing bugs. For thousands of
     rows the fix is to append only what is new. */
  tbody.replaceChildren();

  leads.forEach((lead, rowIndex) => {
    const tr = document.createElement('tr');
    /* A row is a button in spirit, so give it the affordances of one: a tab
       stop, a role, and Enter/Space. Making the whole row clickable without
       this leaves every keyboard user unable to open a card at all. */
    tr.tabIndex = 0;
    tr.setAttribute('role', 'button');
    tr.setAttribute('aria-label',
      `View ${[lead.first_name, lead.last_name].filter(Boolean).join(' ') || lead.source_filename || 'card'}`);
    const open = () => openLightbox(reply.card.__leads || leads, rowIndex);
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
    });
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
  });
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

  /* Who is signed in, in the sidebar. Only meaningful under AUTH_MODE=clerk;
     in local mode there is one fixed user and naming them would imply an
     account boundary that does not exist. */
  if (auth.enabled && auth.user) {
    sidebarWho.textContent =
      auth.user.primaryEmailAddress?.emailAddress || auth.user.username || 'Signed in';
    sidebarWho.hidden = false;
  }

  loadSessions();
}

boot();

/* ============================================================================
   Card images

   WHY THESE ARE NOT JUST <img src="/api/leads/{id}/image">.

   Every API call goes through api(), which attaches the Clerk session token as
   an Authorization header. An <img> tag cannot do that — the browser issues
   that request itself, and there is no way to add a header to it. So the
   moment AUTH_MODE=clerk, every thumbnail and every lightbox image would come
   back 401 while the rest of the page worked perfectly. The alternatives are
   worse: a token in the query string lands in logs and Referer headers, and a
   cookie would need CSRF protection the API does not otherwise require.

   So the bytes are fetched like any other authenticated resource and handed to
   the <img> as a blob URL. This costs nothing in bandwidth: the response still
   carries `private, max-age=31536000, immutable`, so a second fetch of the same
   URL is served from the browser's HTTP cache without touching the network.

   The Map is a SECOND cache in front of that, holding the blob URL itself.
   Without it, reopening the lightbox would allocate a new blob for bytes
   already in memory, and every allocation leaks until the page unloads —
   blob URLs are not garbage collected while a URL string exists.
   ========================================================================= */

const imageCache = new Map();     // lead_id -> blob URL
const imageFailed = new Set();    // lead_ids known to 404, so we stop asking

async function leadImageUrl(leadId) {
  if (!leadId || imageFailed.has(leadId)) return null;
  if (imageCache.has(leadId)) return imageCache.get(leadId);

  try {
    const response = await api(`/api/leads/${encodeURIComponent(leadId)}/image`);
    if (!response.ok) {
      /* 404 is an ordinary outcome, not a failure: retention deletes images on
         purpose, and /api/extract never stores one. Remember it so a sidebar
         that re-renders on every poll does not re-request a known-absent
         image once per second. */
      imageFailed.add(leadId);
      return null;
    }
    const url = URL.createObjectURL(await response.blob());
    imageCache.set(leadId, url);
    objectUrls.push(url);
    return url;
  } catch {
    imageFailed.add(leadId);
    return null;
  }
}

/** Point an <img> at a lead's image once it arrives. Safe if it never does. */
async function fillImage(img, leadId) {
  const url = await leadImageUrl(leadId);
  if (url) img.src = url;
  else img.closest('.run__thumbs, .lightbox__stage')?.classList.add('is-empty');
}

/* ============================================================================
   Session history sidebar

   A SESSION IS THE UNIT, NOT AN UPLOAD. A job is one batch of files; someone
   collecting cards at a conference photographs them in several goes. Grouping
   those into a session means one spreadsheet covers the lot, instead of three
   downloads to merge by hand.

   The model is a chat thread: "New session" opens an empty one, everything
   uploaded while it is open belongs to it, and clicking an old one reopens it
   with every upload it contains, in order.
   ========================================================================= */

const sidebar     = el('sidebar');
const runsList    = el('runs');
const runsEmpty   = el('runs-empty');
const runsMore    = el('runs-more');
const scrim       = el('scrim');
const sidebarWho  = el('sidebar-who');

let nextCursor = null;        // pagination cursor; null = no more pages
let currentSessionId = null;  // where the next upload lands
let openSessionId = null;     // which session the main panel is showing
let liveJobId = null;         // the job currently being polled, if any
let liveReply = null;         // its handles, so we can scroll to it

/**
 * A session's display name in the viewer's own timezone.
 *
 * Falls back to the stored title if the timestamp cannot be parsed, so a row
 * is never blank -- an unlabelled entry in a history list is indistinguishable
 * from every other unlabelled entry.
 */
function sessionLabel(session) {
  const when = new Date(session.created_at);
  if (Number.isNaN(when.getTime())) return session.title || 'Session';
  return when.toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

/** Relative time, because a history list is scanned, not read. */
function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 60)     return 'just now';
  if (seconds < 3600)   return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400)  return `${Math.floor(seconds / 3600)} h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)} d ago`;
  return new Date(then).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function buildSessionRow(session) {
  const li = document.createElement('li');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'run';
  button.dataset.sessionId = session.session_id;

  const title = document.createElement('span');
  title.className = 'run__title';
  /* Rendered from created_at in the BROWSER's timezone, not from the stored
     title. The server writes that title in UTC, so a user in IST opening a
     session at 00:11 local sees it labelled with yesterday's date -- correct,
     and confusing every time. created_at is ISO-8601 with an offset, so the
     browser can place it properly; the stored title remains what the API and
     the export filename use, which must not drift per viewer. */
  title.textContent = sessionLabel(session);

  const top = document.createElement('div');
  top.className = 'run__top';
  const when = document.createElement('span');
  when.className = 'run__when';
  when.textContent = relativeTime(session.created_at);
  const count = document.createElement('span');
  count.className = 'run__count';
  count.textContent = session.cards
    ? plural(session.cards, 'card')
    : 'empty';
  top.append(when, count);

  const stats = document.createElement('div');
  stats.className = 'run__stats';
  stats.dataset.role = 'stats';
  paintStats(stats, session);

  const thumbs = document.createElement('div');
  thumbs.className = 'run__thumbs';
  (session.thumbnails || []).forEach((leadId) => {
    const img = document.createElement('img');
    img.alt = '';
    img.loading = 'lazy';
    thumbs.append(img);
    fillImage(img, leadId);
  });

  button.append(title, top, stats, thumbs);
  button.addEventListener('click', () => openSession(session.session_id));
  li.append(button);
  return li;
}

/** Split out so a poll can repaint a running session's counters in place. */
function paintStats(node, session) {
  node.replaceChildren();
  if (!session.cards) return;
  const ok = document.createElement('span');
  ok.textContent = `${session.succeeded} read`;
  node.append(ok);
  if (session.failed) {
    const bad = document.createElement('span');
    bad.className = 'bad';
    bad.textContent = `${session.failed} flagged`;
    node.append(bad);
  }
}

async function loadSessions({ append = false } = {}) {
  const query = append && nextCursor ? `?cursor=${encodeURIComponent(nextCursor)}` : '';
  let data;
  try {
    const response = await api(`/api/sessions${query}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    data = await response.json();
  } catch {
    /* A history panel that cannot load is not worth interrupting the app for:
       the user came here to extract cards, and that still works. */
    return;
  }

  if (!append) runsList.replaceChildren();
  data.sessions.forEach((session) => runsList.append(buildSessionRow(session)));

  nextCursor = data.next_cursor;
  runsMore.hidden = !nextCursor;
  runsEmpty.hidden = runsList.children.length > 0;
  markActiveSession();
}

function markActiveSession() {
  runsList.querySelectorAll('.run').forEach((node) => {
    node.classList.toggle('is-active', node.dataset.sessionId === openSessionId);
    node.classList.toggle('is-current', node.dataset.sessionId === currentSessionId);
  });
}

/** Open a brand-new, empty session and clear the thread. */
async function startNewSession() {
  closeSidebarOnNarrow();
  let session;
  try {
    const response = await api('/api/sessions', { method: 'POST' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    session = await response.json();
  } catch (error) {
    setHint(`Could not start a session: ${error.message}`, true);
    return;
  }
  currentSessionId = session.session_id;
  openSessionId = session.session_id;
  thread.replaceChildren();
  blank.hidden = false;
  thread.append(blank);
  liveReply = null;
  await loadSessions();
  setHint('New session. Drop cards to begin.');
}

/**
 * Show one session: every upload it contains, oldest first, then one download
 * covering the whole thing.
 *
 * Each job is rendered through the SAME addAssistantTurn + renderTable path a
 * live job uses. Two renderers for one table is how the flagged-row handling
 * and the textContent discipline drift apart.
 */
async function openSession(sessionId) {
  closeSidebarOnNarrow();

  let session;
  try {
    const response = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    session = await response.json();
  } catch (error) {
    setHint(`Could not open that session: ${error.message}`, true);
    return;
  }

  openSessionId = sessionId;
  /* Opening a session also makes it the one new uploads join -- which is what
     "click a chat and keep typing" does, and avoids a state where the visible
     thread and the upload target are different sessions. */
  currentSessionId = sessionId;
  markActiveSession();

  blank.hidden = true;
  thread.replaceChildren();
  liveReply = null;

  const jobs = session.jobs_detail || [];
  if (!jobs.length) {
    blank.hidden = false;
    thread.append(blank);
    setHint('This session is empty. Drop cards to begin.');
    return;
  }

  jobs.forEach((job) => {
    const reply = addAssistantTurn();
    reply.meter.remove();
    paintReplyOutcome(reply, job);
    renderTable(reply, job.leads);
  });

  addSessionDownload(session);
}

/**
 * One download for the session, regenerated server-side on every request.
 *
 * There is no stored spreadsheet being amended: the file is built from the
 * session's current leads each time it is asked for, which is why adding cards
 * to an open session "updates" it. A materialised file would need a cache to
 * invalidate and could disagree with the database.
 */
function addSessionDownload(session) {
  const wrap = document.createElement('div');
  wrap.className = 'turn';

  const card = document.createElement('div');
  card.className = 'card reply';

  const line = document.createElement('div');
  line.className = 'reply__status';
  const text = document.createElement('span');
  const cards = session.cards || 0;
  text.textContent = cards
    ? `${plural(cards, 'card')} in this session across ${plural(session.jobs, 'upload')}.`
    : 'Nothing in this session yet.';
  line.append(text);

  const actions = document.createElement('div');
  actions.className = 'reply__actions';
  if (cards) {
    const all = document.createElement('button');
    all.type = 'button';
    all.className = 'btn btn--primary';
    all.textContent = 'Download session .xlsx';
    all.addEventListener('click', () => downloadSessionXlsx(session.session_id, false));
    actions.append(all);

    if (session.failed) {
      const clean = document.createElement('button');
      clean.type = 'button';
      clean.className = 'btn';
      clean.textContent = 'Successful rows only';
      clean.addEventListener('click', () => downloadSessionXlsx(session.session_id, true));
      actions.append(clean);
    }
  }

  card.append(line, actions);
  wrap.append(card);
  thread.append(wrap);
}

async function downloadSessionXlsx(sessionId, onlySuccessful) {
  const query = onlySuccessful ? '?only_successful=true' : '';
  try {
    const response = await api(
      `/api/sessions/${encodeURIComponent(sessionId)}/export.xlsx${query}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `leads-session-${sessionId}.xlsx`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    setHint(`Download failed: ${error.message}`, true);
  }
}

/* ---------- sidebar open/close (narrow screens only) --------------------- */

function openSidebar() {
  sidebar.classList.add('is-open');
  scrim.hidden = false;
  el('sidebar-toggle').setAttribute('aria-expanded', 'true');
}
function closeSidebar() {
  sidebar.classList.remove('is-open');
  scrim.hidden = true;
  el('sidebar-toggle').setAttribute('aria-expanded', 'false');
}
/* On a wide screen the panel is permanent, so "close after clicking" would be
   a jarring no-op. matchMedia keeps the breakpoint in one place rather than
   repeating 60rem as a magic number across handlers. */
const wideScreen = window.matchMedia('(min-width: 60rem)');
function closeSidebarOnNarrow() { if (!wideScreen.matches) closeSidebar(); }

el('sidebar-toggle').addEventListener('click', () =>
  sidebar.classList.contains('is-open') ? closeSidebar() : openSidebar());
el('sidebar-close').addEventListener('click', closeSidebar);
el('new-session').addEventListener('click', startNewSession);
scrim.addEventListener('click', closeSidebar);
runsMore.addEventListener('click', () => loadSessions({ append: true }));

/* ============================================================================
   Lightbox
   ========================================================================= */

const lightbox   = el('lightbox');
const lbImage    = el('lb-image');
const lbFields   = el('lb-fields');
const lbTitle    = el('lightbox-title');
const lbCount    = el('lb-count');
const lbFlag     = el('lb-flag');
const lbPrev     = el('lb-prev');
const lbNext     = el('lb-next');

let currentLeads = [];        // the set the arrows move through
let lbIndex = 0;
let lastFocused = null;       // restored on close

function openLightbox(leads, index) {
  if (!leads.length) return;
  currentLeads = leads;
  lbIndex = Math.max(0, Math.min(index, leads.length - 1));

  /* Remembered BEFORE focus moves. Returning focus to where it came from is
     what stops a keyboard user being dumped at the top of the document every
     time they close a card — the single most common way a modal breaks
     keyboard navigation. */
  lastFocused = document.activeElement;

  lightbox.hidden = false;
  /* The background must not scroll under the overlay. Set here rather than in
     CSS so it is unmistakably paired with the reset in closeLightbox(). */
  document.body.style.overflow = 'hidden';
  paintLightbox();
  el('lb-close').focus();
}

function closeLightbox() {
  lightbox.hidden = true;
  document.body.style.overflow = '';
  lbImage.removeAttribute('src');
  if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
  lastFocused = null;
}

function paintLightbox() {
  const lead = currentLeads[lbIndex];
  if (!lead) return;

  const name = [lead.first_name, lead.last_name].filter(Boolean).join(' ');
  lbTitle.textContent = name || lead.source_filename || 'Card';
  lbCount.textContent = `${lbIndex + 1} of ${currentLeads.length} · ${lead.source_filename || ''}`;

  if (lead.status !== 'ok' && lead.error) {
    lbFlag.textContent = lead.error;      // model/server text → textContent
    lbFlag.hidden = false;
  } else {
    lbFlag.hidden = true;
  }

  /* Cleared before the fetch resolves, so an arrow press never leaves the
     PREVIOUS card's photograph sitting next to THIS card's fields — which
     would be a convincing, silent lie about what the model read. */
  lbImage.removeAttribute('src');
  lbImage.alt = name ? `Business card for ${name}` : 'Business card';
  const requested = lbIndex;
  leadImageUrl(lead.id).then((url) => {
    if (url && requested === lbIndex) lbImage.src = url;   // ignore a stale response
  });

  lbFields.replaceChildren();
  COLUMNS.forEach((column, i) => {
    const dt = document.createElement('dt');
    dt.textContent = HEADINGS[i];
    const dd = document.createElement('dd');
    const value = lead[column];
    dd.textContent = value || '—';                // model output → textContent
    if (!value) dd.className = 'missing';
    else if (MONO.has(column)) dd.className = 'mono';
    lbFields.append(dt, dd);
  });

  lbPrev.disabled = lbIndex === 0;
  lbNext.disabled = lbIndex === currentLeads.length - 1;
}

function moveLightbox(step) {
  const next = lbIndex + step;
  if (next < 0 || next >= currentLeads.length) return;
  lbIndex = next;
  paintLightbox();
}

lbPrev.addEventListener('click', () => moveLightbox(-1));
lbNext.addEventListener('click', () => moveLightbox(1));
el('lb-close').addEventListener('click', closeLightbox);

/* Click-outside. The check is "did the click land on the backdrop itself",
   not "was it outside the panel" — a click that STARTS inside the panel and
   drags out (selecting text, or a sloppy tap) would otherwise close the
   dialog and lose the selection. */
lightbox.addEventListener('mousedown', (event) => {
  if (event.target === lightbox) closeLightbox();
});

/**
 * Keyboard handling for the open dialog: Escape, arrows, and the focus trap.
 *
 * WHY A TRAP AT ALL. aria-modal="true" tells assistive technology the rest of
 * the page is inert, but it does not make it so: Tab still walks into the page
 * behind. A screen-reader user would be told they are in a dialog while their
 * focus silently wanders into a form they cannot see. The trap makes the
 * promise the attribute makes actually true.
 */
document.addEventListener('keydown', (event) => {
  if (lightbox.hidden) return;

  if (event.key === 'Escape')     { event.preventDefault(); closeLightbox(); return; }
  if (event.key === 'ArrowLeft')  { event.preventDefault(); moveLightbox(-1); return; }
  if (event.key === 'ArrowRight') { event.preventDefault(); moveLightbox(1); return; }
  if (event.key !== 'Tab') return;

  /* Queried live rather than cached: the arrow buttons become disabled at the
     ends of the set, and a disabled control is not focusable — a cached list
     would trap focus onto something the browser refuses to focus. */
  const focusable = [...lightbox.querySelectorAll('button, [href], [tabindex]:not([tabindex="-1"])')]
    .filter((node) => !node.disabled && node.offsetParent !== null);
  if (!focusable.length) return;

  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
});
