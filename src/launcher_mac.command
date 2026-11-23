#!/bin/bash
# ------------------------------------------------------------------
#  SoundCloud Downloader — Mac setup (setup + app in one file)
#
#  To run it without macOS security warnings: open Terminal, type
#  bash  and a space, drag this file into the window, press Return.
#  (Double-clicking makes macOS block it. See "READ ME FIRST.txt".)
#
#  It needs no password and installs nothing system-wide. Everything
#  (a private copy of Python, ffmpeg and scdl) goes in:
#     ~/Library/Application Support/SoundCloudDownloader
#  and a "SoundCloud Downloader" app is added to Applications, so
#  you only need this file once. The app keeps itself up to date.
# ------------------------------------------------------------------
set -u

SCD_VERSION="@VERSION@"
REPO="@REPO@"
PYTHON_VERSION="@PYTHON@"
PACKAGES="scdl yt-dlp imageio-ffmpeg"

APP_DIR="$HOME/Library/Application Support/SoundCloudDownloader"
UV="$APP_DIR/uv/uv"
VENV="$APP_DIR/env"
PY="$VENV/bin/python"
APP="$APP_DIR/soundcloud_downloader.py"
SELF="${BASH_SOURCE[0]}"
RELEASES="${SCD_RELEASE_URL:-https://github.com/$REPO/releases/latest/download}"  # override for testing

# Keep everything uv does inside APP_DIR (no PATH links, no shared caches).
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python" UV_PYTHON_BIN_DIR="$APP_DIR/python/bin" \
       UV_PYTHON_INSTALL_BIN=0 UV_PYTHON_INSTALL_REGISTRY=0 UV_NO_CACHE=1 UV_NO_CONFIG=1 \
       UV_PYTHON_PREFERENCE=only-managed UV_NO_PROGRESS=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n\n' "$*"; read -r -p "Press Return to close." _; exit 1; }
