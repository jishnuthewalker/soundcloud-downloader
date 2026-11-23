// The downloader page (opened in a tab, so long jobs keep running when you click elsewhere).
// Flow: link → lookup → confirm → parallel download + convert → save via the downloads API → report.

import { Client, SoundCloudError, cleanUrl, lookup, fetchAudio, fetchCover } from './lib/soundcloud.js';
import { FFmpegPool, makeMp3, prepareCover, safeName, trackName, trackTags, MAX_ART } from './lib/media.js';

const ext = globalThis.browser ?? globalThis.chrome;
const HOSTS = ['https://soundcloud.com/*', 'https://*.soundcloud.com/*', 'https://*.sndcdn.com/*',
  'https://*.soundcloud.cloud/*'];
const DEFAULTS = { folder: 'SoundCloud', workers: 4, useLogin: true, redownload: false };
const MAX_WORKERS = 8;
const READY = 'Paste a track, playlist or profile link.';
const $ = (id) => document.getElementById(id);

const state = {
  busy: false,
  settings: { ...DEFAULTS },
  token: null,            // the browser's SoundCloud login (oauth_token cookie)
  account: null,          // {username, likesUrl}
  abort: null,
  job: null,              // last looked-up job, for retries
  failures: [],
  dir: '',
  lastDownloadId: null,
};

// ---------------------------------------------------------------- small UI helpers

function log(line) {
  const el = $('log');
  el.textContent += `${line}\n`;
  el.scrollTop = el.scrollHeight;
}
const clearLog = () => { $('log').textContent = ''; };
const setStatus = (text) => { $('status').textContent = text; };

function setBusy(busy, status) {
  state.busy = busy;
  for (const id of ['go-btn', 'likes-btn', 'refresh-btn', 'link', 'folder', 'workers', 'redownload', 'use-login']) {
    $(id).disabled = busy;
  }
  $('stop-btn').hidden = !busy;
  if (status) setStatus(status);
}

async function saveSettings() {
  const workers = Number($('workers').value) || DEFAULTS.workers;
  state.settings = {
    folder: $('folder').value.trim() || DEFAULTS.folder,
    workers: Math.min(MAX_WORKERS, Math.max(1, workers)),
    useLogin: $('use-login').checked,
    redownload: $('redownload').checked,
  };
  await ext.storage.local.set({ settings: state.settings });
}

function reasonOf(error) {
  if (error instanceof SoundCloudError) return error.message;
  const msg = String(error?.message || error || 'unknown error');
  return msg.length > 200 ? `${msg.slice(0, 200)}…` : msg;
}

// ---------------------------------------------------------------- permissions & account

async function hasPermissions() {
  try { return await ext.permissions.contains({ origins: HOSTS }); } catch { return true; }
}

async function checkPermissions() {
  const ok = await hasPermissions();
  $('perm-banner').hidden = ok;
  return ok;
}

async function readLoginCookie() {
  let cookies;
  try {
    cookies = await ext.cookies.getAll({ domain: 'soundcloud.com', name: 'oauth_token' });
  } catch {
    // Firefox with first-party isolation needs this extra parameter.
    cookies = await ext.cookies.getAll({ domain: 'soundcloud.com', name: 'oauth_token', firstPartyDomain: null });
  }
  const best = cookies.filter((c) => c.value).sort((a, b) => (b.expirationDate || 0) - (a.expirationDate || 0))[0];
  return best ? best.value : null;
}

async function refreshAccount() {
  const text = $('account-text');
  $('likes-btn').hidden = true;
  $('use-login-wrap').hidden = true;
  $('signin-link').hidden = true;
  text.textContent = 'Checking…';
  state.account = null;
  try {
    state.token = await readLoginCookie();
  } catch {
    state.token = null;
  }
  if (!state.token) {
    text.textContent = 'Not signed in (only needed for likes, private or Go+ tracks).';
    $('signin-link').hidden = false;
    return;
  }
  try {
    const me = await new Client({ token: state.token }).me();
    const url = (me.permalink_url || `https://soundcloud.com/${me.permalink}`).replace(/\/+$/, '');
    state.account = { username: me.username || me.permalink, likesUrl: `${url}/likes` };
    text.textContent = `Signed in as ${state.account.username} (this browser's SoundCloud login).`;
    $('use-login-wrap').hidden = false;
    $('likes-btn').hidden = false;
  } catch (e) {
    if (e.code === 'auth') {
      state.token = null;
      text.textContent = 'Your SoundCloud login in this browser has expired.';
      $('signin-link').hidden = false;
    } else {
      text.textContent = `Couldn't check your SoundCloud login: ${reasonOf(e)}`;
    }
  }
}

function client() {
  return new Client({ token: state.settings.useLogin ? state.token : null });
}

// ---------------------------------------------------------------- saving files

