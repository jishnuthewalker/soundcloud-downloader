#!/bin/bash
# ------------------------------------------------------------------
#  SoundCloud Downloader — Linux setup (setup + app in one file)
#
#  Right-click this file and choose "Run as a Program" (or open a
#  terminal, type  bash  and a space, drag this file in, press Enter).
#
#  It needs no password and installs nothing system-wide. Everything
#  (a private copy of Python, ffmpeg and scdl) goes in
#  ~/.local/share/SoundCloudDownloader, and "SoundCloud Downloader"
#  is added to your applications menu, so you only need this file
#  once. The app keeps itself up to date.
# ------------------------------------------------------------------
set -u

SELF="$(readlink -f "${BASH_SOURCE[0]}")"

# Started by double-click with no terminal? Re-open in one so progress is visible.
if [ ! -t 0 ] && [ -z "${SCD_IN_TERMINAL:-}" ]; then
  export SCD_IN_TERMINAL=1
  for term in x-terminal-emulator gnome-terminal kgx ptyxis konsole xfce4-terminal mate-terminal \
              lxterminal tilix terminator alacritty kitty xterm; do
    command -v "$term" >/dev/null 2>&1 || continue
    case "$term" in
      gnome-terminal|kgx|ptyxis) exec "$term" -- bash "$SELF" ;;
      *)                         exec "$term" -e bash "$SELF" ;;
    esac
  done
fi

SCD_VERSION="@VERSION@"
REPO="@REPO@"
PYTHON_VERSION="@PYTHON@"
PACKAGES="scdl yt-dlp imageio-ffmpeg"

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/SoundCloudDownloader"
UV="$APP_DIR/uv/uv"
VENV="$APP_DIR/env"
PY="$VENV/bin/python"
APP="$APP_DIR/soundcloud_downloader.py"
RELEASES="${SCD_RELEASE_URL:-https://github.com/$REPO/releases/latest/download}"  # override for testing

# Keep everything uv does inside APP_DIR (no PATH links, no shared caches).
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python" UV_PYTHON_BIN_DIR="$APP_DIR/python/bin" \
       UV_PYTHON_INSTALL_BIN=0 UV_PYTHON_INSTALL_REGISTRY=0 UV_NO_CACHE=1 UV_NO_CONFIG=1 \
       UV_PYTHON_PREFERENCE=only-managed UV_NO_PROGRESS=1

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m✗ %s\033[0m\n\n' "$*"; read -r -p "Press Enter to close." _; exit 1; }
fetch() {  # fetch URL FILE
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 --max-time 300 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -T 60 -O "$2" "$1"
  else return 1; fi
}
version_gt() {  # true if version $1 is newer than $2
  local IFS=. i
  local -a a=($1) b=($2)
  for i in 0 1 2 3; do
    (( 10#${a[i]:-0} > 10#${b[i]:-0} )) 2>/dev/null && return 0
    (( 10#${a[i]:-0} < 10#${b[i]:-0} )) 2>/dev/null && return 1
  done
  return 1
}

chmod +x "$SELF" 2>/dev/null
printf '\n\033[1m♫  SoundCloud Downloader setup (version %s)\033[0m\n' "$SCD_VERSION"
mkdir -p "$APP_DIR" || fail "Couldn't create $APP_DIR"
command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
  || fail "Please install curl first (for example: sudo apt install curl), then run this again."

# 0. If a newer version of this setup file has been published, switch to it.
if [ -z "${SCD_SKIP_SELF_UPDATE:-}" ]; then
  MANIFEST="$(fetch "$RELEASES/latest.json" /dev/stdout 2>/dev/null)"
  LATEST="$(printf '%s\n' "$MANIFEST" | sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p' | head -n 1)"
  if [ -n "$LATEST" ] && version_gt "$LATEST" "$SCD_VERSION"; then
    say "Getting the newest version ($LATEST)…"
    WANT="$(printf '%s\n' "$MANIFEST" | sed -n 's/.*"setup-linux.sh": *"\([0-9a-f]*\)".*/\1/p' | head -n 1)"
    NEW="$APP_DIR/setup.sh.new"
    if fetch "$RELEASES/setup-linux.sh" "$NEW" && [ -n "$WANT" ] \
       && [ "$(sha256sum "$NEW" | awk '{print $1}')" = "$WANT" ]; then
      mv -f "$NEW" "$APP_DIR/setup.sh" && chmod +x "$APP_DIR/setup.sh"
      export SCD_SKIP_SELF_UPDATE=1
      exec bash "$APP_DIR/setup.sh"
    fi
    rm -f "$NEW"
    say "(Couldn't download it; continuing with this version.)"
  fi
fi

# 1. uv: a small tool that fetches a private Python (no password needed)
if [ ! -x "$UV" ]; then
  case "$(uname -m)" in
    x86_64|amd64)  TARGET=x86_64-unknown-linux-musl ;;
    aarch64|arm64) TARGET=aarch64-unknown-linux-musl ;;
    *)             fail "Unsupported processor: $(uname -m)" ;;
  esac
  say "Downloading setup helper…"
  BASE="https://github.com/astral-sh/uv/releases/latest/download/uv-$TARGET.tar.gz"
  TMP="$(mktemp -d)" || fail "Couldn't create a temporary folder."
  fetch "$BASE" "$TMP/uv.tar.gz" && fetch "$BASE.sha256" "$TMP/uv.sha256" \
    || { rm -rf "$TMP"; fail "Download failed. Check your internet connection and try again."; }
  WANT="$(awk '{print $1}' "$TMP/uv.sha256")"
  GOT="$(sha256sum "$TMP/uv.tar.gz" | awk '{print $1}')"
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
  "$PY" -c 'import tkinter' >/dev/null 2>&1 \
    || fail "Python was installed but its window toolkit doesn't work. Is this a desktop (not a server) system?"
fi

# 3. Newest scdl, yt-dlp, ffmpeg and everything they depend on
say "Installing the newest downloader and ffmpeg…"
# shellcheck disable=SC2086
"$UV" pip install --quiet --upgrade --python "$PY" $PACKAGES \
  || fail "Couldn't install the downloader. Check your internet connection and try again."

# 4. Unpack the app (everything after the ##PY-START line), keep a copy of this
#    setup file for repairs, and add the app to the app menu.
awk 'f{print} /^##PY-START$/{f=1}' "$SELF" > "$APP.new" && [ -s "$APP.new" ] && mv -f "$APP.new" "$APP" \
  || fail "This setup file looks damaged. Download it again."
[ "$SELF" = "$APP_DIR/setup.sh" ] || { cp -f "$SELF" "$APP_DIR/setup.sh" && chmod +x "$APP_DIR/setup.sh"; }
"$PY" "$APP" --setup >/dev/null 2>&1

if command -v setsid >/dev/null 2>&1; then
  setsid "$PY" "$APP" >/dev/null 2>&1 < /dev/null &
else
  nohup "$PY" "$APP" >/dev/null 2>&1 < /dev/null &
fi

printf '\n\033[32m✓ All set!\033[0m SoundCloud Downloader is opening.\n'
printf '\nFrom now on, open it from your applications menu (search for "SoundCloud").\n'
printf 'It updates itself, so you can delete this setup file.\n\n'
read -r -t 10 -p "This window closes in 10 seconds (or press Enter)." _
echo
exit 0
##PY-START
