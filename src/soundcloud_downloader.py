#!/usr/bin/env python3
"""
SoundCloud Downloader — a small window around scdl.

Paste any SoundCloud link (a single track, a playlist/set, or a user's page),
confirm what it found, and it downloads everything as MP3. Playlists download
several tracks at once. When it finishes, it lists any songs that failed, with
the reason, and lets you retry just those.

Optional Soulseek: also look for a lossless FLAC of every track on the Soulseek network and
save it in a "FLAC" folder next to the MP3s.

Optional sign-in: connect your SoundCloud account (read from the browser you're
already signed in with, or by pasting a token) to download your likes, private
tracks you can access, and full Go+ tracks if you subscribe.

Normally installed and started by the "SoundCloud Downloader" setup files for
Mac, Windows and Linux. It can also be run directly:
    python soundcloud_downloader.py
Requires Python 3.9+ with tkinter, plus scdl and ffmpeg (or the imageio-ffmpeg
package, which bundles ffmpeg).

Re-running on the same link is safe: an archive file in the destination folder
records finished tracks, so only missing ones are fetched.
"""

import hashlib
import importlib.util
import io
import json
import os
import platform
import plistlib
import queue
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tarfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import webbrowser
import zipfile
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

__version__ = "2.2.1"     # bump this for every release (build.py reads it)
REPO = "jishnuthewalker/soundcloud-downloader"   # GitHub repo that publishes updates
# SCD_RELEASE_URL lets you test updates from a local folder or use a mirror.
RELEASE_URL = (os.environ.get("SCD_RELEASE_URL") or f"https://github.com/{REPO}/releases/latest/download").rstrip("/") + "/"
APP_NAME = "SoundCloud Downloader"
DEFAULT_FOLDER = Path.home() / "Music" / "SoundCloud"
DEFAULT_WORKERS = 4        # much higher tends to trigger SoundCloud rate limits (HTTP 429)
MAX_WORKERS = 8
MP3_QUALITY = "2"          # LAME VBR quality for converted files: 0 = best/largest, 2 ≈ 190 kbps
NON_MP3_AUDIO = {".m4a", ".opus", ".ogg", ".oga", ".aac", ".webm", ".wav", ".flac", ".aif", ".aiff"}
MAX_ART = 1000             # album art is shrunk to fit within MAX_ART x MAX_ART (rekordbox-friendly)
REPORT_NAME = "failed_downloads.txt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
PACKAGES = ["scdl", "yt-dlp", "imageio-ffmpeg", "aioslsk"]
PYTHON_MINOR = "3.12"      # the private Python; patch releases are installed automatically
SIGNIN_URL = "https://soundcloud.com/signin"


def _app_dir():
    """Private folder shared with the setup launchers (must match their paths)."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "SoundCloudDownloader"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SoundCloudDownloader"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "SoundCloudDownloader"


APP_DIR = _app_dir()
SETTINGS_FILE = APP_DIR / "settings.json"
APP_FILE = APP_DIR / "soundcloud_downloader.py"
UV_EXE = APP_DIR / "uv" / ("uv.exe" if sys.platform == "win32" else "uv")
PYTHON_DIR = APP_DIR / "python"
ICON_PNG = APP_DIR / "icon.png"
ICON_ICO = APP_DIR / "icon.ico"


# ---------------------------------------------------------------- helpers

def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW, **kw)


def console_python():
    """The interpreter to use for child processes (python.exe, not pythonw.exe, on Windows)."""
    if sys.platform == "win32":
        p = Path(sys.executable).with_name("python.exe")
        if p.exists():
            return str(p)
    return sys.executable


def last_line(text, fallback="unknown error"):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else fallback


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    """Saved with owner-only permissions because it can hold the sign-in token."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
        if os.name != "nt":
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
        else:
            tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass


def find_scdl():
    """scdl's launcher may not be on PATH right after a pip install, so look next to Python too."""
    exe = "scdl.exe" if sys.platform == "win32" else "scdl"
    candidates = [
        Path(sysconfig.get_path("scripts")) / exe,
        Path(sys.executable).parent / exe,
        Path(sysconfig.get_path("scripts", f"{os.name}_user")) / exe,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("scdl")


def bundled_ffmpeg():
    """Path of the ffmpeg that ships inside the imageio-ffmpeg package, if installed."""
    spec = importlib.util.find_spec("imageio_ffmpeg")
    if not spec or not spec.submodule_search_locations:
        return None
    bins = Path(list(spec.submodule_search_locations)[0]) / "binaries"
    found = [p for p in bins.glob("ffmpeg*")
             if p.is_file() and not p.name.endswith((".py", ".pyc", ".md", ".txt"))]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def setup_ffmpeg():
    """Put a plain 'ffmpeg' command on PATH (the bundled one if available). True if ffmpeg works."""
    exe = bundled_ffmpeg()
    if exe:
        bindir = APP_DIR / "bin"
        link = bindir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        try:
            bindir.mkdir(parents=True, exist_ok=True)
            current = link.exists() and (os.path.samefile(link, exe) or
                                         (sys.platform == "win32" and link.stat().st_size == exe.stat().st_size))
            if not current:
                if link.exists() or link.is_symlink():
                    link.unlink()
                try:
                    if sys.platform == "win32":
                        os.link(exe, link)
                    else:
                        os.symlink(exe, link)
                except OSError:
                    shutil.copy2(exe, link)
            if not os.access(exe, os.X_OK):
                os.chmod(exe, 0o755)
            path = os.environ.get("PATH", "")
            if str(bindir) not in path.split(os.pathsep):
                os.environ["PATH"] = str(bindir) + os.pathsep + path
        except OSError:
            pass
    return shutil.which("ffmpeg") is not None


def managed_install():
    """True when running from the private environment the setup launcher made."""
    try:
        return Path(sys.prefix).resolve() == (APP_DIR / "env").resolve()
    except OSError:
        return False


def uv_env():
    """Keep everything uv does inside the app folder: no PATH links, no Windows registry entries."""
    return dict(os.environ, UV_PYTHON_INSTALL_DIR=str(PYTHON_DIR), UV_PYTHON_BIN_DIR=str(PYTHON_DIR / "bin"),
                UV_PYTHON_INSTALL_BIN="0", UV_PYTHON_INSTALL_REGISTRY="0", UV_NO_CACHE="1",
                UV_NO_CONFIG="1", UV_PYTHON_PREFERENCE="only-managed", UV_NO_PROGRESS="1")


def vtuple(version):
    try:
        return tuple(int(x) for x in str(version).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def ssl_context():
    import ssl
    try:
        import certifi                    # installed with scdl; the private Python may lack system certs
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http_get(url, timeout=20):
    """GET a URL; returns the body bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": f"SoundCloudDownloader/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return r.read()


def github_latest_tag(repo):
    """Latest release tag, read from GitHub's redirect (no API rate limits)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl_context()))
    req = urllib.request.Request(f"https://github.com/{repo}/releases/latest", method="HEAD",
                                 headers={"User-Agent": f"SoundCloudDownloader/{__version__}"})
    try:
        opener.open(req, timeout=20)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")
        if e.code in (301, 302, 303, 307, 308) and "/tag/" in location:
            return location.rstrip("/").rsplit("/", 1)[-1]
        raise
    raise RuntimeError("couldn't find the latest version")


def write_atomic(path, data, mode=None):
    tmp = path.with_name(path.name + ".new")
    tmp.write_bytes(data)
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def setup_asset():
    """(release asset name, local copy kept for repairs) for this platform's setup file."""
    if sys.platform == "darwin":
        return "setup-mac.command", APP_DIR / "setup.command"
    if sys.platform == "win32":
        return "setup-windows.bat", APP_DIR / "setup.bat"
    return "setup-linux.sh", APP_DIR / "setup.sh"


def update_app():
    """Install a newer version of this app (and its setup file) from GitHub. Returns the new version or None."""
    if Path(__file__).resolve() != APP_FILE.resolve():
        return None                       # only the installed copy updates itself
    try:
        manifest = json.loads(http_get(RELEASE_URL + "latest.json"))
    except urllib.error.HTTPError as e:
        if e.code == 404:                 # nothing published yet
            return None
        raise
    latest = str(manifest.get("version", ""))
    if vtuple(latest) <= vtuple(__version__):
        return None
    sums = manifest.get("files") or {}

    def fetch(name):
        data = http_get(RELEASE_URL + name, timeout=120)
        if not sums.get(name) or hashlib.sha256(data).hexdigest() != sums[name]:
            raise RuntimeError(f"the download of {name} was damaged; will retry next time")
        return data

    code = fetch("soundcloud_downloader.py")
    compile(code, "soundcloud_downloader.py", "exec")    # never install something that can't even load
    asset, local = setup_asset()
    try:
        setup = fetch(asset)
    except Exception:
        setup = None
    write_atomic(APP_FILE, code)
    if setup:
        write_atomic(local, setup, 0o755)
    return latest


def uv_asset():
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    if sys.platform == "win32":
        return f"uv-{arch}-pc-windows-msvc.zip"
    if sys.platform == "darwin":
        return f"uv-{arch}-apple-darwin.tar.gz"
    return f"uv-{arch}-unknown-linux-musl.tar.gz"


def update_uv():
    """Replace the private copy of uv if a newer release exists. Returns the new version or None."""
    if not UV_EXE.exists():
        return None
    latest = github_latest_tag("astral-sh/uv")
    parts = run([str(UV_EXE), "--version"]).stdout.split()
    current = parts[1] if len(parts) > 1 else "0"
    if vtuple(latest) <= vtuple(current):
        return None
    name = uv_asset()
    url = f"https://github.com/astral-sh/uv/releases/download/{latest}/{name}"
    data = http_get(url, timeout=300)
    want = http_get(url + ".sha256").decode("ascii", "replace").split()[0]
    if hashlib.sha256(data).hexdigest() != want.lower():
        raise RuntimeError("the uv download was damaged; will retry next time")
    exe_name = UV_EXE.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(n for n in z.namelist() if n.split("/")[-1] == exe_name)
            binary = z.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.isfile() and m.name.split("/")[-1] == exe_name)
            binary = t.extractfile(member).read()
    write_atomic(UV_EXE, binary, 0o755)
    return latest


def _python_links():
    return [p for p in PYTHON_DIR.glob(f"cpython-{PYTHON_MINOR}-*")]


def prune_old_pythons():
    """Delete Python patch versions the minor-version link no longer points at."""
    links = _python_links()
    targets = set()
    for link in links:
        try:
            targets.add(link.resolve().name)
        except OSError:
            return
    patch = re.compile(rf"cpython-{re.escape(PYTHON_MINOR)}\.\d+-")
    # Only prune when every link clearly resolved to a patch folder (protects against odd filesystems).
    if not targets or not all(patch.match(t) for t in targets):
        return
    for p in PYTHON_DIR.iterdir():
        if patch.match(p.name) and p.name not in targets and p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)


