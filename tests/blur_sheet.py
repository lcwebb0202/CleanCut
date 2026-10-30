"""HEAVILY blurred contact sheet of time ranges in a movie (safe to look at: frames are shrunk to
24 px wide by ffmpeg before they ever reach this script, then blown up).

Usage (inside the container): python3 blur_sheet.py MOVIE OUT.jpg 1325-1340 1517-1610 ... [--every 1.5]
"""
import argparse
import subprocess
import sys

import cv2
import numpy as np

sys.path.insert(0, "/app")
from cleancut.media import probe  # noqa: E402
from cleancut.nudity import HDR_TRANSFERS, TONEMAP  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("movie")
p.add_argument("out")
p.add_argument("ranges", nargs="+")
p.add_argument("--every", type=float, default=1.5)
a = p.parse_args()

info = probe(a.movie)
tw, th = 24, max(2, round(24 * info.height / info.width))
hdr = TONEMAP if info.color.get("color_transfer") in HDR_TRANSFERS else ""
tiles = []
for r in a.ranges:
    x, y = map(float, r.split("-"))
    vf = f"fps=1/{a.every},{hdr}scale={tw}:{th}:flags=area,format=bgr24"
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(x), "-i", a.movie, "-t", str(y - x), "-map", "0:v:0",
                          "-vf", vf, "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(raw) // (tw * th * 3)
    for i in range(n):
        tiny = np.frombuffer(raw[i * tw * th * 3:(i + 1) * tw * th * 3], np.uint8).reshape(th, tw, 3)
        lo, hi = np.percentile(tiny, (1, 99.5))
        tiny = np.clip((tiny.astype(np.float32) - lo) * min(4.0, 235 / max(hi - lo, 1)) + 10, 0, 255).astype(np.uint8)
        big = cv2.resize(tiny, (240, 240 * th // tw), interpolation=cv2.INTER_LINEAR)
        t = x + i * a.every
        cv2.putText(big, f"{int(t // 3600)}:{int(t // 60) % 60:02d}:{int(t % 60):02d}", (4, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        tiles.append(big)
    tiles.append(np.full_like(tiles[-1], 40))  # gap between ranges
cols = 10
while len(tiles) % cols:
    tiles.append(np.zeros_like(tiles[0]))
cv2.imwrite(a.out, np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]),
            [cv2.IMWRITE_JPEG_QUALITY, 80])
print("wrote", a.out, len(tiles), "tiles")
