#!/bin/bash
# SoundCloud Downloader: one-line installer for Mac and Linux. Paste this into Terminal:
#
#   curl -fsSL https://raw.githubusercontent.com/jishnuthewalker/soundcloud-downloader/main/install.sh | bash
#
# It downloads the newest setup file from the latest release, checks it against the
# published checksum, and runs it. Nothing downloaded this way triggers security warnings.
set -u

main() {
  local repo="jishnuthewalker/soundcloud-downloader"
  local releases="${SCD_RELEASE_URL:-https://github.com/$repo/releases/latest/download}"
  local asset dir file manifest want got

  fail() { printf '\n\033[31m✗ %s\033[0m\n\n' "$*" >&2; exit 1; }
  fetch() {  # fetch URL FILE
    if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 --max-time 120 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then wget -q -T 60 -O "$2" "$1"
    else fail "Please install curl first, then try again."; fi
  }
  checksum() {
    if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    else sha256sum "$1" | awk '{print $1}'; fi
  }

  case "$(uname -s)" in
    Darwin) asset=setup-mac.command
            dir="$HOME/Library/Application Support/SoundCloudDownloader"; file="$dir/setup.command" ;;
    Linux)  asset=setup-linux.sh
            dir="${XDG_DATA_HOME:-$HOME/.local/share}/SoundCloudDownloader"; file="$dir/setup.sh" ;;
    *)      fail "This installer is for Mac and Linux. On Windows, see https://github.com/$repo" ;;
  esac

  printf '\n\033[1m♫  Downloading SoundCloud Downloader setup…\033[0m\n'
  mkdir -p "$dir" || fail "Couldn't create $dir"
  manifest="$(fetch "$releases/latest.json" /dev/stdout)" \
    || fail "Download failed. Check your internet connection and try again."
  want="$(printf '%s\n' "$manifest" | sed -n "s/.*\"$asset\": *\"\([0-9a-f]*\)\".*/\1/p" | head -n 1)"
  fetch "$releases/$asset" "$file.new" || fail "Download failed. Check your internet connection and try again."
  got="$(checksum "$file.new")"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    rm -f "$file.new"
    fail "The download was damaged. Please try again."
  fi
  mv -f "$file.new" "$file" && chmod +x "$file" || fail "Couldn't save the setup file."

  # It's already the newest version; run it attached to the terminal (stdin is this script).
  export SCD_SKIP_SELF_UPDATE=1 SCD_IN_TERMINAL=1
  if (: </dev/tty) 2>/dev/null; then
    exec bash "$file" </dev/tty
  fi
  exec bash "$file" </dev/null
}

main "$@"   # everything is inside main() so a partly downloaded script can't run halfway