def update_python():
    """Upgrade the private Python to the newest patch release. Returns the new version or None."""
    if not UV_EXE.exists() or not _python_links():
        return None
    cfg = (Path(sys.prefix) / "pyvenv.cfg")
    try:
        if f"cpython-{PYTHON_MINOR}-" not in cfg.read_text(encoding="utf-8"):
            return None                   # older layout: the setup file rebuilds it
    except OSError:
        return None
    prune_old_pythons()
    r = run([str(UV_EXE), "python", "upgrade", PYTHON_MINOR], env=uv_env(), timeout=900)
    if r.returncode != 0:
        raise RuntimeError(last_line(r.stderr))
    m = re.search(r"Installed Python (\S+)", (r.stderr or "") + (r.stdout or ""))
    return m.group(1) if m else None


def update_packages():
    """Upgrade scdl, yt-dlp, ffmpeg and everything they depend on. Returns a list of 'name==version'."""
    if UV_EXE.exists():
        cmd = [str(UV_EXE), "pip", "install", "--upgrade", "--python", console_python(), *PACKAGES]
    else:
        cmd = [console_python(), "-m", "pip", "install", "--disable-pip-version-check",
               "--upgrade", "--upgrade-strategy", "eager", *PACKAGES]
    r = run(cmd, env=uv_env(), timeout=900)
    if r.returncode != 0:
        raise RuntimeError(last_line(r.stderr))
    return [l.strip()[2:] for l in (r.stderr or "").splitlines() if l.strip().startswith("+ ")]


def is_offline_error(e):
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError)) and not isinstance(
        e, urllib.error.HTTPError)


def clean_url(raw):
    """Return a tidy SoundCloud URL (tracking params removed), or None if it isn't one."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.lower()
    if host != "soundcloud.com" and not host.endswith(".soundcloud.com"):
        return None
    if not parts.path.strip("/"):
        return None
    return urlunsplit(("https", parts.netloc, parts.path.rstrip("/"), "", ""))


def expand_short_url(url):
    """Share links like https://on.soundcloud.com/AbC123 redirect to the real track/playlist address,
    which scdl needs. Returns the cleaned full URL (or the original if it can't be expanded)."""
    if urlsplit(url).netloc.lower() != "on.soundcloud.com":
        return url
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as r:
            final = r.geturl()
    except urllib.error.HTTPError as e:
        final = e.headers.get("Location") or e.geturl()
    except Exception:
        return url
    full = clean_url(final)
    return full if full and urlsplit(full).netloc.lower() != "on.soundcloud.com" else url


def ytdlp_auth(token):
    return ["--username", "oauth", "--password", token] if token else []


def probe(url, token=None):
    """Ask yt-dlp (installed with scdl) what the link points to."""
    out = run([console_python(), "-m", "yt_dlp", "--flat-playlist", "-J", *ytdlp_auth(token), url])
    if out.returncode != 0:
        raise RuntimeError(last_line(out.stderr))
    data = json.loads(out.stdout)
    if data.get("_type") == "playlist":
        entries = data.get("entries") or []
        # Titles are often missing from a quick playlist listing; failures get looked up later.
        titles = {i: e.get("title") for i, e in enumerate(entries, 1) if e and e.get("title")}
        ids = {i: e.get("id") for i, e in enumerate(entries, 1) if e and e.get("id")}
        return {"title": data.get("title") or "Playlist", "count": len(entries), "playlist": True,
                "by": data.get("uploader") or "", "titles": titles, "ids": ids}
    title = data.get("title") or "Track"
    return {"title": title, "count": 1, "playlist": False,
            "by": data.get("uploader") or "", "titles": {1: title}, "ids": {1: data.get("id")},
            "meta": {1: {"title": title, "username": data.get("uploader") or "",
                         "artist": data.get("artist") or "", "duration": data.get("duration")}}}


def lookup_title(url, item=None, token=None):
    """Best-effort 'Title — Artist' for one track, even if it can't be downloaded."""
    cmd = [console_python(), "-m", "yt_dlp", "--skip-download", "--ignore-no-formats-error",
           "--no-warnings", "--print", "%(title)s — %(uploader|unknown artist)s", *ytdlp_auth(token)]
    if item is not None:
        cmd += ["--playlist-items", str(item)]
    cmd.append(url)
    line = last_line(run(cmd).stdout, "")
    return line or None


def friendly_reason(err, signed_in=False):
    e = (err or "").lower()
    if "auth_token" in e or "authorization token is invalid" in e:
        return "Your SoundCloud sign-in has expired. Click Sign out, then Sign in again."
    if "429" in e or "too many requests" in e:
        return "Rate-limited by SoundCloud. Wait a few minutes and retry, or lower parallel downloads."
    if "geo" in e or "not available in your country" in e or "region" in e:
        return "Blocked in your region."
    if "requested format" in e or "no video formats" in e or "no formats" in e:
        if signed_in:
            return "No downloadable stream (a Go+ only track, and this account doesn't have Go+)."
        return "No downloadable stream (often a Go+ only track; signing in with a Go+ account can fix this)."
    if "private" in e or "404" in e or "not found" in e:
        return ("Private or removed from SoundCloud." if signed_in else
                "Private or removed from SoundCloud. If it's private and you have access, sign in first.")
    if "timed out" in e or "connection" in e:
        return "Network problem. Retry should fix it."
    return last_line(err)[:200]


def scdl_cmd(scdl, url, dest, archive, item=None, token=None):
    cmd = [scdl, "-l", url, "--path", str(dest),
           "--download-archive", str(archive),   # yt-dlp locks this file, so workers can share it
           "-c", "--original-art", "--addtofile", "--hide-progress",
           "--no-original"]                      # use the stream (usually already MP3), not WAV/FLAC uploads
    if token:
        cmd += ["--auth-token", token]
    if item is not None:
        cmd += ["--yt-dlp-args", f"--playlist-items {item}"]
    return cmd


def to_mp3(path):
    """Convert one audio file to MP3, keeping tags and cover art. Removes the original on success."""
    out = path.with_suffix(".mp3")
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path)]
    audio = ["-c:a", "libmp3lame", "-q:a", MP3_QUALITY, "-map_metadata", "0", "-id3v2_version", "3"]
    with_cover = base + ["-map", "0:a:0", "-map", "0:v:0", "-c:v", "copy",
                         "-disposition:v:0", "attached_pic"] + audio + [str(out)]
    audio_only = base + ["-map", "0:a:0", "-vn"] + audio + [str(out)]
    r = run(with_cover)
    if r.returncode != 0:          # no cover stream, or a cover format MP3 can't hold
        r = run(audio_only)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
        path.unlink(missing_ok=True)
        return path, out, None
    out.unlink(missing_ok=True)
    return path, None, last_line(r.stderr)


_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?\b(\d{1,5})x(\d{1,5})\b")


def cover_info(path):
    """(width, height, codec) of the embedded cover, or None if there isn't one."""
    # 'ffmpeg -i' with no output lists the streams and exits non-zero; that's expected.
    r = run(["ffmpeg", "-hide_banner", "-i", str(path)])
    m = _VIDEO_STREAM.search(r.stderr or "")
    if not m:
        return None if "Stream #" in (r.stderr or "") else ("?", "?", "?")
    return int(m.group(2)), int(m.group(3)), m.group(1)


def fix_cover(path):
    """
    Shrink an MP3's embedded album art to fit within MAX_ART x MAX_ART and store it as a
    standard JPEG with ID3v2.3 tags, which rekordbox reads reliably. Audio is copied untouched.
    Returns (path, status, error) where status is 'resized', 'ok', or 'none'.
    """
    info = cover_info(path)
    if info is None:
        return path, "none", None
    w, h, codec = info
    if isinstance(w, int) and w <= MAX_ART and h <= MAX_ART and codec == "mjpeg":
        return path, "ok", None

    tmp = path.with_name(path.stem + ".artfix.tmp" + path.suffix)
    vf = (f"scale=w='min({MAX_ART},iw)':h='min({MAX_ART},ih)'"
          f":force_original_aspect_ratio=decrease,format=yuvj420p")
    r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-map", "0:a:0", "-map", "0:v:0", "-c:a", "copy",
             "-vf", vf, "-c:v", "mjpeg", "-q:v", "2", "-disposition:v:0", "attached_pic",
             "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
             "-map_metadata", "0", *(["-id3v2_version", "3"] if path.suffix.lower() == ".mp3" else []),
             str(tmp)])
    if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        os.replace(tmp, path)
        return path, "resized", None
    tmp.unlink(missing_ok=True)
    return path, "failed", last_line(r.stderr)


def fix_playlist_files(dest, since, renamed):
    """Point any .m3u files scdl wrote at the new .mp3 names."""
    if not renamed:
        return
    for m3u in list(dest.rglob("*.m3u")) + list(dest.rglob("*.m3u8")):
        try:
            if m3u.stat().st_mtime < since:
                continue
            text = m3u.read_text(encoding="utf-8")
            new = text
            for old, mp3 in renamed:
                new = new.replace(old.name, mp3.name)
            if new != text:
                m3u.write_text(new, encoding="utf-8")
        except OSError:
            pass


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------- Soulseek (lossless FLAC)
#
# Optional: for every SoundCloud track, also search the Soulseek file-sharing network for a
# FLAC of the same song and save it in a "FLAC" folder next to the MP3s. Uses aioslsk on its
# own asyncio loop thread; worker threads call into it through run_coroutine_threadsafe.

