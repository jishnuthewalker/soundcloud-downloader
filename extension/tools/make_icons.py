#!/usr/bin/env python3
"""Draw the extension icons with the desktop app's icon renderer (same artwork everywhere)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "src"))
import soundcloud_downloader as app  # noqa: E402  (importing doesn't touch the user's app folder)

out = HERE.parent / "src" / "icons"
out.mkdir(parents=True, exist_ok=True)
for base, wanted in ((512, {128, 32, 16}), (384, {48})):
    px, n = app._render_icon(base), base
    while True:
        if n in wanted:
            (out / f"icon-{n}.png").write_bytes(app._png(px, n))
        if n <= min(wanted):
            break
        px, n = app._half(px, n), n // 2
print("wrote", sorted(p.name for p in out.iterdir()))
