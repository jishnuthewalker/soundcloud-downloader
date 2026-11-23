// Build the Chrome and Firefox versions of the extension into ../dist/extension/.
//
//   npm install          (once; installs ffmpeg.wasm and the test tools)
//   npm run build
//
// Output: dist/extension/chrome/ and dist/extension/firefox/ (load these unpacked to test),
// plus zips ready for the Chrome Web Store / addons.mozilla.org.

import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(fileURLToPath(import.meta.url));
const SRC = join(ROOT, 'src');
const OUT = join(ROOT, '..', 'dist', 'extension');
const pkg = JSON.parse(await readFile(join(ROOT, 'package.json'), 'utf8'));

const HOSTS = ['https://soundcloud.com/*', 'https://*.soundcloud.com/*', 'https://*.sndcdn.com/*',
  'https://*.soundcloud.cloud/*'];

function manifest(browser) {
  const m = {
    manifest_version: 3,
    name: 'SoundCloud Downloader',
    version: pkg.version,
    description: 'Download SoundCloud tracks, playlists and profiles as MP3, with tags and album art.',
    icons: { 16: 'icons/icon-16.png', 32: 'icons/icon-32.png', 48: 'icons/icon-48.png', 128: 'icons/icon-128.png' },
    action: {
      default_title: 'SoundCloud Downloader',
      default_icon: { 16: 'icons/icon-16.png', 32: 'icons/icon-32.png' },
    },
    permissions: ['downloads', 'cookies', 'storage', 'contextMenus'],
    host_permissions: HOSTS,
    // ffmpeg.wasm needs WebAssembly; no remote code is ever loaded.
    content_security_policy: { extension_pages: "script-src 'self' 'wasm-unsafe-eval'; object-src 'self'" },
  };
  if (browser === 'chrome') {
    m.background = { service_worker: 'background.js' };
    m.minimum_chrome_version = '116';
  } else {
    m.background = { scripts: ['background.js'] };
    m.browser_specific_settings = {
      gecko: {
        id: 'soundcloud-downloader@jishnuthewalker',
        strict_min_version: '128.0',
        data_collection_permissions: { required: ['none'] },
      },
    };
  }
  return m;
}

for (const browser of ['chrome', 'firefox']) {
  const dir = join(OUT, browser);
  await rm(dir, { recursive: true, force: true });
  await mkdir(dir, { recursive: true });
  await cp(SRC, dir, { recursive: true });
  await cp(join(ROOT, 'node_modules/@ffmpeg/ffmpeg/dist/esm'), join(dir, 'vendor/ffmpeg'), {
    recursive: true, filter: (p) => !p.endsWith('.d.ts') && !p.endsWith('.d.mts'),
  });
  await mkdir(join(dir, 'vendor/core'), { recursive: true });
  for (const f of ['ffmpeg-core.js', 'ffmpeg-core.wasm']) {
    await cp(join(ROOT, 'node_modules/@ffmpeg/core/dist/esm', f), join(dir, 'vendor/core', f));
  }
  await writeFile(join(dir, 'manifest.json'), `${JSON.stringify(manifest(browser), null, 2)}\n`);
  const zip = join(OUT, `soundcloud-downloader-${browser}-${pkg.version}.zip`);
  await rm(zip, { force: true });
  execFileSync('zip', ['-qr', '-X', zip, '.'], { cwd: dir });
  console.log(`built ${browser}: dist/extension/${browser}/ and ${zip.split('/dist/')[1]}`);
}