const pendingDownloads = new Map();     // id -> {resolve, reject}

ext.downloads.onChanged.addListener((delta) => {
  const waiter = pendingDownloads.get(delta.id);
  if (!waiter || !delta.state) return;
  if (delta.state.current === 'complete') waiter.resolve();
  else if (delta.state.current === 'interrupted') waiter.reject(new Error(`Saving failed (${delta.error?.current || 'interrupted'}).`));
});

async function saveFile(blob, filename) {
  const url = URL.createObjectURL(blob);
  try {
    const id = await ext.downloads.download({ url, filename, conflictAction: 'overwrite', saveAs: false });
    state.lastDownloadId = id;
    const done = new Promise((resolve, reject) => pendingDownloads.set(id, { resolve, reject }));
    const [item] = await ext.downloads.search({ id });          // it may already be finished
    if (item?.state === 'complete') pendingDownloads.get(id).resolve();
    else if (item?.state === 'interrupted') pendingDownloads.get(id).reject(new Error(`Saving failed (${item.error}).`));
    try { await done; } finally { pendingDownloads.delete(id); }
  } finally {
    URL.revokeObjectURL(url);
  }
}

// Which track IDs were already saved into a folder ("Download again" ignores this).
async function loadArchive(dir) {
  const key = `archive:${dir}`;
  const { [key]: ids = [] } = await ext.storage.local.get(key);
  return { key, ids: new Set(ids) };
}
const saveArchive = (archive) => ext.storage.local.set({ [archive.key]: [...archive.ids] });

// ---------------------------------------------------------------- flow

async function start(urlOverride) {
  if (state.busy) return;
  const url = cleanUrl(urlOverride ?? $('link').value);
  if (!url) {
    setStatus('That isn\'t a SoundCloud link. Paste a soundcloud.com link to a track, playlist or profile.');
    $('link').focus();
    return;
  }
  if (!(await checkPermissions())) {
    setStatus('Click "Allow access to SoundCloud" above first.');
    return;
  }
  $('link').value = url;
  await saveSettings();
  clearLog();
  $('report').hidden = true;
  $('bar').max = 1; $('bar').value = 0;
  state.abort = new AbortController();
  setBusy(true, 'Looking up link…');
  try {
    const job = await lookup(client(), url, { signal: state.abort.signal });
    if (!job.tracks.length) throw new SoundCloudError('That link has no downloadable tracks (it may be empty or private).');
    state.job = job;
    confirmJob(job);
  } catch (e) {
    setBusy(false, e.name === 'AbortError' ? 'Stopped.' : `Couldn't look up that link: ${reasonOf(e)}`);
  }
}

function targetDir(job) {
  const base = safeName(state.settings.folder, 'SoundCloud');
  return job.folder ? `${base}/${safeName(job.folder)}` : base;
}

function confirmJob(job) {
  const n = job.tracks.length;
  $('confirm-title').textContent = job.title;
  $('confirm-body').textContent = `${job.by ? `by ${job.by} · ` : ''}${n === 1 ? '1 track' : `${n} tracks`} as MP3`
    + ` → Downloads/${targetDir(job)}`;
  $('confirm').hidden = false;
  setStatus('Ready to download.');
  $('confirm-yes').focus();
}

async function runJob(job, items, { retry = false } = {}) {
  const signal = state.abort.signal;
  const dir = state.dir = targetDir(job);
  const archive = await loadArchive(dir);
  const total = items.length;
  const pad = String(job.tracks.length).length;
  const pool = new FFmpegPool(Math.min(state.settings.workers, 3));
  const failures = [];
  let done = 0;
  let skipped = 0;
  let saved = 0;
  $('bar').max = total; $('bar').value = 0;
  setBusy(true, `Downloading ${total === 1 ? '1 track' : `${total} tracks`}…`);

  const queue = [...items];
  const worker = async () => {
    while (queue.length && !signal.aborted) {
      const i = queue.shift();
      const track = job.tracks[i - 1];
      const name = trackName(track);
      try {
        if (!state.settings.redownload && !retry && archive.ids.has(track.id)) {
          skipped++;
          log(`↷ ${name} (already downloaded)`);
        } else {
          const c = client();
          const [audio, cover] = await Promise.all([
            fetchAudio(c, track, { signal }),
            fetchCover(track, { signal }).then(prepareCover),
          ]);
          const tags = trackTags(track, { album: job.album, index: job.playlist ? i : null,
            total: job.playlist ? job.tracks.length : null });
          const { data, coverDropped } = await pool.run((ff) => makeMp3(ff, { audio: audio.data, mime: audio.mime, cover, tags }));
          const file = safeName(job.playlist ? `${String(i).padStart(pad, '0')}. ${name}` : name);
          await saveFile(new Blob([data], { type: 'audio/mpeg' }), `${dir}/${file}.mp3`);
          archive.ids.add(track.id);
          saved++;
          if (saved % 10 === 0) await saveArchive(archive);
          log(`✓ ${name}${coverDropped ? ' (saved without album art)' : ''}`);
        }
      } catch (e) {
        if (e.name === 'AbortError' || signal.aborted) return;
        failures.push({ index: i, name, reason: reasonOf(e) });
        log(`✗ ${name} — ${reasonOf(e)}`);
      }
      done++;
      $('bar').value = done;
      setStatus(`${done}/${total} done`);
    }
  };

  try {
    await Promise.all(Array.from({ length: Math.min(state.settings.workers, total) }, worker));
  } finally {
    pool.terminate();
    await saveArchive(archive);
  }
  if (signal.aborted) {
    setBusy(false, `Stopped. ${saved} saved.`);
    return;
  }
  finish(job, total, saved, skipped, failures);
}

