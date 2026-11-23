// Check that link lookups work for every kind of SoundCloud page, without downloading audio.
//   node test/lookup.mjs [url ...]
// Runs inside the built Chrome extension (so it uses the extension's own permissions).

import puppeteer from 'puppeteer';
import { readdir } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const base = join(process.env.SCD_BROWSERS || '/tmp/scd-browsers', 'chrome');
const exe = join(base, (await readdir(base)).sort().reverse()[0],
  'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing');
const urls = process.argv.slice(2).length ? process.argv.slice(2) : [
  'https://soundcloud.com/forss/flickermood',
  'https://soundcloud.com/the-concept-band/sets/the-royal-concept-ep',
  'https://soundcloud.com/forss',
  'https://soundcloud.com/forss/tracks',
  'https://soundcloud.com/forss/popular-tracks',
  'https://soundcloud.com/forss/sets',
  'https://soundcloud.com/forss/albums',
  'https://soundcloud.com/forss/reposts',
  'https://soundcloud.com/forss/likes',
  'm.soundcloud.com/forss/flickermood?si=abc&utm_source=x',
  'https://example.com/not-soundcloud',
];

const browser = await puppeteer.launch({
  browser: 'chrome', executablePath: exe, headless: true, pipe: true, protocolTimeout: 600000,
  enableExtensions: [resolve(ROOT, '..', 'dist', 'extension', 'chrome')],
});
const worker = await browser.waitForTarget((t) => t.type() === 'service_worker' && t.url().startsWith('chrome-extension://'));
const page = await browser.newPage();
await page.goto(`chrome-extension://${new URL(worker.url()).host}/app.html`);
for (const url of urls) {
  const result = await page.evaluate(async (raw) => {
    const sc = await import('./lib/soundcloud.js');
    const clean = sc.cleanUrl(raw);
    if (!clean) return 'rejected (not a SoundCloud link)';
    const t0 = performance.now();
    try {
      const job = await sc.lookup(new sc.Client(), clean);
      const usable = job.tracks.filter((t) => !t.unavailable && sc.pickTranscoding(t)).length;
      return `${clean} → "${job.title}" folder="${job.folder}" tracks=${job.tracks.length} `
        + `(streamable now: ${usable}) ${Math.round(performance.now() - t0)}ms`;
    } catch (e) {
      return `${clean} → ERROR ${e.code || ''} ${e.message}`;
    }
  }, url);
  console.log(result);
}
await browser.close();