FLAC_FOLDER = "FLAC"
SLSK_PORT = 61000                 # avoids the defaults of Nicotine+ (2234) and aioslsk (60000)
SLSK_SEARCH_SECONDS = 12          # how long to collect search results
SLSK_START_TIMEOUT = 120          # give up on a peer whose queue doesn't move within this
SLSK_MAX_CANDIDATES = 3           # peers to try per track before giving up
SLSK_DURATION_SLACK = 4           # seconds a FLAC's length may differ from SoundCloud's
_NOISE = re.compile(r"\b(free\s*(download|dl)|out\s*now|premiere|exclusive|official(\s*(music\s*)?(video|audio))?|"
                    r"hq|hd|320\s*(kbps)?|buy\s*(link)?|click\s*buy|download\s*link)\b", re.I)
_STOP = {"the", "a", "an", "and", "of", "feat", "ft", "featuring", "x", "vs", "with", "original", "mix"}
_VERSIONS = {"remix", "bootleg", "vip", "dub", "rework", "flip", "instrumental", "acapella", "live", "cover",
             "edit", "remake", "mashup"}


def words(text):
    """Lower-case word list with accents removed (non-Latin scripts are kept)."""
    text = unicodedata.normalize("NFKD", str(text or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.findall(r"[^\W_]+", text)


def safe_filename(name, fallback="untitled"):
    """A file or folder name that's valid on Windows, macOS and Linux."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    name = name[:120].rstrip(" .")
    if not name or re.fullmatch(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name, re.I):
        name = f"_{name}" if name else fallback
    return name


def track_identity(meta):
    """(artist, title) for searching, from SoundCloud metadata."""
    title = str(meta.get("title") or "")
    artist = str(meta.get("artist") or "")
    m = re.match(r"\s*(.+?)\s+[-–—]\s+(.+)$", title)
    if m and not artist:
        artist, title = m.group(1), m.group(2)
    if not artist:
        artist = str(meta.get("username") or "")
    # Drop bracketed promo text like "[FREE DOWNLOAD]" and leftover promo words.
    title = re.sub(r"[\[(][^\])]*[\])]", lambda g: "" if _NOISE.search(g.group(0)) else g.group(0), title)
    title = _NOISE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip(" -–—|")
    return artist.strip(), title


def search_query(artist, title):
    keep = [w for w in words(f"{artist} {title}") if w not in _STOP and len(w) > 1]
    return " ".join(dict.fromkeys(keep))[:120]          # unique words, original order


def score_flac(meta, artist, title, result, fd):
    """How well a search result matches the SoundCloud track (higher is better), or None if it doesn't."""
    remote = str(fd.filename)
    if not remote.lower().endswith(".flac") or (fd.filesize or 0) < 2_000_000:
        return None
    parts = re.split(r"[\\/]+", remote)
    base = words(re.sub(r"\.flac$", "", parts[-1], flags=re.I))
    path = set(words(" ".join(parts)))
    title_words = [w for w in words(title) if w not in _STOP] or words(title)
    artist_words = [w for w in words(artist) if w not in _STOP] or words(artist)
    if not title_words:
        return None
    title_hit = sum(w in base for w in title_words) / len(title_words)
    artist_hit = sum(w in path for w in artist_words) / len(artist_words) if artist_words else 1.0
    if title_hit < 0.8 or artist_hit < 0.5:
        return None
    # Don't swap a remix for the original or vice versa.
    want = _VERSIONS & set(title_words)
    have = _VERSIONS & set(base)
    if want - have or have - want:
        return None
    duration_score = 0.5
    try:
        from aioslsk.protocol.primitives import AttributeKey
        seconds = fd.get_attribute_map().get(AttributeKey.DURATION)
    except Exception:
        seconds = None
    if seconds and meta.get("duration"):
        if abs(float(seconds) - float(meta["duration"])) > SLSK_DURATION_SLACK:
            return None
        duration_score = 1.0
    return (title_hit * 40 + artist_hit * 20 + duration_score * 20
            + (12 if result.has_free_slots else 0)
            - min(result.queue_size or 0, 50) * 0.3
            + min((result.avg_speed or 0) / 1_000_000, 5) * 2)


def track_metadata(info, token=None):
    """{playlist index: {title, username, artist, duration}} for Soulseek matching."""
    if info.get("meta"):
        return info["meta"]
    ids = {i: int(v) for i, v in (info.get("ids") or {}).items() if str(v).isdigit()}
    if not ids:
        return {}
    from soundcloud import SoundCloud      # installed with scdl
    client = SoundCloud(None, token)
    by_id = {}
    unique = list(dict.fromkeys(ids.values()))
    for start in range(0, len(unique), 50):
        for t in client.get_tracks(unique[start:start + 50]):
            pub = getattr(t, "publisher_metadata", None)
            by_id[t.id] = {"title": t.title, "username": getattr(t.user, "username", "") if t.user else "",
                           "artist": getattr(pub, "artist", "") if pub else "",
                           "duration": (t.duration or 0) / 1000 or None}
    return {i: by_id[tid] for i, tid in ids.items() if tid in by_id}


class SoulseekError(Exception):
    pass


class Soulseek:
    """A logged-in Soulseek client living on its own asyncio loop thread."""

    def __init__(self, username, password, share_dir=None, port=SLSK_PORT):
        import asyncio
        import logging
        logging.getLogger("aioslsk").setLevel(logging.CRITICAL)   # peers failing to connect is normal noise
        self.username, self.password, self.share_dir, self.port = username, password, share_dir, port
        self.asyncio = asyncio
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="soulseek", daemon=True)
        self.thread.start()
        self.client = None
        self.staging = APP_DIR / "soulseek-incoming"
        self._search_lock = threading.Lock()
        self._last_search = 0.0
        self._user_locks = defaultdict(threading.Lock)  # one download per peer at a time (etiquette)

    def _call(self, coro, timeout):
        return self.asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        shutil.rmtree(self.staging, ignore_errors=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self._call(self._start(), 90)
        return self

    async def _start(self):
        from aioslsk.client import SoulSeekClient
        from aioslsk.exceptions import AuthenticationError, ListeningConnectionFailedError
        from aioslsk.settings import Settings, SharedDirectorySettingEntry
        shares = [SharedDirectorySettingEntry(path=str(self.share_dir))] if self.share_dir else []
        last_error = None
        for port in range(self.port, self.port + 20, 2):
            settings = Settings(
                credentials={"username": self.username, "password": self.password},
                network={"listening": {"port": port, "obfuscated_port": port + 1}, "upnp": {"enabled": True},
                         "server": {"reconnect": {"auto": True, "timeout": 10}}},
                shares={"download": str(self.staging), "directories": shares, "scan_on_start": bool(shares)},
                transfers={"limits": {"upload_slots": 2}},
            )
            client = SoulSeekClient(settings)
            try:
                await client.start()
            except ListeningConnectionFailedError as e:      # port in use: try the next one
                last_error = e
                await self._quiet_stop(client)
                continue
            except Exception as e:
                await self._quiet_stop(client)
                raise SoulseekError("Couldn't reach the Soulseek server. Check your internet connection.") from e
            try:
                async with self.asyncio.timeout(30):
                    await client.login()
            except AuthenticationError as e:
                await self._quiet_stop(client)
                reason = str(getattr(e, "reason", "") or "")
                if "INVALIDPASS" in reason.upper():
                    raise SoulseekError("Wrong Soulseek password for that username.") from e
                raise SoulseekError(f"Soulseek refused the login ({reason or 'unknown reason'}).") from e
            except Exception as e:
                await self._quiet_stop(client)
                raise SoulseekError("Soulseek didn't answer the login. Try again in a minute.") from e
            self.client = client
            return
        raise SoulseekError(f"Couldn't open a network port for Soulseek ({last_error}).")

    @staticmethod
    async def _quiet_stop(client):
        try:
            await client.stop()
        except Exception:
            pass

    def stop(self):
        try:
            if self.client:
                self._call(self._quiet_stop(self.client), 15)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    # ---- search

    def search(self, query, seconds=SLSK_SEARCH_SECONDS):
        """[(SearchResult, FileData)] for every unlocked file in the replies."""
        with self._search_lock:                          # space searches out (etiquette)
            wait = self._last_search + 2.0 - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.time()
        req = self._call(self.client.searches.search(query), 30)
        time.sleep(seconds)
        return self._call(self._collect(req), 30)

    async def _collect(self, req):
        found = [(r, fd) for r in list(req.results) for fd in list(r.shared_items)]
        try:
            self.client.searches.remove_request(req)
        except (KeyError, ValueError):
            pass
        return found

    def find_flacs(self, meta):
        artist, title = track_identity(meta)
        query = search_query(artist, title)
        if not query:
            return []
        scored = []
        for result, fd in self.search(query):
            score = score_flac(meta, artist, title, result, fd)
            if score is not None:
                scored.append((score, result, fd))
        scored.sort(key=lambda x: x[0], reverse=True)
        picks, users = [], set()
        for score, result, fd in scored:                 # best file from each of the best peers
            if result.username not in users:
                users.add(result.username)
                picks.append((result.username, fd.filename, fd.filesize))
            if len(picks) >= SLSK_MAX_CANDIDATES:
                break
        return picks

    # ---- download

    def download(self, username, remote, dest, size):
        """Download one file to dest. Raises SoulseekError if it can't be done in reasonable time."""
        total = SLSK_START_TIMEOUT + max(300, (size or 0) / 100_000)     # allow ≥100 kB/s once started
        with self._user_locks[username]:
            return self._call(self._download(username, remote, Path(dest), total), total + 60)

    async def _download(self, username, remote, dest, total_timeout):
        from aioslsk.transfer.model import Transfer, TransferDirection
        from aioslsk.transfer.state import TransferState
        S = TransferState.State
        part = self.staging / f"{abs(hash((username, remote)))}.flac.part"
        part.unlink(missing_ok=True)
        transfer = Transfer(username, remote, TransferDirection.DOWNLOAD)
        transfer.local_path = str(part)
        transfer = await self.client.transfers.add(transfer)
        await self.client.transfers.queue(transfer)
        began = time.monotonic()
        try:
            while True:
                await self.asyncio.sleep(1)
                state = transfer.state.VALUE
                elapsed = time.monotonic() - began
                if state == S.COMPLETE:
                    break
                if state in (S.FAILED, S.ABORTED):
                    raise SoulseekError(transfer.fail_reason or "the other user cancelled it")
                if transfer.bytes_transfered == 0 and elapsed > SLSK_START_TIMEOUT:
                    raise SoulseekError("still waiting in the other user's queue")
                if elapsed > total_timeout:
                    raise SoulseekError("too slow")
            got = Path(transfer.local_path or part)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(got), str(dest))
            return dest
        except BaseException:
            leftover = transfer.local_path
            try:
                await self.client.transfers.remove(transfer)   # frees the peer's queue slot too
            except Exception:
                pass
            for f in {leftover, str(part)}:
                if f:
                    Path(f).unlink(missing_ok=True)
            raise

    def fetch_flac(self, meta, dest):
        """Find and download a FLAC for one track. Returns (path, peer) or raises SoulseekError."""
        candidates = self.find_flacs(meta)
        if not candidates:
            raise SoulseekError("no matching FLAC found")
        errors = []
        for username, remote, size in candidates:
            try:
                return self.download(username, remote, dest, size), username
            except SoulseekError as e:
                errors.append(f"{username}: {e}")
            except Exception as e:                       # a peer misbehaving shouldn't stop the run
                errors.append(f"{username}: {type(e).__name__}")
        raise SoulseekError("found, but couldn't download (" + "; ".join(errors[:3]) + ")")


# ---------------------------------------------------------------- sign-in

BROWSERS = {   # (label, yt-dlp name), in the order shown in the list
    "darwin": [("Chrome", "chrome"), ("Safari", "safari"), ("Firefox", "firefox"), ("Edge", "edge"),
               ("Brave", "brave"), ("Opera", "opera"), ("Vivaldi", "vivaldi")],
    "win32": [("Chrome", "chrome"), ("Edge", "edge"), ("Firefox", "firefox"), ("Brave", "brave"),
              ("Opera", "opera"), ("Vivaldi", "vivaldi")],
    "linux": [("Chrome", "chrome"), ("Firefox", "firefox"), ("Chromium", "chromium"), ("Brave", "brave"),
              ("Edge", "edge"), ("Opera", "opera"), ("Vivaldi", "vivaldi")],
}
# Firefox first: it never triggers a password prompt. Safari last: macOS usually blocks it.
AUTO_ORDER = ["firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium", "safari"]
AUTO_LABEL = "Find it automatically"


def platform_browsers():
    key = sys.platform if sys.platform in ("darwin", "win32") else "linux"
    return BROWSERS[key]


class _QuietLog:
    """Collects yt-dlp's cookie-reading messages instead of printing them."""

    def __init__(self):
        self.problems = []

    def debug(self, msg, *a, **k):
        pass

    info = debug

    def warning(self, msg, *a, **k):
        self.problems.append(str(msg))

    error = warning

    def progress_bar(self):
        return None


def browser_token(browser):
    """Read SoundCloud's sign-in token from a browser. Returns (token, problem_text)."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        return None, "yt-dlp isn't installed"
    log = _QuietLog()
    try:
        jar = extract_cookies_from_browser(browser, logger=log)
    except Exception as e:  # missing browser, locked database, macOS privacy block, …
        return None, str(e) or type(e).__name__
    best = None
    for c in jar:
        if c.name == "oauth_token" and c.value and c.domain.lstrip(".").endswith("soundcloud.com"):
            if best is None or (c.expires or 0) > (best.expires or 0):
                best = c
    if best:
        return best.value, None
    return None, " ".join(log.problems[-2:]) or "not signed in"


def explain_browser_problem(label, problem):
    p = (problem or "").lower()
    if "could not find" in p or "no such file" in p or "not installed" in p:
        return f"{label}: not found on this computer (or never opened)."
    if "operation not permitted" in p or ("permission" in p and sys.platform == "darwin"):
        return f"{label}: macOS doesn't let apps read its data. Choose another browser, or use another way."
    if "permission" in p or "being used" in p or "locked" in p or "copy" in p:
        return f"{label}: its data is locked while it's running. Quit {label} completely, then click Connect."
    if "decrypt" in p or "keyring" in p or "keychain" in p or "v20" in p or "app-bound" in p:
        if sys.platform == "darwin":
            return (f"{label}: access to its saved data was refused. Click Connect again and choose "
                    f"\"Always Allow\" when your Mac asks.")
        return f"{label}: it keeps its sign-in locked. Use Firefox, or use another way."
    if "not signed in" in p:
        return f"{label}: you're not signed in to SoundCloud there. Sign in at soundcloud.com, then click Connect."
    return f"{label}: {problem[:150]}"


class TokenRejected(Exception):
    pass


def verify_token(token):
    """Ask SoundCloud who this token belongs to. Raises TokenRejected if it isn't valid,
    or another exception if SoundCloud couldn't be reached."""
    from soundcloud import SoundCloud      # installed with scdl
    client = SoundCloud(None, None)
    client.auth_token = token
    try:
        me = client.get_me()
    except Exception as e:
        if getattr(getattr(e, "response", None), "status_code", None) in (401, 403):
            raise TokenRejected() from e
        raise
    if not me:
        raise TokenRejected()
    url = (me.permalink_url or f"https://soundcloud.com/{me.permalink}").rstrip("/")
    return {"username": me.username or me.permalink, "likes_url": url + "/likes"}


def clean_token(raw):
    t = (raw or "").strip().strip('"\'')
    if t.lower().startswith("oauth "):
        t = t[6:].strip()
    if t.lower().startswith("oauth_token="):
        t = t[12:].split(";")[0].strip()
    return t if t and " " not in t else None


# ---------------------------------------------------------------- icons & shortcuts

def _render_icon(n=512):
    """Orange rounded square with a white download arrow, as premultiplied RGBA floats."""
    ss = 2                                     # 2x2 supersampling for smooth edges
    big = n * ss
    px = [0.0] * (n * n * 4)
    lo, hi, rad = 0.1, 0.9, 0.18
    inner_lo, inner_hi = lo + rad, hi - rad
    w = 1.0 / (ss * ss)
    for j in range(big):
        v = (j + 0.5) / big
        if v < lo or v > hi:
            continue
        t = (v - lo) / (hi - lo)
        g = 136 - 85 * t                       # #FF8800 at the top to #FF3300 at the bottom
        dy = max(inner_lo - v, 0.0, v - inner_hi)
        head_half = 0.17 * (0.66 - v) / 0.22 if 0.44 <= v <= 0.66 else -1.0
        shaft = 0.24 <= v <= 0.50
        bar = 0.72 <= v <= 0.78
        row = (j // ss) * n
        for i in range(big):
            u = (i + 0.5) / big
            if u < lo or u > hi:
                continue
            dx = max(inner_lo - u, 0.0, u - inner_hi)
            if dx * dx + dy * dy > rad * rad:
                continue
            white = ((shaft and 0.44 <= u <= 0.56) or abs(u - 0.5) <= head_half or
                     (bar and (0.31 <= u <= 0.69 or (u - 0.31) ** 2 + (v - 0.75) ** 2 <= 0.0009
                               or (u - 0.69) ** 2 + (v - 0.75) ** 2 <= 0.0009)))
            k = (row + i // ss) * 4
            px[k] += 255 * w
            px[k + 1] += (255 if white else g) * w
            px[k + 2] += (255 if white else 0) * w
            px[k + 3] += 255 * w
    return px


def _half(px, n):
    m = n // 2
    out = [0.0] * (m * m * 4)
    for y in range(m):
        for x in range(m):
            a = ((2 * y) * n + 2 * x) * 4
            b = a + n * 4
            o = (y * m + x) * 4
            for c in range(4):
                out[o + c] = (px[a + c] + px[a + 4 + c] + px[b + c] + px[b + 4 + c]) / 4
    return out


def _png(px, n):
    raw = bytearray()
    for y in range(n):
        raw.append(0)
        for x in range(n):
            k = (y * n + x) * 4
            a = px[k + 3]
            if a <= 0:
                raw += b"\0\0\0\0"
            else:
                raw += bytes(min(255, round(px[k + c] * 255 / a)) for c in range(3)) + bytes([round(a)])

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def make_icons():
    """Returns {size: png_bytes} for 16…512, and writes icon.png / icon.ico to the app folder."""
    n, px = 512, _render_icon(512)
    pngs = {n: _png(px, n)}
    while n > 16:
        px, n = _half(px, n), n // 2
        pngs[n] = _png(px, n)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ICON_PNG.write_bytes(pngs[256])
    sizes = [16, 32, 64, 128, 256]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    entries, blobs, offset = b"", b"", 6 + 16 * len(sizes)
    for s in sizes:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(pngs[s]), offset)
        blobs += pngs[s]
        offset += len(pngs[s])
    ICON_ICO.write_bytes(header + entries + blobs)
    return pngs


def _desktop_quote(s):
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + s.replace("%", "%%") + '"'


def mac_app_dirs():
    if os.environ.get("SCD_APPLICATIONS_DIR"):          # for testing
        return [Path(os.environ["SCD_APPLICATIONS_DIR"])]
    return [Path("/Applications"), Path.home() / "Applications"]


def install_mac_app(pngs):
    """Make a double-clickable app in Applications that opens this window (no Terminal)."""
    for apps in mac_app_dirs():
        app = apps / f"{APP_NAME}.app"
        try:
            apps.mkdir(exist_ok=True)
            contents = app / "Contents"
            (contents / "MacOS").mkdir(parents=True, exist_ok=True)
            (contents / "Resources").mkdir(parents=True, exist_ok=True)
            break
        except OSError:
            continue
    else:
        return None
    iconset = APP_DIR / "icon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    for s in (16, 32, 128, 256):
        (iconset / f"icon_{s}x{s}.png").write_bytes(pngs[s])
        (iconset / f"icon_{s}x{s}@2x.png").write_bytes(pngs[s * 2])
    (iconset / "icon_512x512.png").write_bytes(pngs[512])
    run(["iconutil", "-c", "icns", "-o", str(contents / "Resources" / "icon.icns"), str(iconset)])
    shutil.rmtree(iconset, ignore_errors=True)

    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "local.soundcloud-downloader", "CFBundleVersion": "2.0",
            "CFBundleShortVersionString": "2.0", "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "launch", "CFBundleIconFile": "icon",
            "LSMinimumSystemVersion": "10.13", "NSHighResolutionCapable": True,
        }, f)
    launch = contents / "MacOS" / "launch"
    launch.write_text(
        '#!/bin/bash\n'
        '# Opens SoundCloud Downloader; if its private Python is missing or broken, re-runs setup.\n'
        'D="$HOME/Library/Application Support/SoundCloudDownloader"\n'
        'if "$D/env/bin/python" -c "import tkinter" >/dev/null 2>&1; then\n'
        '  exec "$D/env/bin/python" "$D/soundcloud_downloader.py"\n'
        'fi\n'
        'if [ -f "$D/setup.command" ]; then exec open -a Terminal "$D/setup.command"; fi\n'
        'osascript -e \'display alert "SoundCloud Downloader needs repairing" message '
        '"Please run the SoundCloud Downloader setup file again."\'\n',
        encoding="utf-8")
    launch.chmod(0o755)
    subprocess.run(["touch", str(app)])                     # make Finder pick up the icon
    lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                  "LaunchServices.framework/Support/lsregister")
    if os.path.exists(lsregister):
        run([lsregister, "-f", str(app)])
    return app


