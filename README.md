# SoundCloud Downloader

A small desktop app for Mac, Windows and Linux. Paste a SoundCloud link (track, playlist or profile) and it saves everything as MP3, with album art sized for DJ software. Optional sign-in adds your likes, private tracks and Go+ tracks.

## Install

No admin password needed, and no security warnings with these one-liners.

**Mac:** open Terminal (press ⌘ Space, type *Terminal*, press Return), paste this line and press Return:

```
curl -fsSL https://raw.githubusercontent.com/jishnuthewalker/soundcloud-downloader/main/install.sh | bash
```

**Windows:** press **Win+R**, paste this line and press Enter:

```
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/jishnuthewalker/soundcloud-downloader/main/install.ps1 | iex"
```

**Linux:** paste the Mac line into a terminal.

When it says *All set!*, the app opens. After that, open **SoundCloud Downloader** like any other app. It keeps itself up to date.

**Prefer a download?** Get `SoundCloud-Downloader.zip` from the [latest release](../../releases/latest) and follow *READ ME FIRST.txt* inside it.

## Lossless FLAC from Soulseek (optional, desktop app)

Turn on **Also look for lossless FLACs on Soulseek** and enter a Soulseek login. A new username creates an account automatically. Every track is then also searched on Soulseek, and a matching FLAC is saved in a `FLAC` folder next to the MP3s.

A file only counts as a match when its name, artist and length agree with the SoundCloud track, and remixes are never swapped for originals. While the app is open it shares your download folder (this can be turned off).

## Browser extension (in development)

`extension/` contains a Chrome + Firefox version of the same features. Nothing to install besides the extension, and sign-in uses your browser's SoundCloud login. It isn't published yet. See [extension/README.md](extension/README.md).

## How it works

- Each setup file (`src/launcher_*`) installs a private copy of Python (via [uv](https://github.com/astral-sh/uv)), [scdl](https://github.com/scdl-org/scdl), yt-dlp and ffmpeg into a single per-user folder, then adds an app or shortcut.
- The app (`src/soundcloud_downloader.py`) checks for updates **every time it opens**:
  1. Updates itself from this repo's latest release and restarts. Downloads are checked against the checksums in `latest.json`.
  2. Updates its private copy of uv.
  3. Installs the newest Python 3.12 patch release.
  4. Upgrades scdl, yt-dlp, imageio-ffmpeg and all their dependencies.
- An outdated setup file fetches the newest one from the latest release before it runs.
- `install.sh` / `install.ps1` are what the one-liners run. They download the newest setup file from the latest release, check it against `latest.json`, and run it.

## Releasing a new version

1. Edit `src/`.
2. Bump `__version__` in `src/soundcloud_downloader.py`.
3. Commit.
4. Run `python3 build.py --release`.

This needs the [GitHub CLI](https://cli.github.com/), logged in. Every installed copy picks up the new version the next time it's opened.

Anyone who can publish releases here can push code to every installed copy, so keep two-factor authentication on for this GitHub account.

`python3 build.py` on its own builds into `dist/` without publishing. To test updates locally, set `SCD_RELEASE_URL` to a local web server that serves `dist/release/`.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)). The app uses aioslsk (GPL-3.0) and the browser extension bundles ffmpeg.wasm (GPL-2.0-or-later).
