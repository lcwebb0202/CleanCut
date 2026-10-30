"""Diagnostic scan: per sampled frame, keep CLIP embeddings (one per crop) and every NudeNet
detection, as numbers only (no pictures), so detection rules can be tuned offline.

Usage (inside the container): python3 scan_embed.py MOVIE OUT.npz [--fps 2]
"""
import argparse
import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, "/app")
from cleancut.media import probe  # noqa: E402
from cleancut.nudity import HDR_TRANSFERS, _load_detector, read_cuts, sample_frames  # noqa: E402
from cleancut.scenes import SceneDetector  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("movie")
p.add_argument("out")
p.add_argument("--fps", type=float, default=2.0)
a = p.parse_args()

info = probe(a.movie)
det = _load_detector(print)
clip = SceneDetector(print)
fd, cuts_file = tempfile.mkstemp(suffix=".txt")
os.close(fd)
times, embs, dets = [], [], []
hdr = info.color.get("color_transfer") in HDR_TRANSFERS
for i, (t, frame) in enumerate(sample_frames(a.movie, info.width, info.height, a.fps, hdr, cuts_file)):
    out = clip.vision.run(None, {clip.input: clip._crops(frame)})
    names = [o.name for o in clip.vision.get_outputs()]
    e = dict(zip(names, out)).get("image_embeds", out[0])
    embs.append(np.pad(e, ((0, 3 - len(e)), (0, 0))).astype(np.float16))
    fh, fw = frame.shape[:2]
    dets.append([[d["class"], round(float(d["score"]), 3), *[round(v, 4) for v in (
        d["box"][0] / fw, d["box"][1] / fh, (d["box"][0] + d["box"][2]) / fw, (d["box"][1] + d["box"][3]) / fh)]]
        for d in det.detect(frame) if d["score"] >= 0.2])
    times.append(t + info.video_offset)
    if i % 600 == 0:
        print(f"{t / 60:.0f} min", flush=True)
cuts = read_cuts(cuts_file)
np.savez_compressed(a.out, t=np.array(times), emb=np.stack(embs), cuts=np.array(cuts),
                    dets=np.array(json.dumps(dets)), duration=info.duration)
print("saved", a.out, len(times), "samples,", len(cuts), "cuts")
