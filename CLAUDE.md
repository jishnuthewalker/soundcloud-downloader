# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Tkinter desktop app (`src/soundcloud_downloader.py`) that wraps scdl/yt-dlp to download SoundCloud links as MP3. It is distributed as one-file setup launchers for Mac, Windows and Linux, plus one-line installers. Every installed copy updates itself from this repo's GitHub Releases each time it opens, so **publishing a release ships code to every user**.

## Commands

```bash
python3 build.py              # regenerate dist/ (launchers, zip, release assets, latest.json)
python3 build.py --release    # build, push HEAD, publish dist/release/* as GitHub release v<__version__>
                              # (needs a clean git tree, gh logged in as jishnuthewalker, a version not yet released)
python3 -m py_compile src/soundcloud_downloader.py
bash -n src/launcher_mac.command src/launcher_linux.sh install.sh   # syntax-check shell parts
python src/soundcloud_downloader.py            # run the app directly (needs tkinter + scdl; it won't self-update)
python src/soundcloud_downloader.py --setup    # what launchers call: draw icons, create the Mac .app / Linux .desktop
```

There is no test suite. Lint with pyflakes from a scratch venv (Homebrew's Python is externally managed, so don't `pip install` into it).

Don't run `bash -n` on the built files in `dist/`. Bash would also parse the embedded Python after `exit 0` and report a false syntax error.

### Testing installs and updates safely

Never run a launcher or installer against the real home folder. These overrides exist for testing:

- `HOME=/tmp/<sandbox>/home` keeps the app folder, settings and browser lookups inside the sandbox.
- `SCD_APPLICATIONS_DIR=/tmp/<sandbox>/Apps` stops the Mac `--setup` from writing to `/Applications`.
- `SCD_RELEASE_URL=http://127.0.0.1:<port>` serves a locally built `dist/release/` (for example via `python3 -m http.server`). The app, the launchers and both install scripts read it. To test an update, build a copy of the project with a bumped `__version__` and serve that.
- `SCD_SKIP_SELF_UPDATE=1` makes a launcher run as-is instead of switching to the newest published setup file.

Only the Mac path has been run end-to-end. Windows (`.bat`/PowerShell, `install.ps1`) and Linux have not been executed on real machines.

## Architecture

**Single source, embedded app.** `src/soundcloud_downloader.py` is the whole app. `build.py` builds each launcher by concatenating a launcher head (`src/launcher_*`, which must end with a `##PY-START` line) with the app source. At install time the launcher extracts everything after `##PY-START` into the app folder.

`build.py` also does the following:
- Substitutes `@VERSION@`, `@REPO@` and `@PYTHON@` from the app's `__version__`, `REPO` and `PYTHON_MINOR` constants.
- Converts the Windows `.bat` to CRLF line endings.
- Checks that `install.sh` and `install.ps1` contain the current `REPO` (they hardcode it).

Edit `src/`, never `dist/`.

**Release contract.** These asset names are hardcoded in the app, the launchers and the install scripts; renaming one breaks updates for existing installs:
- `latest.json` (`{"version", "files": {asset: sha256}}`)
- `soundcloud_downloader.py`
- `setup-mac.command`, `setup-windows.bat`, `setup-linux.sh`
- `SoundCloud-Downloader.zip`

Everything is fetched via `releases/latest/download/<asset>` and verified against `latest.json` before use. The one-line installers (`install.sh`, `install.ps1`) are served from `main` via raw.githubusercontent.com. They fetch the release's setup file and run it.

**Shared per-user folder.** The app's `_app_dir()` and all three launchers must agree on the location of the app folder:
- Mac: `~/Library/Application Support/SoundCloudDownloader`
- Windows: `%LOCALAPPDATA%\SoundCloudDownloader`
- Linux: `$XDG_DATA_HOME/SoundCloudDownloader`

It contains `uv/`, `python/` (uv-managed), `env/` (the venv), the app file, `settings.json` (folder, workers, sign-in token; owner-only permissions), icons, `bin/ffmpeg`, and a copy of the setup file for repairs.