async function finish(job, total, saved, skipped, failures) {
  state.failures = failures;
  const skippedNote = skipped ? ` (${skipped} already downloaded, skipped)` : '';
  if (!failures.length) {
    setBusy(false, `All ${total} done: ${saved} saved as MP3${skippedNote}. Album art fits ${MAX_ART}×${MAX_ART}.`);
    if (saved) {
      $('report').hidden = true;
      log('');
      log(`Saved to Downloads/${state.dir}`);
    }
    return;
  }
  failures.sort((a, b) => a.index - b.index);
  const body = failures.map((f) => `• ${job.playlist ? `#${f.index}  ` : ''}${f.name}\n    ${f.reason}`).join('\n\n');
  setBusy(false, `${total - failures.length}/${total} done — ${failures.length} problem(s)${skippedNote}`);
  $('report-title').textContent = `${total - failures.length} of ${total} done. ${failures.length} didn't make it:`;
  $('report-body').textContent = body;
  $('report').hidden = false;
  try {
    const text = `${job.title} — ${new Date().toLocaleString()}\n${$('link').value}\n\n${body}\n`;
    await saveFile(new Blob([text], { type: 'text/plain' }), `${state.dir}/failed_downloads.txt`);
  } catch { /* the on-screen list is enough */ }
}

// ---------------------------------------------------------------- wiring

async function init() {
  $('version').textContent = `v${ext.runtime.getManifest().version}`;
  const { settings } = await ext.storage.local.get('settings');
  state.settings = { ...DEFAULTS, ...(settings || {}) };
  $('folder').value = state.settings.folder;
  for (let n = 1; n <= MAX_WORKERS; n++) $('workers').add(new Option(String(n), String(n)));
  $('workers').value = String(state.settings.workers);
  $('use-login').checked = state.settings.useLogin;
  $('redownload').checked = state.settings.redownload;
  const fromUrl = new URLSearchParams(location.search).get('url');
  if (fromUrl) $('link').value = cleanUrl(fromUrl) || fromUrl;

  $('form').addEventListener('submit', (e) => { e.preventDefault(); start(); });
  for (const id of ['folder', 'workers', 'use-login', 'redownload']) $(id).addEventListener('change', saveSettings);
  $('perm-btn').addEventListener('click', () => {
    // Must be called directly in the click handler (browsers require a user gesture).
    ext.permissions.request({ origins: HOSTS }).then(async (granted) => {
      await checkPermissions();
      if (granted) refreshAccount();
    });
  });
  $('refresh-btn').addEventListener('click', refreshAccount);
  $('likes-btn').addEventListener('click', () => {
    if (state.account) { $('link').value = state.account.likesUrl; start(state.account.likesUrl); }
  });
  $('stop-btn').addEventListener('click', () => { state.abort?.abort(); setStatus('Stopping…'); });
  $('confirm-no').addEventListener('click', () => { $('confirm').hidden = true; setBusy(false, 'Cancelled.'); });
  $('confirm-yes').addEventListener('click', () => {
    $('confirm').hidden = true;
    runJob(state.job, state.job.tracks.map((_, i) => i + 1));
  });
  $('retry-btn').addEventListener('click', () => {
    if (state.busy || !state.failures.length) return;
    $('report').hidden = true;
    clearLog();
    log(`Retrying ${state.failures.length} track(s)…`);
    state.abort = new AbortController();
    runJob(state.job, state.failures.map((f) => f.index), { retry: true });
  });
  $('copy-btn').addEventListener('click', () => navigator.clipboard.writeText($('report-body').textContent));
  $('show-btn').addEventListener('click', () => {
    if (state.lastDownloadId != null) ext.downloads.show(state.lastDownloadId);
    else ext.downloads.showDefaultFolder();
  });

  setStatus(READY);
  if (await checkPermissions()) await refreshAccount();
  else $('account-text').textContent = 'Allow access to SoundCloud first.';
  $('link').focus();
}

init();