version_gt() {  # true if version $1 is newer than $2
  local IFS=. i
  local -a a=($1) b=($2)
  for i in 0 1 2 3; do
    (( 10#${a[i]:-0} > 10#${b[i]:-0} )) 2>/dev/null && return 0
    (( 10#${a[i]:-0} < 10#${b[i]:-0} )) 2>/dev/null && return 1
  done
  return 1
}

# Let future double-clicks work (downloaded files lose these by default).
chmod +x "$SELF" 2>/dev/null
xattr -d com.apple.quarantine "$SELF" 2>/dev/null

printf '\n\033[1m♫  SoundCloud Downloader setup (version %s)\033[0m\n' "$SCD_VERSION"
mkdir -p "$APP_DIR" || fail "Couldn't create $APP_DIR"

# 0. If a newer version of this setup file has been published, switch to it.
if [ -z "${SCD_SKIP_SELF_UPDATE:-}" ]; then
  MANIFEST="$(curl -fsSL --max-time 20 "$RELEASES/latest.json" 2>/dev/null)"
  LATEST="$(printf '%s\n' "$MANIFEST" | sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p' | head -n 1)"
  if [ -n "$LATEST" ] && version_gt "$LATEST" "$SCD_VERSION"; then
    say "Getting the newest version ($LATEST)…"
    WANT="$(printf '%s\n' "$MANIFEST" | sed -n 's/.*"setup-mac.command": *"\([0-9a-f]*\)".*/\1/p' | head -n 1)"
    NEW="$APP_DIR/setup.command.new"
    if curl -fsSL --retry 3 -o "$NEW" "$RELEASES/setup-mac.command" && [ -n "$WANT" ] \
       && [ "$(shasum -a 256 "$NEW" | awk '{print $1}')" = "$WANT" ]; then
      mv -f "$NEW" "$APP_DIR/setup.command" && chmod +x "$APP_DIR/setup.command"
      export SCD_SKIP_SELF_UPDATE=1
      exec bash "$APP_DIR/setup.command"
    fi
    rm -f "$NEW"
    say "(Couldn't download it; continuing with this version.)"
  fi
fi

# 1. uv: a small tool that fetches a private Python (no admin password needed)
if [ ! -x "$UV" ]; then
  case "$(uname -m)" in
    arm64)  TARGET=aarch64-apple-darwin ;;
    x86_64) TARGET=x86_64-apple-darwin ;;
    *)      fail "Unsupported Mac processor: $(uname -m)" ;;
  esac
  say "Downloading setup helper…"
  BASE="https://github.com/astral-sh/uv/releases/latest/download/uv-$TARGET.tar.gz"
  TMP="$(mktemp -d)" || fail "Couldn't create a temporary folder."
  curl -fsSL --retry 3 -o "$TMP/uv.tar.gz" "$BASE" \
    && curl -fsSL --retry 3 -o "$TMP/uv.sha256" "$BASE.sha256" \
    || { rm -rf "$TMP"; fail "Download failed. Check your internet connection and try again."; }
  WANT="$(awk '{print $1}' "$TMP/uv.sha256")"
  GOT="$(shasum -a 256 "$TMP/uv.tar.gz" | awk '{print $1}')"
  [ -n "$WANT" ] && [ "$WANT" = "$GOT" ] || { rm -rf "$TMP"; fail "The download was damaged. Please try again."; }
  tar -xzf "$TMP/uv.tar.gz" -C "$TMP" || { rm -rf "$TMP"; fail "Couldn't unpack the setup helper."; }
  mkdir -p "$APP_DIR/uv" && mv -f "$TMP/uv-$TARGET/uv" "$UV" && chmod +x "$UV"
  rm -rf "$TMP"
  [ -x "$UV" ] || fail "Couldn't install the setup helper."
fi

# 2. Private Python, newest patch release. The environment points at uv's
#    "3.12" link, so later Python upgrades apply to it automatically.
say "Setting up Python (the first time takes about a minute)…"
"$UV" python install --quiet "$PYTHON_VERSION" \
  && "$UV" python upgrade --quiet "$PYTHON_VERSION" \
  || fail "Couldn't set up Python. Check your internet connection and try again."
if ! grep -qsF "$APP_DIR/python/cpython-$PYTHON_VERSION-" "$VENV/pyvenv.cfg" \
   || ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
  rm -rf "$VENV"
  "$UV" venv --quiet --python "$PYTHON_VERSION" "$VENV" || fail "Couldn't set up Python."
  "$PY" -c 'import tkinter' >/dev/null 2>&1 || fail "Python was installed but its window toolkit doesn't work."
fi

# 3. Newest scdl, yt-dlp, ffmpeg and everything they depend on
say "Installing the newest downloader and ffmpeg…"
# shellcheck disable=SC2086
"$UV" pip install --quiet --upgrade --python "$PY" $PACKAGES \
  || fail "Couldn't install the downloader. Check your internet connection and try again."

# 4. Unpack the app (everything after the ##PY-START line), keep a copy of this
#    setup file for repairs, and add the app to Applications.
awk 'f{print} /^##PY-START$/{f=1}' "$SELF" > "$APP.new" && [ -s "$APP.new" ] && mv -f "$APP.new" "$APP" \
  || fail "This setup file looks damaged. Download it again."
if [ "$(cd "$(dirname "$SELF")" && pwd)/$(basename "$SELF")" != "$APP_DIR/setup.command" ]; then
  cp -f "$SELF" "$APP_DIR/setup.command" && chmod +x "$APP_DIR/setup.command"
fi
MAC_APP="$("$PY" "$APP" --setup 2>/dev/null | sed -n 's/^APP=//p')"

nohup "$PY" "$APP" >/dev/null 2>&1 &

printf '\n\033[32m✓ All set!\033[0m SoundCloud Downloader is opening.\n'
if [ -n "$MAC_APP" ]; then
  printf '\nFrom now on, open it like any other app: find \033[1mSoundCloud Downloader\033[0m\n'
  printf 'in your Applications folder or search for it with Spotlight (⌘ Space).\n'
  printf 'It updates itself, so you can delete this setup file.\n'
  open -R "$MAC_APP" 2>/dev/null
fi
printf '\nYou can close this window.\n\n'
exit 0
##PY-START