def install_linux_entry():
    """Add the app to the desktop's application menu."""
    apps = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = apps / "soundcloud-downloader.desktop"
    entry.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_NAME}\nComment=Download SoundCloud tracks and playlists as MP3\n"
        f"Exec={_desktop_quote(sys.executable)} {_desktop_quote(Path(__file__).resolve())}\n"
        f"Icon={ICON_PNG}\nTerminal=false\nCategories=AudioVideo;Audio;Network;\n"
        "StartupWMClass=SoundCloudDownloader\n", encoding="utf-8")
    entry.chmod(0o755)
    if shutil.which("update-desktop-database"):
        run(["update-desktop-database", str(apps)])
    return entry


def setup_shortcuts():
    """Called by the setup launchers: draws the icon and adds the app to the OS app list."""
    pngs = make_icons()
    if sys.platform == "darwin":
        app = install_mac_app(pngs)
        print(f"APP={app}" if app else "APP=")
    elif sys.platform.startswith("linux"):
        print(f"APP={install_linux_entry()}")
    else:
        print(f"ICON={ICON_ICO}")   # the Windows launcher makes the Start menu / Desktop shortcuts


# ---------------------------------------------------------------- app

def main():
    if "--setup" in sys.argv:
        setup_shortcuts()
        return

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    def add_edit_menu(widget):
        """Right-click Cut/Copy/Paste, which Tk entries don't have by default."""
        menu = tk.Menu(widget, tearoff=0)
        for label, event in (("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"), ("Paste", "<<Paste>>")):
            menu.add_command(label=label, command=lambda e=event: widget.event_generate(e))
        menu.add_separator()
        menu.add_command(label="Select all", command=lambda: (widget.select_range(0, "end"),
                                                              widget.icursor("end")))

        def show(event):
            widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)
        widget.bind("<Button-3>", show)
        if sys.platform == "darwin":
            widget.bind("<Button-2>", show)
            widget.bind("<Control-Button-1>", show)

    class App:
        def __init__(self, root):
            self.root = root
            self.q = queue.Queue()
            self.busy = False
            self.settings = load_settings()
            self.signin_win = None

            root.title(f"{APP_NAME}  ·  v{__version__}")
            root.minsize(580, 460)
            frm = ttk.Frame(root, padding=16)
            frm.pack(fill="both", expand=True)
            frm.columnconfigure(1, weight=1)

            ttk.Label(frm, text="SoundCloud link").grid(row=0, column=0, sticky="w", pady=(0, 8))
            self.url = tk.StringVar(value=self._clipboard_url())
            url_entry = ttk.Entry(frm, textvariable=self.url)
            url_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8))
            url_entry.focus_set()
            url_entry.icursor("end")
            add_edit_menu(url_entry)
            ttk.Button(frm, text="Paste", command=self.paste).grid(row=0, column=2, sticky="ew",
                                                                   padx=(8, 0), pady=(0, 8))

            ttk.Label(frm, text="Save to").grid(row=1, column=0, sticky="w", pady=(0, 8))
            self.folder = tk.StringVar(value=self.settings.get("folder") or str(DEFAULT_FOLDER))
            folder_entry = ttk.Entry(frm, textvariable=self.folder)
            folder_entry.grid(row=1, column=1, sticky="ew", pady=(0, 8))
            add_edit_menu(folder_entry)
            ttk.Button(frm, text="Browse…", command=self.browse).grid(row=1, column=2, sticky="ew",
                                                                     padx=(8, 0), pady=(0, 8))

            ttk.Label(frm, text="Parallel downloads").grid(row=2, column=0, sticky="w", pady=(0, 8))
            workers = self.settings.get("workers")
            self.workers = tk.IntVar(value=workers if isinstance(workers, int) and 1 <= workers <= MAX_WORKERS
                                     else DEFAULT_WORKERS)
            ttk.Spinbox(frm, from_=1, to=MAX_WORKERS, textvariable=self.workers, width=5,
                        state="readonly").grid(row=2, column=1, sticky="w", pady=(0, 8))

            ttk.Label(frm, text="Account").grid(row=3, column=0, sticky="w")
            acct = ttk.Frame(frm)
            acct.grid(row=3, column=1, sticky="w")
            self.acct_text = tk.StringVar()
            ttk.Label(acct, textvariable=self.acct_text).pack(side="left")
            self.likes_btn = ttk.Button(acct, text="Download my likes", command=self.my_likes)
            self.sign_btn = ttk.Button(frm, command=self.sign_in_or_out)
            self.sign_btn.grid(row=3, column=2, sticky="ew", padx=(8, 0))

            ttk.Label(frm, text="Soulseek").grid(row=4, column=0, sticky="w", pady=(8, 0))
            self.slsk_on = tk.BooleanVar(value=bool(self.settings.get("slsk_enabled")))
            self.slsk_check = ttk.Checkbutton(frm, variable=self.slsk_on, command=self.toggle_soulseek)
            self.slsk_check.grid(row=4, column=1, sticky="w", pady=(8, 0))
            self.slsk_btn = ttk.Button(frm, text="Settings…", command=self.open_soulseek)
            self.slsk_btn.grid(row=4, column=2, sticky="ew", padx=(8, 0), pady=(8, 0))
            self.slsk = None                  # Soulseek session, started on first use, kept while the app is open
            self.slsk_key = None
            self.slsk_lock = threading.Lock()
            self.slsk_win = None

            self.go = ttk.Button(frm, text="Download", command=self.start)
            self.go.grid(row=5, column=1, columnspan=2, sticky="e", pady=(14, 0))
            root.bind("<Return>", lambda e: self.start())

            self.bar = ttk.Progressbar(frm, mode="determinate")
            self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 4))
            self.status = tk.StringVar(value="Paste a track, playlist or profile link.")
            ttk.Label(frm, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky="w")

            self.log = tk.Text(frm, height=10, state="disabled", wrap="word")
            self.log.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
            frm.rowconfigure(8, weight=1)

            self.fix_btn = ttk.Button(frm, text=f"Fix album art in a folder (max {MAX_ART}×{MAX_ART})…",
                                      command=self.fix_folder)
            self.fix_btn.grid(row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))
            root.protocol("WM_DELETE_WINDOW", self.on_close)

            self.refresh_account()
            self.refresh_soulseek()
            setup_ffmpeg()
            self.root.after(100, self.poll)
            if managed_install():           # never touch someone's own Python setup
                self.set_busy(True, "Checking for updates…")
                threading.Thread(target=self.run_updates, daemon=True).start()

        # ---- small UI helpers

        def _clipboard_url(self):
            try:
                return clean_url(self.root.clipboard_get()) or ""
            except tk.TclError:
                return ""

        def paste(self):
            try:
                text = self.root.clipboard_get().strip()
            except tk.TclError:
                text = ""
            if not text:
                messagebox.showinfo("Nothing to paste",
                                    "Copy a link from SoundCloud first (in your browser, click Share → "
                                    "Copy link, or copy the address bar), then click Paste.")
                return
            self.url.set(clean_url(text) or text)

        def browse(self):
            chosen = filedialog.askdirectory(initialdir=self.folder.get() or str(Path.home()))
            if chosen:
                self.folder.set(chosen)

        def write(self, line):
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        def clear_log(self):
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")

        def set_busy(self, busy, status=None):
            self.busy = busy
            state = "disabled" if busy else "normal"
            for b in (self.go, self.fix_btn, self.sign_btn, self.likes_btn, self.slsk_check, self.slsk_btn):
                b.configure(state=state)
            if status:
                self.status.set(status)

        def poll(self):
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    getattr(self, "on_" + kind)(payload)
            except queue.Empty:
                pass
            self.root.after(100, self.poll)

        def run_updates(self):
            """Runs at every launch: the app itself, then uv, Python, and all Python packages."""
            notes = []

            def step(what, fn):
                self.q.put(("status", f"Checking for updates: {what}…"))
                try:
                    return fn(), None
                except Exception as e:
                    return None, e

            version, err = step("app", update_app)
            if version:
                self.q.put(("restart", version))
                return
            if err is not None and is_offline_error(err):
                self.q.put(("updated", ["Couldn't check for updates (no internet?). "
                                        "Everything still works; it'll check again next time."]))
                return
            if err is not None:
                notes.append(f"Couldn't update the app: {err}")
            for what, fn, label in (("setup helper", update_uv, "setup helper (uv)"),
                                    ("Python", update_python, "Python"),
                                    ("downloader", update_packages, None)):
                result, err = step(what, fn)
                if err is not None:
                    notes.append(f"Couldn't update the {what}: {str(err)[:160]}")
                elif result and label:
                    notes.append(f"Updated {label} to {result}.")
                elif result:
                    notes.append("Updated " + ", ".join(result) + ".")
            self.q.put(("updated", notes))

        def on_status(self, text):
            self.status.set(text)

        def on_updated(self, notes):
            setup_ffmpeg()                     # the bundled ffmpeg may have moved after an upgrade
            for n in notes:
                self.write(n)
            if notes:
                self.write("")
            self.set_busy(False, "Up to date. Paste a track, playlist or profile link.")

        def on_restart(self, version):
            self.status.set(f"Updated to version {version}. Restarting…")
            self.write(f"Installed SoundCloud Downloader {version}. Restarting…")

            def relaunch():
                # Refresh icons/shortcuts with the new version, then start it and close this window.
                run([console_python(), str(APP_FILE), "--setup"])
                subprocess.Popen([sys.executable, str(APP_FILE)], cwd=str(APP_DIR), close_fds=True,
                                 start_new_session=(os.name != "nt"))
                self.root.destroy()
            self.root.after(800, relaunch)

        @property
        def token(self):
            return self.settings.get("auth_token") or None

        # ---- Soulseek

        def slsk_ready(self):
            return bool(self.settings.get("slsk_user") and self.settings.get("slsk_pass"))

        def refresh_soulseek(self):
            if self.slsk_on.get() and self.slsk_ready():
                text = f"Also get lossless FLACs (as {self.settings['slsk_user']})"
            elif self.slsk_on.get():
                text = "Also get lossless FLACs (set up your Soulseek login first)"
            else:
                text = "Also look for lossless FLACs on Soulseek"
            self.slsk_check.configure(text=text)

        def toggle_soulseek(self):
            if self.slsk_on.get() and not self.slsk_ready():
                self.slsk_on.set(False)
                self.open_soulseek()
                return
            self.settings["slsk_enabled"] = self.slsk_on.get()
            save_settings(self.settings)
            self.refresh_soulseek()

        def soulseek_share_dir(self, dest=None):
            if not self.settings.get("slsk_share", True):
                return None
            return Path(dest or Path(self.folder.get()).expanduser())

        def get_soulseek(self, dest):
            """The running Soulseek session (started if needed). Called from worker threads."""
            share = self.soulseek_share_dir(dest)
            key = (self.settings.get("slsk_user"), self.settings.get("slsk_pass"), str(share) if share else None)
            with self.slsk_lock:
                if self.slsk and self.slsk_key == key:
                    return self.slsk
                if self.slsk:
                    self.slsk.stop()
                    self.slsk = None
                if share:
                    share.mkdir(parents=True, exist_ok=True)
                self.slsk = Soulseek(key[0], key[1], share).start()
                self.slsk_key = key
                return self.slsk

        def on_close(self):
            if self.slsk:
                threading.Thread(target=self.slsk.stop, daemon=True).start()
            self.root.after(300, self.root.destroy)

        def open_soulseek(self):
            if self.slsk_win is not None and self.slsk_win.winfo_exists():
                self.slsk_win.lift()
                return
            win = self.slsk_win = tk.Toplevel(self.root)
            win.title("Soulseek (lossless FLAC)")
            win.transient(self.root)
            win.resizable(False, False)
            frm = ttk.Frame(win, padding=18)
            frm.pack(fill="both", expand=True)
            wrap = 470
            ttk.Label(frm, text="Get lossless FLACs from Soulseek",
                      font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
            ttk.Label(frm, wraplength=wrap, justify="left", foreground="gray", text=(
                "When this is on, every SoundCloud track is also searched on Soulseek, a music file-sharing "
                f"network. A matching FLAC is saved in a \"{FLAC_FOLDER}\" folder inside your download folder, "
                "next to the MP3s. Many tracks, especially SoundCloud-only edits, won't be found, and "
                "downloads from other people can be slow.")).grid(row=1, column=0, columnspan=2, sticky="w",
                                                                   pady=(4, 12))
            user = tk.StringVar(value=self.settings.get("slsk_user", ""))
            pw = tk.StringVar(value=self.settings.get("slsk_pass", ""))
            share = tk.BooleanVar(value=self.settings.get("slsk_share", True))
            ttk.Label(frm, text="Username").grid(row=2, column=0, sticky="w", pady=3)
            user_entry = ttk.Entry(frm, textvariable=user, width=32)
            user_entry.grid(row=2, column=1, sticky="ew", pady=3)
            ttk.Label(frm, text="Password").grid(row=3, column=0, sticky="w", pady=3)
            ttk.Entry(frm, textvariable=pw, show="•", width=32).grid(row=3, column=1, sticky="ew", pady=3)
            add_edit_menu(user_entry)
            ttk.Label(frm, wraplength=wrap, justify="left", foreground="gray", text=(
                "No account? Just choose a new username and password: Soulseek creates the account the first "
                "time you connect. Check the spelling, because a typo quietly creates a different account."
            )).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 10))
            ttk.Checkbutton(frm, variable=share, text="Share my download folder with other Soulseek users"
                            ).grid(row=5, column=0, columnspan=2, sticky="w")
            ttk.Label(frm, wraplength=wrap, justify="left", foreground="gray", text=(
                "Recommended: many Soulseek users refuse people who share nothing. Sharing only happens while "
                "this app is open. Only share music you have the right to share."
                + ("\nYour Mac may ask whether Python may accept incoming connections: click Allow."
                   if sys.platform == "darwin" else
                   "\nWindows may ask to allow Python through the firewall: click Allow."
                   if sys.platform == "win32" else "")
            )).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 10))
            status = tk.StringVar()
            ttk.Label(frm, textvariable=status, wraplength=wrap, justify="left").grid(
                row=7, column=0, columnspan=2, sticky="w")
            btns = ttk.Frame(frm)
            btns.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(12, 0))

            def store():
                u, p = user.get().strip(), pw.get()
                if not u or not p:
                    status.set("Enter a username and a password.")
                    return False
                self.settings.update(slsk_user=u, slsk_pass=p, slsk_share=share.get())
                save_settings(self.settings)
                return True

            def test():
                if not store():
                    return
                status.set("Connecting to Soulseek…")
                test_btn.configure(state="disabled")

                def work():
                    try:
                        self.get_soulseek(None)
                        msg = (f"Connected as {self.settings['slsk_user']}. "
                               "(If this username was new, the account now exists.)")
                    except SoulseekError as e:
                        msg = str(e)
                    except Exception as e:
                        msg = f"Couldn't connect: {e}"
                    self.q.put(("slsk_status", msg))
                threading.Thread(target=work, daemon=True).start()

            def save():
                if not store():
                    return
                self.slsk_on.set(True)
                self.settings["slsk_enabled"] = True
                save_settings(self.settings)
                self.refresh_soulseek()
                win.destroy()

            test_btn = ttk.Button(btns, text="Test connection", command=test)
            test_btn.pack(side="left")
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
            ttk.Button(btns, text="Save and turn on", command=save).pack(side="right", padx=(0, 8))
            win.slsk_status = lambda text: (status.set(text), test_btn.configure(state="normal"))
            user_entry.focus_set()

        def on_slsk_status(self, text):
            if self.slsk_win is not None and self.slsk_win.winfo_exists():
                self.slsk_win.slsk_status(text)

        def soulseek_target(self, info, dest, i, meta):
            title = str(meta.get("title") or f"Track {i}")
            user = str(meta.get("username") or "")
            name = title if re.search(r"\s[-–—]\s", title) or not user else f"{user} - {title}"
            folder = dest / FLAC_FOLDER
            if info["playlist"]:
                folder = folder / safe_filename(info["title"], "Playlist")
                name = f"{i:0{len(str(info['count']))}d}. {name}"
            return folder / f"{safe_filename(name)}.flac"

        def soulseek_track(self, info, dest, i, results):
            """Try to get a FLAC for track i; logs the outcome and records it in results."""
            meta = (info.get("meta") or {}).get(i)
            if not meta:
                results.append((i, "skipped", None))
                return
            target = self.soulseek_target(info, dest, i, meta)
            label = target.stem
            if target.exists():
                results.append((i, "found", target))
                self.q.put(("log", f"♫ FLAC already downloaded: {label}"))
                return
            try:
                session = self.get_soulseek(dest)
            except Exception as e:
                results.append((i, "error", None))
                self.q.put(("log", f"✗ Soulseek: {e}"))
                return
            try:
                path, peer = session.fetch_flac(meta, target)
                results.append((i, "found", path))
                self.q.put(("log", f"♫ FLAC from Soulseek: {label} ({path.stat().st_size / 1e6:.0f} MB, from {peer})"))
            except SoulseekError as e:
                results.append((i, "missing", None))
                self.q.put(("log", f"– No FLAC for {label}: {e}"))
            except Exception as e:
                results.append((i, "missing", None))
                self.q.put(("log", f"– No FLAC for {label}: {type(e).__name__}: {e}"))

        # ---- account

        def refresh_account(self):
            if self.token:
                self.acct_text.set(f"Signed in as {self.settings.get('username') or 'you'}   ")
                self.likes_btn.pack(side="left")
                self.sign_btn.configure(text="Sign out")
            else:
                self.acct_text.set("Not signed in (only needed for likes, private or Go+ tracks)")
                self.likes_btn.pack_forget()
                self.sign_btn.configure(text="Sign in…")

        def sign_in_or_out(self):
            if self.busy:
                return
            if not self.token:
                self.open_signin()
                return
            if messagebox.askyesno("Sign out?", "Disconnect your SoundCloud account from this app?\n\n"
                                                "(This doesn't sign you out in your browser.)"):
                for k in ("auth_token", "username", "likes_url"):
                    self.settings.pop(k, None)
                save_settings(self.settings)
                self.refresh_account()

        def my_likes(self):
            url = self.settings.get("likes_url")
            if url and not self.busy:
                self.url.set(url)
                self.start()

        def open_signin(self):
            if self.signin_win is not None and self.signin_win.winfo_exists():
                self.signin_win.lift()
                return
            win = self.signin_win = tk.Toplevel(self.root)
            win.title("Sign in to SoundCloud")
            win.transient(self.root)
            win.resizable(False, False)
            frm = ttk.Frame(win, padding=18)
            frm.pack(fill="both", expand=True)
            wrap = 470

            ttk.Label(frm, text="Connect your SoundCloud account",
                      font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
            ttk.Label(frm, wraplength=wrap, justify="left", foreground="gray",
                      text="This lets you download your own likes, private tracks you have access to, "
                           "and full Go+ tracks if you subscribe. Your password is never seen or stored "
                           "by this app; it only borrows the sign-in your browser already has.").pack(
                anchor="w", pady=(4, 12))

            step1 = ttk.Frame(frm)
            step1.pack(fill="x")
            ttk.Label(step1, text="1.  Sign in at soundcloud.com in your web browser.").pack(side="left")
            ttk.Button(step1, text="Open SoundCloud",
                       command=lambda: webbrowser.open(SIGNIN_URL)).pack(side="right")

            step2 = ttk.Frame(frm)
            step2.pack(fill="x", pady=(10, 0))
            ttk.Label(step2, text="2.  Choose that browser:").pack(side="left")
            labels = [AUTO_LABEL] + [l for l, _ in platform_browsers()]
            choice = tk.StringVar(value=labels[0])
            ttk.Combobox(step2, textvariable=choice, values=labels, state="readonly",
                         width=20).pack(side="left", padx=(8, 0))
            connect = ttk.Button(step2, text="Connect")
            connect.pack(side="right")

            if sys.platform == "darwin":
                tip = ("If your Mac asks to allow access to \"… Safe Storage\", enter your Mac password and "
                       "click Always Allow. Safari can't be used; choose another browser or the other way below.")
            elif sys.platform == "win32":
                tip = ("Firefox works best. Chrome and Edge on Windows lock their sign-in; if Connect "
                       "doesn't work with them, use the other way below.")
            else:
                tip = "If your system asks to unlock your keyring, allow it."
            ttk.Label(frm, text=tip, wraplength=wrap, justify="left",
                      foreground="gray").pack(anchor="w", pady=(8, 0))

            status = tk.StringVar()
            ttk.Label(frm, textvariable=status, wraplength=wrap, justify="left").pack(anchor="w", pady=(10, 0))

            more_btn = ttk.Button(frm, text="Show me another way…")
            more_btn.pack(anchor="w", pady=(12, 0))
            manual = ttk.Frame(frm)
            ttk.Label(manual, text="Paste your sign-in token", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
            devtools = "Option+Cmd+I" if sys.platform == "darwin" else "F12 (or Ctrl+Shift+I)"
            steps = ("1. In Chrome, Edge, Firefox or Brave, go to soundcloud.com and make sure you're signed in.\n"
                     f"2. Press {devtools} to open the developer tools.\n"
                     "3. Click the \"Application\" tab (Firefox: \"Storage\"), then Cookies → "
                     "https://soundcloud.com.\n"
                     "4. Find the row named oauth_token, double-click its Value and copy it.\n"
                     "    It looks like 2-123456-12345678-AbCdEfGhIjKlMn\n"
                     "5. Paste it below and click Use token. Treat it like a password.")
            ttk.Label(manual, text=steps, wraplength=wrap, justify="left").pack(anchor="w", pady=(4, 6))
            row = ttk.Frame(manual)
            row.pack(fill="x")
            token_var = tk.StringVar()
            token_entry = ttk.Entry(row, textvariable=token_var, show="•")
            token_entry.pack(side="left", fill="x", expand=True)
            add_edit_menu(token_entry)
            use = ttk.Button(row, text="Use token")
            use.pack(side="left", padx=(8, 0))

            close = ttk.Button(frm, text="Close", command=win.destroy)
            close.pack(anchor="e", pady=(14, 0))

            def show_manual():
                more_btn.pack_forget()
                manual.pack(fill="x", pady=(12, 0), before=close)
                token_entry.focus_set()
            more_btn.configure(command=show_manual)

            def working(on, text=""):
                for b in (connect, use):
                    b.configure(state="disabled" if on else "normal")
                status.set(text)

            def do_connect():
                picked = choice.get()
                names = dict(platform_browsers())
                order = ([names[picked]] if picked in names else
                         [n for n in AUTO_ORDER if n in names.values()])
                working(True, "Looking for your SoundCloud sign-in…")
                threading.Thread(target=self.find_signin, args=(order,), daemon=True).start()

            def do_token():
                tok = clean_token(token_var.get())
                if not tok:
                    status.set("That doesn't look like a token. Copy just the Value of oauth_token.")
                    return
                working(True, "Checking with SoundCloud…")
                threading.Thread(target=self.check_token, args=(tok, "token"), daemon=True).start()

            connect.configure(command=do_connect)
            use.configure(command=do_token)
            win.signin_status = lambda text, busy=False: working(busy, text)

        def find_signin(self, order):
            labels = {n: l for l, n in platform_browsers()}
            problems = []
            for name in order:
                label = labels.get(name, name)
                self.q.put(("signin_status", (f"Checking {label}…", True)))
                tok, problem = browser_token(name)
                if tok:
                    self.q.put(("signin_status", (f"Found a sign-in in {label}. Checking with SoundCloud…", True)))
                    problem = self.check_token(tok, label, report_failure=False)
                    if problem is None:
                        return
                    problems.append(f"{label}: {problem}")
                    continue
                text = explain_browser_problem(label, problem)
                if len(order) == 1 or "not found on this computer" not in text:
                    problems.append(text)
            if not problems:
                problems = ["Couldn't find a SoundCloud sign-in in any browser. Sign in at soundcloud.com "
                            "first, then click Connect."]
            self.q.put(("signin_status", ("\n".join(problems[:4]), False)))

        def check_token(self, token, via, report_failure=True):
            """Verify a token; on success signs in and returns None, otherwise returns the problem."""
            try:
                acct = verify_token(token)
            except TokenRejected:
                problem = ("SoundCloud says this sign-in has expired. Sign in again at soundcloud.com, then "
                           "try again." if via != "token" else
                           "SoundCloud didn't accept that token. Make sure you copied the whole oauth_token "
                           "value while signed in.")
            except Exception as e:
                problem = ("Couldn't reach SoundCloud to check the sign-in. Check your internet connection "
                           f"and try again. ({(str(e) or type(e).__name__)[:120]})")
            else:
                self.q.put(("signed_in", dict(acct, auth_token=token, via=via)))
                return None
            if report_failure:
                self.q.put(("signin_status", (problem, False)))
            return problem

        def on_signin_status(self, payload):
            text, busy = payload
            if self.signin_win is not None and self.signin_win.winfo_exists():
                self.signin_win.signin_status(text, busy)

        def on_signed_in(self, acct):
            self.settings.update(auth_token=acct["auth_token"], username=acct["username"],
                                 likes_url=acct["likes_url"])
            save_settings(self.settings)
            self.refresh_account()
            if self.signin_win is not None and self.signin_win.winfo_exists():
                self.signin_win.destroy()
            messagebox.showinfo("Signed in", f"Connected as {acct['username']}.\n\nPaste any link as usual, "
                                             "or click \"Download my likes\".")

        # ---- flow: click → check → probe (thread) → confirm → download → convert → report

        def start(self):
            if self.busy:
                return
            url = clean_url(self.url.get())
            if not url:
                messagebox.showwarning("Not a SoundCloud link",
                                       "Paste a soundcloud.com link to a track, playlist or profile.")
                return
            if not setup_ffmpeg():
                messagebox.showerror("ffmpeg missing",
                                     "ffmpeg (used to make MP3s) is missing.\n\n"
                                     "Run the SoundCloud Downloader setup file again to repair it.")
                return
            need_install = find_scdl() is None
            if need_install and managed_install():
                messagebox.showerror("Downloader missing",
                                     "Part of the app is missing.\n\nRun the SoundCloud Downloader "
                                     "setup file again to repair it.")
                return
            if need_install and not messagebox.askyesno("Install scdl?",
                                                         "scdl isn't installed. Install it now with pip?"):
                return

            self.settings.update(folder=self.folder.get(), workers=self.workers.get())
            save_settings(self.settings)
            self.url.set(url)
            self.clear_log()
            self.bar.configure(value=0, maximum=1)
            self.set_busy(True, "Looking up link…")
            threading.Thread(target=self.lookup, args=(url, need_install, self.token), daemon=True).start()

        def lookup(self, url, need_install, token):
            try:
                if need_install:
                    self.q.put(("log", "Installing scdl…"))
                    r = run([console_python(), "-m", "pip", "install", "--upgrade", *PACKAGES])
                    if r.returncode != 0 or not find_scdl():
                        raise RuntimeError("Couldn't install scdl: " + last_line(r.stderr))
                full = expand_short_url(url)
                if full != url:
                    self.q.put(("expanded", full))
                    url = full
                info = probe(url, token)
                info["url"] = url
                info["token"] = token
                info["soulseek"] = bool(self.settings.get("slsk_enabled") and self.slsk_ready())
                if info["soulseek"]:
                    try:
                        info["meta"] = track_metadata(info, token)
                    except Exception as e:
                        info["meta"] = {}
                        self.q.put(("log", f"(Couldn't read track details for Soulseek: {e})"))
                self.q.put(("probed", info))
            except Exception as e:
                self.q.put(("error", friendly_reason(str(e), bool(token))))

        def on_expanded(self, url):
            self.url.set(url)

        def on_probed(self, info):
            if info["count"] == 0:
                self.on_error("That link has no downloadable tracks (it may be empty or private).")
                return
            dest = Path(self.folder.get()).expanduser()
            what = f"{info['count']} tracks" if info["playlist"] else "1 track"
            by = f"\nby {info['by']}" if info["by"] else ""
            extra = "\n+ lossless FLACs from Soulseek when found" if info.get("soulseek") else ""
            ok = messagebox.askyesno("Confirm download",
                                     f"{info['title']}{by}\n\n{what} as MP3{extra}\n\nSave to:\n{dest}\n\nDownload now?")
            if not ok:
                self.set_busy(False, "Cancelled.")
                return
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self.on_error(f"Can't use that folder: {e}")
                return
            self.launch(info, dest, list(range(1, info["count"] + 1)))

        def launch(self, info, dest, items):
            self.bar.configure(value=0, maximum=len(items))
            what = f"{len(items)} tracks" if len(items) > 1 else "1 track"
            self.set_busy(True, f"Downloading {what}…")
            threading.Thread(target=self.download, args=(info, dest, self.workers.get(), items),
                             daemon=True).start()

        def download(self, info, dest, workers, items):
            started = time.time() - 5           # small margin for filesystem timestamp rounding
            scdl = find_scdl()
            archive = dest / "scdl_archive.txt"
            url, token = info["url"], info.get("token")
            failures = []                       # dicts: index, name, reason, retryable
            flac_results = []                   # (index, "found"|"missing"|"error"|"skipped", path)

            def fetch(i):
                item = i if info["playlist"] else None
                r = run(scdl_cmd(scdl, url, dest, archive, item, token))
                err = r.stderr or ""
                if info.get("soulseek"):
                    self.soulseek_track(info, dest, i, flac_results)
                if r.returncode == 0 and "ERROR:" not in err:
                    return i, None
                name = info["titles"].get(i) or lookup_title(url, item, token) or f"Track #{i}"
                return i, {"index": i, "name": name,
                           "reason": friendly_reason(err or r.stdout, bool(token)), "retryable": True}

            total = len(items)
            with ThreadPoolExecutor(max_workers=workers if info["playlist"] else 1) as pool:
                futures = [pool.submit(fetch, i) for i in items]
                for done, fut in enumerate(as_completed(futures), 1):
                    i, fail = fut.result()
                    if fail:
                        failures.append(fail)
                        self.q.put(("log", f"✗ {fail['name']} — {fail['reason']}"))
                    else:
                        label = info["titles"].get(i) or (f"track #{i}" if info["playlist"] else info["title"])
                        self.q.put(("log", f"✓ {label}"))
                    self.q.put(("progress", (done, total, "downloaded")))

            # Anything that came down as M4A/Opus/etc. gets converted to MP3.
            to_convert = [p for p in dest.rglob("*")
                          if p.is_file() and p.suffix.lower() in NON_MP3_AUDIO and p.stat().st_mtime >= started
                          and p.relative_to(dest).parts[0] != FLAC_FOLDER]     # Soulseek FLACs stay FLAC
            renamed = []
            if to_convert:
                self.q.put(("phase", (f"Converting {len(to_convert)} file(s) to MP3…", len(to_convert))))
                with ThreadPoolExecutor(max_workers=max(1, min(workers, os.cpu_count() or 2))) as pool:
                    futures = [pool.submit(to_mp3, p) for p in to_convert]
                    for done, fut in enumerate(as_completed(futures), 1):
                        src, out, err = fut.result()
                        if out:
                            renamed.append((src, out))
                            self.q.put(("log", f"♪ converted {out.name}"))
                        else:
                            failures.append({"index": None, "name": src.stem, "retryable": False,
                                             "reason": f"Downloaded but couldn't convert to MP3 "
                                                       f"(original {src.suffix} file kept): {err}"})
                            self.q.put(("log", f"✗ couldn't convert {src.name} — {err}"))
                        self.q.put(("progress", (done, len(to_convert), "converted")))
                fix_playlist_files(dest, started, renamed)

            # Shrink album art on everything this run produced, so rekordbox imports cleanly.
            new_mp3s = [p for p in dest.rglob("*.mp3") if p.is_file() and p.stat().st_mtime >= started]
            new_flacs = [p for _, state, p in flac_results if state == "found" and p and p.exists()
                         and p.stat().st_mtime >= started]
            failures += self.resize_art(new_mp3s + new_flacs, workers)

            failures.sort(key=lambda f: (f["index"] is None, f["index"] or 0, f["name"].lower()))
            flac = None
            if info.get("soulseek"):
                flac = {"found": sum(1 for _, st, _ in flac_results if st == "found"), "total": len(items)}
            self.q.put(("finished", {"info": info, "dest": dest, "total": total, "failures": failures,
                                     "flac": flac}))

        def resize_art(self, mp3s, workers):
            """Run fix_cover over files with progress; returns failure entries for the report."""
            if not mp3s:
                return []
            failures, resized = [], 0
            self.q.put(("phase", (f"Checking album art on {len(mp3s)} file(s)…", len(mp3s))))
            with ThreadPoolExecutor(max_workers=max(1, min(workers, os.cpu_count() or 2))) as pool:
                futures = [pool.submit(fix_cover, p) for p in mp3s]
                for done, fut in enumerate(as_completed(futures), 1):
                    path, status, err = fut.result()
                    if status == "resized":
                        resized += 1
                        self.q.put(("log", f"▣ art resized: {path.name}"))
                    elif status == "failed":
                        failures.append({"index": None, "name": path.stem, "retryable": False,
                                         "reason": f"Song is saved, but its album art couldn't be resized: {err}"})
                        self.q.put(("log", f"✗ couldn't resize art in {path.name} — {err}"))
                    self.q.put(("progress", (done, len(mp3s), "checked for album art")))
            self.q.put(("log", f"Album art: {resized} resized to fit {MAX_ART}×{MAX_ART}, "
                               f"{len(mp3s) - resized - len(failures)} already fine."))
            return failures

        # ---- fixing art in a folder of existing MP3s

        def fix_folder(self):
            if self.busy:
                return
            if not setup_ffmpeg():
                messagebox.showerror("ffmpeg missing", "ffmpeg is required to fix album art.\n\n"
                                                       "Run the SoundCloud Downloader setup file again.")
                return
            start_dir = self.folder.get() if Path(self.folder.get()).expanduser().is_dir() else str(Path.home())
            chosen = filedialog.askdirectory(title="Folder with MP3s to fix", initialdir=start_dir)
            if not chosen:
                return
            folder = Path(chosen)
            mp3s = [p for p in folder.rglob("*.mp3") if p.is_file()]
            if not mp3s:
                messagebox.showinfo("No MP3s", f"No MP3 files found in:\n{folder}")
                return
            if not messagebox.askyesno("Fix album art",
                                       f"Check {len(mp3s)} MP3(s) in\n{folder}\n(including subfolders)\n\n"
                                       f"Any album art bigger than {MAX_ART}×{MAX_ART} will be shrunk. "
                                       "The audio itself isn't changed.\n\nContinue?"):
                return
            self.clear_log()
            self.set_busy(True, "Checking album art…")

            def work():
                fails = self.resize_art(mp3s, self.workers.get())
                self.q.put(("artdone", (folder, len(mp3s), fails)))

            threading.Thread(target=work, daemon=True).start()

        def on_artdone(self, payload):
            folder, count, fails = payload
            if not fails:
                self.set_busy(False, f"Album art fixed — {count} file(s) checked.")
                messagebox.showinfo("Done", f"Checked {count} MP3(s). All album art now fits within "
                                            f"{MAX_ART}×{MAX_ART}.")
                return
            self.set_busy(False, f"{len(fails)} file(s) couldn't be fixed.")
            names = "\n".join(f"• {f['name']}" for f in fails[:20])
            more = f"\n…and {len(fails) - 20} more (see the log)" if len(fails) > 20 else ""
            messagebox.showwarning("Some files couldn't be fixed",
                                   f"{len(fails)} of {count} file(s) couldn't be fixed:\n\n{names}{more}")

        # ---- queue handlers (run on the UI thread)

        def on_log(self, line):
            self.write(line)

        def on_phase(self, payload):
            text, maximum = payload
            self.bar.configure(value=0, maximum=maximum)
            self.status.set(text)

        def on_progress(self, payload):
            done, total, verb = payload
            self.bar.configure(value=done)
            self.status.set(f"{done}/{total} {verb}")

        def on_error(self, msg):
            self.write("Error: " + msg)
            self.set_busy(False, "Something went wrong.")
            messagebox.showerror("Couldn't download", msg)

        def on_finished(self, result):
            dest, total, failures = result["dest"], result["total"], result["failures"]
            report_file = dest / REPORT_NAME
            dl_failed = [f for f in failures if f["retryable"]]
            ok = total - len(dl_failed)

            flac = result.get("flac")
            flac_note = (f"\nLossless FLAC found on Soulseek for {flac['found']} of {flac['total']} "
                         f"(in the \"{FLAC_FOLDER}\" folder)." if flac else "")
            if flac:
                self.write(flac_note.strip())
            if not failures:
                report_file.unlink(missing_ok=True)      # clear a stale report from an earlier run
                self.set_busy(False, f"All {total} downloaded as MP3." + (" " + flac_note.strip() if flac else ""))
                if messagebox.askyesno("Done", f"All {total} downloaded as MP3.{flac_note}\n\nOpen the folder?"):
                    open_folder(dest)
                return

            self.set_busy(False, f"{ok}/{total} downloaded — {len(failures)} problem(s)")
            lines = []
            for f in failures:
                num = f"#{f['index']}  " if f["index"] and result["info"]["playlist"] else ""
                lines.append(f"• {num}{f['name']}\n    {f['reason']}")
            body = "\n\n".join(lines)
            try:
                report_file.write_text(
                    f"{result['info']['title']} — {datetime.now():%Y-%m-%d %H:%M}\n"
                    f"{result['info']['url']}\n\n{body}\n", encoding="utf-8")
            except OSError:
                pass
            self.show_report(result, ok, body, report_file)

        def show_report(self, result, ok, body, report_file):
            info, dest, total, failures = result["info"], result["dest"], result["total"], result["failures"]
            retry_items = [f["index"] for f in failures if f["retryable"]]

            win = tk.Toplevel(self.root)
            win.title("Some songs didn't download")
            win.transient(self.root)
            win.minsize(480, 300)
            frm = ttk.Frame(win, padding=16)
            frm.pack(fill="both", expand=True)

            ttk.Label(frm, text=f"{ok} of {total} downloaded. {len(failures)} didn't make it:",
                      font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
            ttk.Label(frm, text=f"This list is also saved as {report_file.name} in the download folder.",
                      foreground="gray").pack(anchor="w", pady=(2, 8))

            box = ttk.Frame(frm)
            box.pack(fill="both", expand=True)
            txt = tk.Text(box, height=14, width=72, wrap="word")
            scroll = ttk.Scrollbar(box, command=txt.yview)
            txt.configure(yscrollcommand=scroll.set)
            txt.insert("1.0", body)
            txt.configure(state="disabled")
            txt.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

            btns = ttk.Frame(frm)
            btns.pack(fill="x", pady=(12, 0))

            def copy():
                self.root.clipboard_clear()
                self.root.clipboard_append(body)

            def retry():
                win.destroy()
                self.clear_log()
                self.write(f"Retrying {len(retry_items)} track(s)…")
                self.launch(info, dest, retry_items)

            ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")
            ttk.Button(btns, text="Open folder", command=lambda: open_folder(dest)).pack(side="right", padx=(0, 8))
            ttk.Button(btns, text="Copy list", command=copy).pack(side="right", padx=(0, 8))
            if retry_items:
                ttk.Button(btns, text=f"Retry failed ({len(retry_items)})",
                           command=retry).pack(side="left")

    if sys.platform == "win32":
        try:   # show our own icon in the taskbar instead of grouping under Python
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SoundCloudDownloader.App")
        except Exception:
            pass
    root = tk.Tk(className="SoundCloudDownloader")
    if sys.platform.startswith("linux"):
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
    try:
        if sys.platform == "win32" and ICON_ICO.exists():
            root.iconbitmap(default=str(ICON_ICO))
        elif ICON_PNG.exists():
            root._icon = tk.PhotoImage(file=str(ICON_PNG))
            root.iconphoto(True, root._icon)
    except tk.TclError:
        pass
    App(root)
    # When started from a launcher, make sure the window comes to the front.
    root.lift()
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
