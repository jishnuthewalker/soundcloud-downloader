// SoundCloud API client for the extension pages.
//
// Extension pages may fetch any site listed in host_permissions without the cross-site
// (CORS) restrictions a normal web page has, which is what makes this work without a server.
// This is a small re-implementation of what yt-dlp/scdl do for SoundCloud in the desktop app;
// if SoundCloud changes its API, this file is where fixes go.

const API = 'https://api-v2.soundcloud.com';
const CLIENT_ID_MAX_AGE = 12 * 60 * 60 * 1000;
const ext = globalThis.browser ?? globalThis.chrome;

export class SoundCloudError extends Error {
  constructor(message, { code = 'error', status = 0 } = {}) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

// ---------------------------------------------------------------- client_id

let clientIdPromise = null;

async function scrapeClientId() {
  let html;
  try {
    // credentials: SoundCloud's bot protection may need the cookies the user already has.
    html = await (await fetch('https://soundcloud.com/', { credentials: 'include' })).text();
  } catch {
    throw new SoundCloudError("Couldn't reach SoundCloud. Check your internet connection.", { code: 'network' });
  }
  const scripts = [...html.matchAll(/<script[^>]+src="(https:\/\/a-v2\.sndcdn\.com\/assets\/[^"]+\.js)"/g)]
    .map((m) => m[1]);
  for (const src of scripts.reverse()) {          // the client_id is usually in one of the last bundles
    try {
      const js = await (await fetch(src)).text();
      const m = js.match(/client_id\s*:\s*"([0-9a-zA-Z]{32})"/) || js.match(/client_id=([0-9a-zA-Z]{32})/);
      if (m) return m[1];
    } catch { /* try the next bundle */ }
  }
  throw new SoundCloudError(
    "Couldn't connect to SoundCloud. Open soundcloud.com in this browser once (solve any check it shows), " +
    'then try again.', { code: 'clientid' });
}

export async function getClientId({ refresh = false } = {}) {
  if (!refresh) {
    const { clientId, clientIdAt } = await ext.storage.local.get(['clientId', 'clientIdAt']);
    if (clientId && Date.now() - (clientIdAt || 0) < CLIENT_ID_MAX_AGE) return clientId;
  }
  clientIdPromise ??= scrapeClientId()
    .then(async (id) => { await ext.storage.local.set({ clientId: id, clientIdAt: Date.now() }); return id; })
    .finally(() => { clientIdPromise = null; });
  return clientIdPromise;
}

// ---------------------------------------------------------------- API

function httpError(status, signedIn) {
  if (status === 429) {
    return new SoundCloudError('Rate-limited by SoundCloud. Wait a few minutes and retry, or lower parallel downloads.',
      { code: 'rate', status });
  }
  if (status === 404) {
    return new SoundCloudError(signedIn ? 'Private or removed from SoundCloud.'
      : "Private or removed from SoundCloud. If it's private and you have access, sign in first.",
    { code: 'notfound', status });
  }
  if (status === 401 || status === 403) {
    return new SoundCloudError(signedIn
      ? 'SoundCloud refused access. Your login may have expired: sign in again at soundcloud.com, then click Refresh.'
      : "SoundCloud refused access (private, or not available in your region).", { code: 'auth', status });
  }
  return new SoundCloudError(`SoundCloud error (HTTP ${status}). Retry may fix it.`, { code: 'http', status });
}

export class Client {
  constructor({ token = null } = {}) {
    this.token = token;
  }

  async get(pathOrUrl, params = {}, { signal } = {}) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const url = new URL(pathOrUrl.startsWith('http') ? pathOrUrl : API + pathOrUrl);
      for (const [k, v] of Object.entries(params)) if (v != null) url.searchParams.set(k, v);
      url.searchParams.set('client_id', await getClientId({ refresh: attempt > 0 }));
      const headers = { Accept: 'application/json' };
      if (this.token) headers.Authorization = `OAuth ${this.token}`;
      let res;
      try {
        res = await fetch(url, { headers, credentials: 'omit', signal });
      } catch (e) {
        if (e.name === 'AbortError') throw e;
        throw new SoundCloudError('Network problem. Retry should fix it.', { code: 'network' });
      }
      if (res.ok) return res.json();
      // A stale client_id also gives 401/403: refresh it once before giving up.
      if ((res.status === 401 || res.status === 403) && attempt === 0) continue;
      throw httpError(res.status, Boolean(this.token));
    }
  }

  resolve(url, opts) { return this.get('/resolve', { url }, opts); }
  me(opts) { return this.get('/me', {}, opts); }

  async collect(path, params = {}, { signal, limit = Infinity } = {}) {
    const items = [];
    let next = path;
    let query = { limit: 200, linked_partitioning: 1, ...params };
    while (next && items.length < limit) {
      const page = await this.get(next, query, { signal });
      items.push(...(page.collection || []));
      next = page.next_href || null;
      query = {};                                    // next_href already carries the query
    }
    return items;
  }

  // Playlists list only the first few tracks in full; the rest are {id} stubs.
  async fillTracks(tracks, { signal } = {}) {
    const missing = tracks.filter((t) => t && !t.media).map((t) => t.id);
    const full = new Map();
    for (let i = 0; i < missing.length; i += 50) {
      const batch = await this.get('/tracks', { ids: missing.slice(i, i + 50).join(',') }, { signal });
      for (const t of batch) full.set(t.id, t);
    }
    return tracks.filter(Boolean).map((t) => (t.media ? t : full.get(t.id) || { ...t, unavailable: true }));
  }

  async playlistTracks(playlist, opts) {
    let tracks = playlist.tracks;
    if (!tracks) tracks = (await this.get(`/playlists/${playlist.id}`, {}, opts)).tracks || [];
    return this.fillTracks(tracks, opts);
  }
}

