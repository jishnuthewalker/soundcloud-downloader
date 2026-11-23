#!/usr/bin/env python3
"""
Build the three one-file setup launchers (Mac, Windows, Linux), the readme and
the zip, plus the release files the app and launchers update themselves from.

    python3 build.py            # build into dist/
    python3 build.py --release  # build, then publish dist/release/* as a GitHub release

To ship a new version: edit src/, bump __version__ in src/soundcloud_downloader.py,
commit, then run  python3 build.py --release . Every installed copy picks it up
the next time it's opened. Never edit files in dist/ (they're regenerated).
"""

import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
RELEASE = DIST / "release"
FOLDER = "SoundCloud Downloader"
ZIP_NAME = "SoundCloud-Downloader.zip"

TARGETS = [  # (launcher head, name in the zip, release asset name, line ending, executable)
    ("launcher_mac.command", "SoundCloud Downloader (Mac).command", "setup-mac.command", "\n", True),
    ("launcher_windows.bat", "SoundCloud Downloader (Windows).bat", "setup-windows.bat", "\r\n", False),
    ("launcher_linux.sh", "SoundCloud Downloader (Linux).sh", "setup-linux.sh", "\n", True),
]


def lf(text):
    return text.replace("\r\n", "\n")


def constant(app, name):
    m = re.search(rf'^{name} = "([^"]+)"', app, re.M)
    if not m:
        sys.exit(f"Couldn't find {name} in src/soundcloud_downloader.py")
    return m.group(1)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build():
    app = lf((SRC / "soundcloud_downloader.py").read_text(encoding="utf-8"))
    version, repo = constant(app, "__version__"), constant(app, "REPO")
    python = constant(app, "PYTHON_MINOR")
    for name in ("install.sh", "install.ps1"):      # one-line installers live in the repo, not the release
        if repo not in (ROOT / name).read_text(encoding="utf-8"):
            sys.exit(f"{name} doesn't point at {repo}; update it to match REPO.")
    shutil.rmtree(DIST, ignore_errors=True)
    out_dir = DIST / FOLDER
    out_dir.mkdir(parents=True)
    RELEASE.mkdir(parents=True)

    files = {}                                   # zip entry -> (bytes, executable)
    sums = {}
    app_bytes = app.encode("utf-8")
    (RELEASE / "soundcloud_downloader.py").write_bytes(app_bytes)
    sums["soundcloud_downloader.py"] = sha256(app_bytes)
    for head_name, out_name, asset, eol, executable in TARGETS:
        head = lf((SRC / head_name).read_text(encoding="utf-8"))
        assert head.rstrip("\n").endswith("##PY-START"), f"{head_name} must end with ##PY-START"
        head = head.replace("@VERSION@", version).replace("@REPO@", repo).replace("@PYTHON@", python)
        assert "@" + "VERSION@" not in head and "@" + "REPO@" not in head
        data = (head.rstrip("\n") + "\n" + app).replace("\n", eol).encode("utf-8")
        files[out_name] = (data, executable)
        (RELEASE / asset).write_bytes(data)
        sums[asset] = sha256(data)
    readme = lf((SRC / "READ ME FIRST.txt").read_text(encoding="utf-8"))
    files["READ ME FIRST.txt"] = (readme.replace("\n", "\r\n").encode("utf-8"), False)  # CRLF for Notepad

    stamp = time.localtime()[:6]
    zip_path = RELEASE / ZIP_NAME
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, (data, executable) in files.items():
            path = out_dir / name
            path.write_bytes(data)
            if executable:
                path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            info = zipfile.ZipInfo(f"{FOLDER}/{name}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3                                  # Unix, so permissions are kept
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            z.writestr(info, data)
    shutil.copy2(zip_path, DIST / ZIP_NAME)
    sums[ZIP_NAME] = sha256(zip_path.read_bytes())

    (RELEASE / "latest.json").write_text(json.dumps({"version": version, "files": sums}, indent=2) + "\n",
                                         encoding="utf-8")
    print(f"Built version {version} for {repo}:")
    for p in sorted(DIST.rglob("*")):
        if p.is_file():
            print("  ", p.relative_to(ROOT))
    return version, repo


def sh(*cmd, check=True):
    return subprocess.run(cmd, cwd=ROOT, check=check, capture_output=True, text=True)


def release_notes(repo):
    raw = f"https://raw.githubusercontent.com/{repo}/main"
    return (
        "## Install\n\n"
        "**Mac:** open Terminal (⌘ Space, type *Terminal*, Return), paste this and press Return:\n"
        f"```\ncurl -fsSL {raw}/install.sh | bash\n```\n"
        "**Windows:** press **Win+R**, paste this and press Enter:\n"
        f"```\npowershell -ExecutionPolicy Bypass -c \"irm {raw}/install.ps1 | iex\"\n```\n"
        "**Linux:** paste the Mac line into a terminal.\n\n"
        f"Prefer a download? Get **{ZIP_NAME}** below and follow *READ ME FIRST.txt* inside it.\n\n"
        "**Already installed?** Nothing to do: the app updates itself the next time it's opened.\n\n"
        "_The other files below (`setup-*`, `soundcloud_downloader.py`, `latest.json`) are used by automatic "
        "updates. Don't download them directly: browsers remove their permission to run._"
    )


def release(version, repo):
    tag = f"v{version}"
    if sh("git", "status", "--porcelain").stdout.strip():
        sys.exit("Commit your changes first (git status isn't clean).")
    if sh("gh", "release", "view", tag, "--repo", repo, check=False).returncode == 0:
        sys.exit(f"{tag} is already released. Bump __version__ in src/soundcloud_downloader.py first.")
    sh("git", "push", "origin", "HEAD")
    assets = [str(p) for p in sorted(RELEASE.iterdir())]
    sh("gh", "release", "create", tag, *assets, "--repo", repo, "--target", sh("git", "rev-parse", "HEAD").stdout.strip(),
       "--title", f"SoundCloud Downloader {version}",
       "--notes", release_notes(repo))
    print(f"Published {tag}: https://github.com/{repo}/releases/tag/{tag}")


if __name__ == "__main__":
    v, r = build()
    if "--release" in sys.argv:
        release(v, r)
