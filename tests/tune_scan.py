"""Score a diagnostic scan (from scan_embed.py) with candidate rules, offline and fast.

Prints, per signal, how many minutes of the movie it flags inside and outside reference windows
(known real scenes), and a minute-by-minute strip so stray flags are easy to spot.

Usage (inside the container): python3 tune_scan.py SCAN.npz --ref 1340-1510,4250-4355
"""
import argparse
import json
import sys

import numpy as np

sys.path.insert(0, "/app")
from cleancut import scenes  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("scan")
p.add_argument("--ref", default="", help="real scenes, seconds: a-b,c-d")
p.add_argument("--show", default="", help="signals to print per minute")
a = p.parse_args()

z = np.load(a.scan)
t, emb, dur = z["t"], z["emb"].astype(np.float32), float(z["duration"])
dets = json.loads(str(z["dets"]))
valid = np.linalg.norm(emb, axis=2) > 0
emb = emb / np.maximum(np.linalg.norm(emb, axis=2, keepdims=True), 1e-6)
ref = [tuple(map(float, r.split("-"))) for r in a.ref.split(",") if r]
in_ref = np.array([any(x <= tt <= y for x, y in ref) for tt in t])
dt = np.median(np.diff(t))

signals = scenes.frame_signals(emb, valid, dets)

def minutes(mask):
    return mask.sum() * dt / 60

print(f"{len(t)} samples, {dur / 60:.0f} min, reference {minutes(in_ref):.1f} min")
for name, s in signals.items():
    for thr in (0.5, 0.7, 0.9):
        m = s >= thr
        print(f"{name:14s} >= {thr}: inside {minutes(m & in_ref):5.1f} min of {minutes(in_ref):.1f}   outside {minutes(m & ~in_ref):5.1f} min")

for name in [n for n in a.show.split(",") if n]:
    s = signals[name]
    print(f"\n{name}: share of samples >= 0.7 per minute ('#' >= 50%, '+' >= 20%, '.' > 0)")
    line = ""
    for m0 in range(int(dur // 60) + 1):
        sel = (t >= m0 * 60) & (t < m0 * 60 + 60)
        f = (s[sel] >= 0.7).mean() if sel.any() else 0
        line += "#" if f >= 0.5 else "+" if f >= 0.2 else "." if f > 0 else " "
        if m0 % 60 == 59:
            print(f"{m0 // 60}h |{line}|")
            line = ""
    print(f"{int(dur // 3600)}h |{line}|")

# ---- scene rules: per camera shot, share of strongly flagged frames; join nearby shots.
cuts = list(z["cuts"])
bounds = [0.0] + [c for c in cuts if 0 < c < dur] + [dur]

def fmt(s):
    return f"{int(s // 3600)}:{int(s // 60) % 60:02d}:{int(s % 60):02d}"

def scenes_for(sig, thr, share, min_frames, gap):
    s = signals[sig]
    flagged = []
    for x, y in zip(bounds, bounds[1:]):
        sel = (t >= x) & (t < y)
        n = int(sel.sum())
        if not n:
            continue
        k = int((s[sel] >= thr).sum())
        if k >= 1 and k / n >= share:
            flagged.append([x, y, k])
    out = []
    for x, y, k in flagged:
        if out and x - out[-1][1] <= gap:
            out[-1][1], out[-1][2] = y, out[-1][2] + k
        else:
            out.append([x, y, k])
    return [o for o in out if o[2] >= min_frames]

for sig in ("nsfw_sex", "nsfw_sugg"):
    for thr, share, mf in ((0.7, 0.3, 3), (0.7, 0.3, 5), (0.8, 0.4, 4), (0.6, 0.25, 4)):
        sc = scenes_for(sig, thr, share, mf, 10.0)
        inside = sum(min(y, b) - max(x, a) for x, y, _ in sc for a, b in ref if min(y, b) > max(x, a))
        total = sum(y - x for x, y, _ in sc)
        print(f"\n{sig} thr {thr} share {share} min_frames {mf}: {len(sc)} scenes, {total / 60:.1f} min, "
              f"of which in reference {inside / 60:.1f} min")
        for x, y, k in sc:
            tag = "REF" if any(min(y, b) > max(x, a) for a, b in ref) else "   "
            print(f"   {tag} {fmt(x)}-{fmt(y)} ({y - x:5.1f}s, {k} strong frames)")