// ---------------------------------------------------------------- links

const USER_PAGES = {
  '': { label: 'All tracks and reposts', path: (id) => `/stream/users/${id}` },
  tracks: { label: 'Tracks', path: (id) => `/users/${id}/tracks` },
  'popular-tracks': { label: 'Popular tracks', path: (id) => `/users/${id}/toptracks` },
  albums: { label: 'Albums', path: (id) => `/users/${id}/albums` },
  sets: { label: 'Playlists', path: (id) => `/users/${id}/playlists_without_albums` },
  reposts: { label: 'Reposts', path: (id) => `/stream/users/${id}/reposts` },
  likes: { label: 'Likes', path: (id) => `/users/${id}/likes` },
  spotlight: { label: 'Spotlight', path: (id) => `/users/${id}/spotlight` },
  comments: { label: 'Commented tracks', path: (id) => `/users/${id}/comments` },
};

/** Tidy a pasted link (adds https://, drops tracking parameters). Returns null if it isn't SoundCloud. */
export function cleanUrl(raw) {
  let text = (raw || '').trim();
  if (!text) return null;
  if (!/^https?:\/\//i.test(text)) text = `https://${text}`;
  let u;
  try { u = new URL(text); } catch { return null; }
  const host = u.hostname.toLowerCase();
  if (host !== 'soundcloud.com' && !host.endsWith('.soundcloud.com')) return null;
  const path = u.pathname.replace(/\/+$/, '');
  if (!path) return null;
  return `https://${host === 'm.soundcloud.com' ? 'soundcloud.com' : host}${path}`;
}

function userPage(url) {
  const parts = new URL(url).pathname.split('/').filter(Boolean);
  if (parts.length === 2 && parts[1] in USER_PAGES) {
    return { profile: `https://soundcloud.com/${parts[0]}`, page: parts[1] };
  }
  return null;
}

/** Share links (https://on.soundcloud.com/AbC123) redirect to the real address; follow them. */
export async function expandShortUrl(url, { signal } = {}) {
  if (new URL(url).hostname.toLowerCase() !== 'on.soundcloud.com') return url;
  try {
    const res = await fetch(url, { credentials: 'omit', signal });
    const full = cleanUrl(res.url);
    return full && new URL(full).hostname !== 'on.soundcloud.com' ? full : url;
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    return url;
  }
}

/** Work out what a link points to. Returns {title, by, folder, album, playlist, tracks}. */
export async function lookup(client, url, { signal } = {}) {
  url = await expandShortUrl(url, { signal });
  const page = userPage(url);
  const obj = await client.resolve(page ? page.profile : url, { signal });
  if (obj.kind === 'track') {
    return { url, title: obj.title, by: obj.user?.username || '', folder: '', album: '', playlist: false, tracks: [obj] };
  }
  if (obj.kind === 'playlist') {
    return {
      url, title: obj.title, by: obj.user?.username || '', folder: obj.title, album: obj.title,
      playlist: true, tracks: await client.playlistTracks(obj, { signal }),
    };
  }
  if (obj.kind === 'user') {
    const which = USER_PAGES[page ? page.page : ''];
    const items = await client.collect(which.path(obj.id), {}, { signal });
    const tracks = [];
    const seen = new Set();
    const add = (t) => { if (t && !seen.has(t.id)) { seen.add(t.id); tracks.push(t); } };
    for (const item of items) {
      const track = item.kind === 'track' ? item : item.track;
      const playlist = item.kind === 'playlist' ? item : item.playlist;
      if (track) add(track);
      else if (playlist) (await client.playlistTracks(playlist, { signal })).forEach(add);
    }
    const title = `${obj.username} · ${which.label}`;
    return {
      url, title, by: obj.username, folder: `${obj.username} - ${which.label}`, album: '',
      playlist: true, tracks: await client.fillTracks(tracks, { signal }),
    };
  }
  throw new SoundCloudError("That link isn't a track, playlist or profile.", { code: 'unsupported' });
}

// ---------------------------------------------------------------- audio

const isEncrypted = (t) => /encrypted/i.test(t.format?.protocol || '');
const mimeOf = (t) => (t.format?.mime_type || '').toLowerCase();
export const isMp3Mime = (mime) => /^audio\/mpeg(;|$)/i.test(mime || '');
// "audio/mpegurl" transcodings (preset abr_*) are adaptive master playlists that this
// client_id can't open, so they're never used.
const isAdaptive = (t) => mimeOf(t).includes('mpegurl') || /^abr_/.test(t.preset || '');
const QUALITY = { hq: 200, sq: 100, lq: 0 };      // hq = Go+ high quality

export function pickTranscoding(track) {
  const usable = (track.media?.transcodings || [])
    .filter((t) => !t.snipped && !isEncrypted(t) && !isAdaptive(t));
  const score = (t) => {
    const mime = mimeOf(t);
    let s = QUALITY[t.quality] ?? 50;
    if (isMp3Mime(mime)) s += 30;                  // MP3 needs no re-encoding
    else if (mime.includes('mp4') || mime.includes('aac')) s += 20;
    else if (mime.includes('ogg') || mime.includes('opus')) s += 10;
    if (t.format?.protocol === 'progressive') s += 5;
    return s;
  };
  return usable.sort((a, b) => score(b) - score(a))[0] || null;
}

function noStreamReason(track, signedIn) {
  if (track.unavailable) return 'Private or removed from SoundCloud.';
  const all = track.media?.transcodings || [];
  if (track.policy === 'BLOCK') return 'Blocked in your region.';
  if (track.policy === 'SNIP' || (all.length && all.every((t) => t.snipped))) {
    return signedIn ? "Go+ only track, and this account doesn't have Go+."
      : 'Go+ only track (only a preview is available). Signing in with a Go+ account can fix this.';
  }
  if (all.length && all.every((t) => isEncrypted(t) || isAdaptive(t))) return "This track is protected (encrypted) and can't be downloaded.";
  return 'No downloadable stream for this track.';
}

async function fetchOk(url, signal) {
  let res;
  try {
    res = await fetch(url, { credentials: 'omit', signal });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new SoundCloudError('Network problem. Retry should fix it.', { code: 'network' });
  }
  if (!res.ok) throw httpError(res.status, false);
  return res;
}

async function fetchHls(url, signal, depth = 0) {
  const text = await (await fetchOk(url, signal)).text();
  const lines = text.split(/\r?\n/).map((l) => l.trim());
  if (lines.some((l) => l.startsWith('#EXT-X-STREAM-INF')) && depth === 0) {
    const variant = lines.find((l) => l && !l.startsWith('#'));
    return fetchHls(new URL(variant, url).href, signal, 1);
  }
  if (lines.some((l) => l.startsWith('#EXT-X-KEY') && !/METHOD=NONE/.test(l))) {
    throw new SoundCloudError("This track is protected (encrypted) and can't be downloaded.", { code: 'encrypted' });
  }
  const parts = [];
  for (const line of lines) {
    if (line.startsWith('#EXT-X-MAP:')) {
      const m = line.match(/URI="([^"]+)"/);
      if (m) parts.push(new URL(m[1], url).href);
    } else if (line && !line.startsWith('#')) {
      parts.push(new URL(line, url).href);
    }
  }
  const chunks = new Array(parts.length);
  let next = 0;
  const worker = async () => {
    while (next < parts.length) {
      const i = next++;
      chunks[i] = new Uint8Array(await (await fetchOk(parts[i], signal)).arrayBuffer());
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, parts.length) }, worker));
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) { out.set(c, offset); offset += c.length; }
  return out;
}

