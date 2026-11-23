// MP3 conversion, tags and album art (ffmpeg.wasm is bundled, never loaded from the internet).
// The browser shrinks the cover to fit MAX_ART x MAX_ART and re-encodes it as a baseline JPEG
// (ffmpeg.wasm's own scaler hangs/crashes), then one ffmpeg pass converts to MP3 (or copies an
// MP3 stream as-is), writes ID3v2.3 tags and embeds the JPEG, which rekordbox reads reliably.

import { FFmpeg } from '../vendor/ffmpeg/index.js';
import { isMp3Mime } from './soundcloud.js';

const ext = globalThis.browser ?? globalThis.chrome;
export const MAX_ART = 1000;
const MP3_QUALITY = '2';           // LAME VBR: 0 = best/largest, 2 ≈ 190 kbps (same as the desktop app)

async function newFFmpeg() {
  const ff = new FFmpeg();
  await ff.load({
    coreURL: ext.runtime.getURL('vendor/core/ffmpeg-core.js'),
    wasmURL: ext.runtime.getURL('vendor/core/ffmpeg-core.wasm'),
  });
  return ff;
}

/** A few ffmpeg instances shared by the download workers (each one uses a lot of memory). */
export class FFmpegPool {
  constructor(size) {
    this.size = Math.max(1, size);
    this.idle = [];
    this.created = 0;
    this.waiting = [];
  }

  async acquire() {
    if (this.idle.length) return this.idle.pop();
    if (this.created < this.size) {
      this.created++;
      try {
        return await newFFmpeg();
      } catch (e) {
        this.created--;
        throw e;
      }
    }
    return new Promise((resolve) => this.waiting.push(resolve));
  }

  release(ff, broken = false) {
    if (broken) {
      try { ff.terminate(); } catch { /* already gone */ }
      this.created--;
      ff = null;
    }
    const next = this.waiting.shift();
    if (next) {
      if (ff) next(ff);
      else this.acquire().then(next);
    } else if (ff) {
      this.idle.push(ff);
    }
  }

  async run(fn) {
    const ff = await this.acquire();
    let broken = false;
    try {
      return await fn(ff);
    } catch (e) {
      broken = true;            // start the next job with a fresh instance
      throw e;
    } finally {
      this.release(ff, broken);
    }
  }

  terminate() {
    for (const ff of this.idle) { try { ff.terminate(); } catch { /* ignore */ } }
    this.idle = [];
    this.created = 0;
  }
}

function inputName(mime) {
  if (isMp3Mime(mime)) return 'in.mp3';
  if (mime.includes('ogg') || mime.includes('opus')) return 'in.ogg';
  return 'in.m4a';
}

/** Shrink a cover image to fit MAX_ART x MAX_ART and return it as JPEG bytes (null if unreadable). */
export async function prepareCover(bytes) {
  if (!bytes) return null;
  try {
    const bitmap = await createImageBitmap(new Blob([bytes]));
    const scale = Math.min(1, MAX_ART / Math.max(bitmap.width, bitmap.height));
    const w = Math.max(1, Math.round(bitmap.width * scale));
    const h = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = new OffscreenCanvas(w, h);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff';                  // transparent PNG covers get a white background
    ctx.fillRect(0, 0, w, h);
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(bitmap, 0, 0, w, h);
    bitmap.close();
    // Below 0.9 Firefox also uses 4:2:0 chroma (like Chrome and the desktop app), the safest for rekordbox.
    const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.88 });
    return new Uint8Array(await blob.arrayBuffer());
  } catch {
    return null;
  }
}

/**
 * Build the final MP3. `cover` must already be a JPEG from prepareCover(). tags: {title, artist, album, genre, date, track, comment}.
 * Returns {data: Uint8Array, coverDropped: boolean}.
 */
export async function makeMp3(ff, { audio, mime, cover, tags }) {
  const input = inputName(mime);
  const copyAudio = isMp3Mime(mime);
  await ff.writeFile(input, audio);
  if (cover) await ff.writeFile('cover', cover);

  const build = (withCover) => {
    const args = ['-hide_banner', '-loglevel', 'error', '-y', '-i', input];
    if (withCover) args.push('-i', 'cover');
    args.push('-map', '0:a:0');
    if (withCover) {
      args.push('-map', '1:v:0', '-c:v', 'copy', '-disposition:v:0', 'attached_pic',
        '-metadata:s:v', 'title=Album cover', '-metadata:s:v', 'comment=Cover (front)');
    }
    args.push(...(copyAudio ? ['-c:a', 'copy'] : ['-c:a', 'libmp3lame', '-q:a', MP3_QUALITY]));
    args.push('-map_metadata', '-1');
    for (const [k, v] of Object.entries(tags)) if (v) args.push('-metadata', `${k}=${v}`);
    args.push('-id3v2_version', '3', '-write_id3v1', '1', '-f', 'mp3', 'out.mp3');
    return args;
  };

  const errors = [];
  const onLog = ({ message }) => errors.push(message);
  ff.on('log', onLog);
  try {
    let code = await ff.exec(build(Boolean(cover)));
    let coverDropped = false;
    if (code !== 0 && cover) {            // an unusual cover format: keep the song, skip the art
      errors.length = 0;
      code = await ff.exec(build(false));
      coverDropped = true;
    }
    if (code !== 0) {
      throw new Error(`Couldn't convert to MP3: ${errors.filter(Boolean).slice(-1)[0] || `ffmpeg exit ${code}`}`);
    }
    return { data: await ff.readFile('out.mp3'), coverDropped };
  } finally {
    ff.off('log', onLog);
    for (const f of [input, 'cover', 'out.mp3']) {
      try { await ff.deleteFile(f); } catch { /* not created */ }
    }
  }
}

// ---------------------------------------------------------------- names & tags

const RESERVED = /^(con|prn|aux|nul|com[0-9]|lpt[0-9])$/i;

/** A file/folder name that's valid on Windows, macOS and Linux and accepted by both browsers. */
export function safeName(name, fallback = 'untitled') {
  let s = String(name || '')
    .replace(/[\\/:*?"<>|\u0000-\u001f\u007f]/g, '_')
    .replace(/[~]/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^[.\s]+|[.\s]+$/g, '');
  if (s.length > 120) s = s.slice(0, 120).trim().replace(/[.\s]+$/, '');
  if (!s || RESERVED.test(s.split('.')[0])) s = s ? `_${s}` : fallback;
  return s;
}

/** "Artist - Title" unless the title already contains a dash (like scdl --addtofile). */
export function trackName(track) {
  const title = track.title || `Track ${track.id}`;
  const artist = track.user?.username || '';
  return /\s[-–—]\s/.test(title) || !artist ? title : `${artist} - ${title}`;
}

export function trackTags(track, { album = '', index = null, total = null } = {}) {
  const date = track.release_date || track.display_date || track.created_at || '';
  return {
    title: track.title || '',
    artist: track.user?.username || '',
    album: album || '',
    genre: track.genre || '',
    date: date ? date.slice(0, 4) : '',
    track: index ? (total ? `${index}/${total}` : String(index)) : '',
    comment: track.permalink_url || '',
  };
}
