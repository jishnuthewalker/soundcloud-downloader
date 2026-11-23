// End-to-end test: load the built extension into a test copy of Chrome or Firefox,
// download a SoundCloud link through the real UI, and list the files that were saved.
//
//   npx @puppeteer/browsers install chrome@stable firefox@stable --path /tmp/scd-browsers
//   npm run build
//   node test/e2e.mjs chrome [soundcloud-url]
//   node test/e2e.mjs firefox [soundcloud-url]
//
// Uses throwaway profiles and download folders under /tmp; never touches your own browsers.

import puppeteer from 'puppeteer';
import { appendFile, cp, mkdir, mkdtemp, readdir, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const which = process.argv[2] || 'chrome';
const link = process.argv[3] || 'https://soundcloud.com/forss/flickermood';
const browsersDir = process.env.SCD_BROWSERS || '/tmp/scd-browsers';
const extDir = resolve(ROOT, '..', 'dist', 'extension', which);
if (!existsSync(join(extDir, 'manifest.json'))) throw new Error(`Build first: ${extDir} is missing`);

async function findExecutable(name) {
  const base = join(browsersDir, name);
  const versions = (await readdir(base)).sort().reverse();
  const rel = name === 'chrome'
    ? 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
    : 'Firefox.app/Contents/MacOS/firefox';
  return join(base, versions[0], rel);
}

async function listFiles(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true, recursive: true })) {
    if (entry.isFile()) out.push(relative(dir, join(entry.parentPath ?? entry.path, entry.name)));
  }
  return out.sort();
}

const downloads = await mkdtemp('/tmp/scd-dl-');
let browser;
let appUrl;
let page;
if (which === 'chrome') {
  const profile = await mkdtemp('/tmp/scd-profile-');
  await mkdir(join(profile, 'Default'), { recursive: true });
  await writeFile(join(profile, 'Default', 'Preferences'),
    JSON.stringify({ download: { default_directory: downloads, prompt_for_download: false } }));
  browser = await puppeteer.launch({
    browser: 'chrome', executablePath: await findExecutable('chrome'), headless: true, pipe: true,
    enableExtensions: [extDir], userDataDir: profile,
  });
  const worker = await browser.waitForTarget(
    (t) => t.type() === 'service_worker' && t.url().startsWith('chrome-extension://'), { timeout: 20000 });
  appUrl = `chrome-extension://${new URL(worker.url()).host}/app.html`;
} else {
  const id = 'soundcloud-downloader@jishnuthewalker';
  const uuid = '5c0b2f9a-1111-4222-8333-123456789abc';
  browser = await puppeteer.launch({
    browser: 'firefox', executablePath: await findExecutable('firefox'), headless: true,
    // Lets automation script moz-extension:// pages (this throwaway test browser only).
    env: { ...process.env, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS: '1' },
    extraPrefsFirefox: {
      'extensions.webextensions.uuids': JSON.stringify({ [id]: uuid }),
      'browser.download.dir': downloads,
      'browser.download.folderList': 2,
      'browser.download.useDownloadDir': true,
      'browser.download.always_ask_before_handling_new_types': false,
      'browser.download.start_downloads_in_tmp_dir': false,
    },
  });
  // WebDriver BiDi refuses to navigate to moz-extension:// pages, so install a throwaway copy
  // whose background script opens the app tab itself, then attach to that tab.
  const testExt = await mkdtemp('/tmp/scd-ffext-');
  await cp(extDir, testExt, { recursive: true });
  await appendFile(join(testExt, 'background.js'),
    `\next.tabs.create({ url: ext.runtime.getURL('app.html?url=${encodeURIComponent(link)}') });\n`);
  appUrl = `moz-extension://${uuid}/app.html`;
  await browser.installExtension(testExt);
  // BiDi reports extension tabs as about:blank, so find the tab by asking each page where it is.
  for (let tries = 0; !page && tries < 60; tries++) {
    for (const p of await browser.pages()) {
      const href = await p.evaluate(() => location.href).catch(() => '');
      if (href.startsWith(appUrl)) { page = p; break; }
    }
    if (!page) await new Promise((r) => setTimeout(r, 500));
  }
  if (!page) throw new Error('The extension page never opened in Firefox');
}

page ??= await browser.newPage();
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));
if (which === 'chrome') await page.goto(`${appUrl}?url=${encodeURIComponent(link)}`);
await page.waitForFunction(() => document.getElementById('account-text').textContent !== 'Checking…', { timeout: 30000 });

const text = (sel) => page.$eval(sel, (el) => el.textContent.trim());
const hidden = (sel) => page.$eval(sel, (el) => el.hidden);
console.log(`[${which}] page:`, appUrl);
console.log(`[${which}] permission banner shown:`, !(await hidden('#perm-banner')));
console.log(`[${which}] account:`, await text('#account-text'));

const started = Date.now();
await page.click('#go-btn');
await page.waitForFunction(() => !document.getElementById('confirm').hidden || !document.getElementById('go-btn').disabled,
  { timeout: 120000 });
if (await hidden('#confirm')) {
  console.log(`[${which}] lookup failed:`, await text('#status'));
} else {
  console.log(`[${which}] confirm:`, await text('#confirm-title'), '|', await text('#confirm-body'));
  await page.click('#confirm-yes');
  await page.waitForFunction(() => document.getElementById('stop-btn').hidden && !document.getElementById('go-btn').disabled,
    { timeout: 15 * 60 * 1000 });
  console.log(`[${which}] status:`, await text('#status'), `(${((Date.now() - started) / 1000).toFixed(1)}s)`);
  console.log(`[${which}] log:\n${await text('#log')}`);
  if (!(await hidden('#report'))) console.log(`[${which}] report:\n${await text('#report-body')}`);
}
await new Promise((r) => setTimeout(r, 1500));     // let the last download finish writing
console.log(`[${which}] files in ${downloads}:`);
for (const f of await listFiles(downloads)) console.log('   ', f);
if (consoleErrors.length) console.log(`[${which}] console errors:\n  ${consoleErrors.join('\n  ')}`);
await browser.close();
console.log(`DOWNLOADS=${downloads}`);