/** Download a track's audio. Returns {data: Uint8Array, mime}. */
export async function fetchAudio(client, track, { signal } = {}) {
  const tc = track.unavailable ? null : pickTranscoding(track);
  if (!tc) throw new SoundCloudError(noStreamReason(track, Boolean(client.token)), { code: 'nostream' });
  const { url } = await client.get(tc.url, { track_authorization: track.track_authorization }, { signal });
  const mime = tc.format?.mime_type || 'audio/mpeg';
  const data = tc.format?.protocol === 'progressive'
    ? new Uint8Array(await (await fetchOk(url, signal)).arrayBuffer())
    : await fetchHls(url, signal);
  return { data, mime };
}

/** Best available cover image bytes (original size if possible), or null. */
export async function fetchCover(track, { signal } = {}) {
  const base = track.artwork_url || track.user?.avatar_url;
  if (!base || /default_avatar/.test(base)) return null;
  for (const size of ['original', 't500x500', 'large']) {
    try {
      const res = await fetch(base.replace(/-(large|t\d+x\d+|crop|original)(?=\.\w+$)/, `-${size}`),
        { credentials: 'omit', signal });
      if (res.ok) return new Uint8Array(await res.arrayBuffer());
    } catch (e) {
      if (e.name === 'AbortError') throw e;
    }
  }
  return null;
}
