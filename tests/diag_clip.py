"""Diagnose detection on a clip: one sample per second, every NudeNet class above a low threshold,
and a contact sheet of HEAVILY blurred thumbnails with the boxes outlined (safe to look at).

Usage (inside the container): python3 diag_clip.py CLIP OUT_DIR [--every SECONDS]
"""
import argparse
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, "/app")
from cleancut.media import probe  # noqa: E402
from cleancut.nudity import CLASS_GROUPS, HDR_TRANSFERS, MIN_SCORE, _load_detector, sample_frames  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("clip")
p.add_argument("out")
p.add_argument("--every", type=float, default=1.0)
p.add_argument("--stretch", action="store_true", help="auto-brighten each frame before detection")
p.add_argument("--plan", help="also outline this plan's covers (white = box, full white border = blackout)")
p.add_argument("--start", type=float, default=0.0, help="only from this time (seconds)")
p.add_argument("--end", type=float, default=1e9)
a = p.parse_args()
import json  # noqa: E402
covers = []
if a.plan:
    for ev in json.load(open(a.plan))["events"]:
        if ev["type"] == "visual" and ev["action"] != "ignore":
            covers += ev["covers"]


def stretch(frame):
    """Stretch brightness so the 1st-99th percentile fills the range."""
    lo, hi = np.percentile(frame, (1, 99))
    return np.clip((frame.astype(np.float32) - lo) * (255.0 / max(hi - lo, 1)), 0, 255).astype(np.uint8)
os.makedirs(a.out, exist_ok=True)

info = probe(a.clip)
det = _load_detector(print)
tiles = []
hdr = info.color.get("color_transfer") in HDR_TRANSFERS
for t, frame in sample_frames(a.clip, info.width, info.height, 1.0 / a.every, hdr):
    t += info.video_offset
    if not a.start <= t <= a.end:
        continue
    h, w = frame.shape[:2]
    if a.stretch:
        frame = stretch(frame)
    found = [d for d in det.detect(frame) if d["score"] >= 0.25 and not d["class"].startswith(("FACE", "FEET", "ARMPITS", "BELLY"))]
    line = ", ".join(f"{d['class'].replace('_EXPOSED', '+').replace('_COVERED', '-')} {d['score']:.2f}" for d in found)
    print(f"{t:6.1f}s  mean-brightness {frame.mean():5.1f}  {line}")
    # Heavy blur: shrink to 24 px wide, then blow back up.
    tiny = cv2.resize(frame, (24, max(1, 24 * h // w)), interpolation=cv2.INTER_AREA)
    thumb = cv2.resize(tiny, (320, 320 * h // w), interpolation=cv2.INTER_LINEAR)
    for d in found:
        x, y, bw, bh = d["box"]
        group = CLASS_GROUPS.get(d["class"])
        counted = group in MIN_SCORE and d["score"] >= MIN_SCORE[group]
        color = (0, 0, 255) if counted else (0, 200, 255)  # red = would be covered, orange = below threshold
        s = 320 / w
        cv2.rectangle(thumb, (int(x * s), int(y * s)), (int((x + bw) * s), int((y + bh) * s)), color, 2)
    th, tw = thumb.shape[:2]
    for c in covers:
        if c["start"] <= t <= c["end"]:
            if c["box"] is None:
                cv2.rectangle(thumb, (0, 0), (tw - 1, th - 1), (255, 255, 255), 6)
            else:
                x0, y0, x1, y1 = c["box"]
                cv2.rectangle(thumb, (int(x0 * tw), int(y0 * th)), (int(x1 * tw), int(y1 * th)), (255, 255, 255), 2)
    cv2.putText(thumb, f"{t:.2f}s", (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    tiles.append(thumb)

cols = 8
while len(tiles) % cols:
    tiles.append(np.zeros_like(tiles[0]))
rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
cv2.imwrite(os.path.join(a.out, "contact.jpg"), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 80])
print("wrote", os.path.join(a.out, "contact.jpg"))
