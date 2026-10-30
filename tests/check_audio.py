"""Compare loudness inside each censored word: original vs muted vs bleeped.

Usage (inside the container): python3 check_audio.py PLAN SRC MUTED BLEEPED
"""
import json
import re
import subprocess
import sys


def peak(path, a, b):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", path,
                          "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(re.search(r"max_volume: ([-0-9.]+)", err).group(1))


def pauses(path):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vn", "-af", "silencedetect=n=-45dB:d=0.15",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return [round(float(x), 3) for x in re.findall(r"silence_start: ([0-9.]+)", err) if float(x) > 0][:4]


plan_path, src, muted, bleeped = sys.argv[1:5]
plan = json.load(open(plan_path))
print("pause start times  original", pauses(src))
print("                   muted   ", pauses(muted))
print("                   bleeped ", pauses(bleeped))
print("peak dB inside each word:   original | muted | bleeped")
for e in plan["events"]:
    if e["type"] != "language":
        continue
    a, b = e["start"] + 0.06, e["end"] - 0.09
    word = e["word"]
    print(f"  {word:>10} {e['start']:6.2f}s  {peak(src, a, b):6.1f} | {peak(muted, a, b):6.1f} | {peak(bleeped, a, b):6.1f}")
print(f"clean speech 0.2-0.8s:      {peak(src, .2, .8):6.1f} | {peak(muted, .2, .8):6.1f} | {peak(bleeped, .2, .8):6.1f}")