**Isolation from the user's system.** Every uv call sets these environment variables (`uv_env()` in the app, exported in the launchers): `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_BIN_DIR`, `UV_PYTHON_INSTALL_BIN=0`, `UV_PYTHON_INSTALL_REGISTRY=0`, `UV_NO_CACHE`, `UV_NO_CONFIG` and `UV_PYTHON_PREFERENCE=only-managed`. Without them, uv adds `python3.x` links to `~/.local/bin` and Windows registry entries. Keep them on any new uv call.

**Python upgrades.** Launchers run `uv python install 3.12` before `uv venv`, so the venv's `home` points at uv's minor-version link (`cpython-3.12-<platform>`), not a patch folder. That lets `uv python upgrade` move the venv to new patch releases transparently. The launchers rebuild the venv if `pyvenv.cfg` doesn't reference that link. `prune_old_pythons()` only deletes patch folders when every link resolved to one, as a guard against junction quirks.

**Update flow** (`App.run_updates`). It runs on every launch, and only for the installed copy (`managed_install()`, `Path(__file__) == APP_FILE`):
1. `update_app()`: if newer, it writes the new app and setup file atomically, then `on_restart` runs `--setup` (refreshes icons, the Mac `.app`, the Linux `.desktop`) and relaunches.
2. `update_uv()`: reads the latest tag from GitHub's redirect.
3. `update_python()`.
4. `update_packages()`: `uv pip install --upgrade`, which also upgrades transitive dependencies.

Offline errors stop the sequence quietly. Launchers do their own version check first and `exec` the newer setup file if one exists.

**Download pipeline.**
1. yt-dlp `--flat-playlist -J` probes the link.
2. One `scdl` subprocess runs per playlist item (`--playlist-items N` via `--yt-dlp-args`) in a thread pool. All workers share `scdl_archive.txt`, which yt-dlp locks.
3. Non-MP3 audio is converted with ffmpeg.
4. Cover art is shrunk to at most 1000×1000 JPEG with ID3v2.3 tags, for rekordbox.

ffmpeg comes from the `imageio-ffmpeg` wheel and is linked as `bin/ffmpeg` on `PATH` (`setup_ffmpeg()`). There's no ffprobe, so cover inspection parses `ffmpeg -i` output. Worker threads talk to the Tk thread only through `self.q`; handlers are named `on_<kind>`.

**Sign-in.**
- `browser_token()` reads the `oauth_token` cookie using `yt_dlp.cookies.extract_cookies_from_browser`.
- `verify_token()` checks it with soundcloud-v2 (installed with scdl).
- The token is passed as `--auth-token` to scdl and as `--username oauth --password` to yt-dlp.
- Known limits: Safari is blocked by macOS privacy protections, and Chrome/Edge on Windows lock their cookies.
- The manual paste-token path is the fallback.

## Browser extension (`extension/`)

A separate Chrome + Firefox (Manifest V3) port of the same features. It has no server: the extension's host permissions let it call SoundCloud directly, and ffmpeg.wasm does the MP3 conversion. Commands, the code map and its known pitfalls are in `extension/README.md`. In short:

```bash
cd extension && npm install && npm run build      # → dist/extension/{chrome,firefox}/
node test/e2e.mjs chrome|firefox [url]            # real download via test browsers in /tmp/scd-browsers
node test/lookup.mjs [url ...]                    # lookups only
```

- `extension/src/lib/soundcloud.js` re-implements what yt-dlp does for SoundCloud, so SoundCloud API changes have to be fixed there by hand.
- Root `python3 build.py` deletes all of `dist/`, including `dist/extension/`, so run `npm run build` again afterwards.
- The extension has its own version number, in `extension/package.json`.

## Conventions

- Text users see is written for non-technical people. Keep error messages actionable, and update `src/READ ME FIRST.txt` (which ships inside the zip) when the user-facing flow changes.
- Bump `__version__` for every release. Existing installs only update when `latest.json` has a higher version.
- Commits in this repo use the account's GitHub noreply email (set in the local git config).
