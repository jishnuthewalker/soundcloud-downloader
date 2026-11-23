# SoundCloud Downloader

A small desktop app for Mac, Windows and Linux. Paste a SoundCloud link (track, playlist or profile) and it saves everything as MP3, with album art sized for DJ software. Optional sign-in adds your likes, private tracks and Go+ tracks.

**To install:** download `SoundCloud-Downloader.zip` from the [latest release](../../releases/latest) and follow *READ ME FIRST.txt*. No admin password needed.

## How it works

- Each setup file (`src/launcher_*`) installs a private copy of Python (via [uv](https://github.com/astral-sh/uv)), [scdl](https://github.com/scdl-org/scdl), yt-dlp and ffmpeg into a single per-user folder, then adds an app or shortcut.
- The app (`src/soundcloud_downloader.py`) checks for updates **every time it opens**:
  1. Updates itself from this repo's latest release and restarts. Downloads are checked against the checksums in `latest.json`.
  2. Updates its private copy of uv.
  3. Installs the newest Python 3.12 patch release.
  4. Upgrades scdl, yt-dlp, imageio-ffmpeg and all their dependencies.
- An outdated setup file fetches the newest one from the latest release before it runs.

## Releasing a new version

1. Edit `src/`.
2. Bump `__version__` in `src/soundcloud_downloader.py`.
3. Commit.
4. Run `python3 build.py --release`.

This needs the [GitHub CLI](https://cli.github.com/), logged in. Every installed copy picks up the new version the next time it's opened.

Anyone who can publish releases here can push code to every installed copy, so keep two-factor authentication on for this GitHub account.

`python3 build.py` on its own builds into `dist/` without publishing. To test updates locally, set `SCD_RELEASE_URL` to a local web server that serves `dist/release/`.
