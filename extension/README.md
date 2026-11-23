# SoundCloud Downloader — browser extension (Chrome + Firefox)

The desktop app's features, running entirely inside the browser:
- Download tracks, playlists and profile pages (tracks, popular tracks, albums, playlists, reposts, likes) as MP3.
- Write tags (title, artist, album, track number, genre, year, link) and album art shrunk to fit 1000×1000.
- Show a list of failed tracks with the reason, and retry just those.
- Skip tracks you've already downloaded.
- Sign in automatically using the browser's own SoundCloud login, including a **Download my likes** button.
- Start from the toolbar button, or right-click any SoundCloud link and choose *Download as MP3*.

No server is involved. The extension's host permissions let it call SoundCloud directly, and MP3 conversion uses the bundled ffmpeg.wasm (no code is loaded from the internet). Files are saved under the browser's Downloads folder, in `SoundCloud/` by default.

Not supported:
- Go+ tracks without a Go+ login (only a preview exists), and encrypted streams.
- The desktop app's "Fix album art in a folder", because browsers can't modify files already on disk.

## Develop

```bash
cd extension
npm install          # ffmpeg.wasm + Puppeteer (browser downloads are skipped; see below)
npm run build        # → ../dist/extension/{chrome,firefox}/ and store-ready zips
```

To load it by hand:
- **Chrome:** open `chrome://extensions`, turn on Developer mode, click **Load unpacked**, and choose `dist/extension/chrome`.
- **Firefox:** open `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on**, and choose `dist/extension/firefox/manifest.json`.

## Test

The tests drive test copies of Chrome and Firefox (never your own browsers) with throwaway profiles and download folders under `/tmp`:

```bash
npx @puppeteer/browsers install chrome@stable firefox@stable --path /tmp/scd-browsers
npm run build
node test/e2e.mjs chrome  [soundcloud-url]   # full download through the real UI, lists saved files
node test/e2e.mjs firefox [soundcloud-url]
node test/lookup.mjs      [url ...]          # link lookups for every page type, no audio downloaded
```

## Code map

| File | Contents |
|---|---|
| `src/lib/soundcloud.js` | SoundCloud client: finds the player's `client_id`, `/resolve`, pagination, playlist stubs, stream choice, HLS download. |
| `src/lib/media.js` | Cover resizing with the browser's canvas, the ffmpeg.wasm pool, and the single ffmpeg pass that converts to MP3 and adds tags and art. |
| `src/app.js` | The downloader page: settings, account, confirm, worker pool, saving files, report and retry. |
| `src/background.js` | The toolbar button and right-click menu, both of which open `app.html` in a tab. |
| `build.mjs` | Writes a separate `manifest.json` for each browser. Chrome gets a service worker; Firefox gets background scripts and `browser_specific_settings`. |

Things to know before changing the code:
- **The cover is resized by the browser, not ffmpeg.wasm.** ffmpeg.wasm's own image scaler hangs or crashes ("memory access out of bounds"), so ffmpeg only embeds the finished JPEG (`-c:v copy`).
- **`ffmpeg.writeFile` empties the data you pass it** (it hands the buffer over to its worker). Pass a copy if you need the data again.
- **Never use `audio/mpegurl` (`abr_*`) streams.** They're adaptive playlists that SoundCloud refuses to open for this kind of request. Only `audio/mpeg` streams count as MP3.

## License note

`@ffmpeg/core` is GPL-2.0-or-later. A published build bundles it, so the extension must be distributed under a GPL-compatible license, with its source available. This repo doesn't have a license yet.
