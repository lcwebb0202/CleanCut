"""Write the filtered movie: cover video regions and silence/bleep words."""
import json
import os
import shutil
import subprocess
import tempfile

import cv2
import numpy as np

from . import subtitles
from .media import MediaInfo, encoder_works, probe, run

# AC3 (E-AC3 above 5.1) rather than AAC: ffmpeg writes AAC into MKV ~21 ms late, AC3 only ~5 ms.
AUDIO_BITRATE = {1: "192k", 2: "256k", 6: "640k", 8: "1024k"}
BLEEP_HZ = 1000
# ffmpeg's sine is 1/8 amplitude, so 0.8 peaks around -20 dB. It was 2.0 (-12 dB) and played on
# every speaker of a 5.1 track; the user found that too in-your-face. Now it's quieter and only
# comes from where the voices are (the center speaker, or both speakers in stereo).
BLEEP_VOLUME = 0.8
BLUR_CELLS = 3  # a blurred region is pixelated to about this many blocks across


def _video_codec_args(info: MediaInfo) -> list[str]:
    if encoder_works("hevc_nvenc"):
        args = ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"]
        if info.ten_bit:
            args += ["-profile:v", "main10", "-pix_fmt", "p010le"]
    elif info.ten_bit:
        args = ["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p10le",
                "-x265-params", "log-level=error"]
    else:
        args = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    return args + _color_args(info)


def _color_args(info: MediaInfo) -> list[str]:
    """Color tags (HDR etc). Needed on both the raw input and the output in ffmpeg 7."""
    flags = {"color_primaries": "-color_primaries", "color_transfer": "-color_trc",
             "color_space": "-colorspace", "color_range": "-color_range"}
    args = []
    for key, value in info.color.items():
        args += [flags[key], value]
    return args


def _between(ranges) -> str:
    """ffmpeg expression that is 1 inside any of the ranges. Overlaps are merged, and the sum is
    built as a balanced tree: ffmpeg can't read a flat sum of 100 or more terms (a film with 152
    muted words failed to save), but a balanced one works with thousands."""
    merged: list[list[float]] = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    def tree(rs):
        if len(rs) == 1:
            return f"between(t,{rs[0][0]:.3f},{rs[0][1]:.3f})"
        m = len(rs) // 2
        return f"({tree(rs[:m])}+{tree(rs[m:])})"
    return tree(merged) if merged else "0"


def _bleep_pan(channels: int, layout: str) -> str:
    """Route the mono bleep tone to the speakers that carry the dialogue."""
    if channels == 1:
        return "aformat=channel_layouts=mono"
    if channels >= 3 and not layout.startswith(("stereo", "2.", "quad")):  # layouts with a center
        return f"pan={layout}|FC=c0"  # surround: the center speaker only
    return f"pan={layout}|FL=c0|FR=c0"


def _audio_filter(info: MediaInfo, ranges: list[list[float]], mode: str, skips: list) -> str:
    """Filtergraph (input 1 = original file) that silences or bleeps the given ranges,
    then drops the skipped scenes."""
    src = f"[1:a:{info.audio_index}]"
    if not ranges:
        graph = f"{src}anull[clean]"
    elif mode == "mute":
        graph = f"{src}volume=volume='1-min(1,{_between(ranges)})':eval=frame[clean]"
    else:
        hit = f"min(1,{_between(ranges)})"
        layout = info.audio_layout if info.audio_layout != "unknown" else "stereo"
        # amix halves both inputs, so the result is doubled back up.
        graph = (f"{src}volume=volume='1-{hit}':eval=frame[speech];"
                 f"sine=frequency={BLEEP_HZ}:sample_rate={info.audio_rate},"
                 f"volume=volume='{BLEEP_VOLUME}*{hit}':eval=frame,"
                 f"{_bleep_pan(info.audio_channels, layout)}[bleep];"
                 f"[speech][bleep]amix=inputs=2:duration=first,volume=2[clean]")
    if skips:
        # Keep the stream's original start offset so it stays in sync with the video.
        graph += (f";[clean]aselect='not({_between(skips)})',"
                  f"asetpts='N/SR/TB+{info.audio_offset:.3f}/TB'[aout]")
    else:
        graph = graph.replace("[clean]", "[aout]")
    return graph


def _chapters_file(path: str, timeline, out: str) -> bool:
    """Write the chapters, moved to match cut scenes, as an ffmetadata file. False if none."""
    chapters = json.loads(run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", path]).stdout)["chapters"]
    if not chapters:
        return False
    lines = [";FFMETADATA1"]
    seen = set()
    for c in chapters:
        a = round(timeline.map(float(c["start_time"])) * 1000)
        b = round(timeline.map(float(c["end_time"])) * 1000)
        if b <= a or a in seen:
            continue  # chapter was inside a cut scene
        seen.add(a)
        title = c.get("tags", {}).get("title", "").replace("=", "\\=").replace("\n", " ")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={a}", f"END={b}", f"title={title}"]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return True


class Frame:
    """Writable Y/U/V views over one raw yuv420p(10le) frame."""

    def __init__(self, buf: bytearray, w: int, h: int, ten_bit: bool):
        arr = np.frombuffer(buf, np.uint16 if ten_bit else np.uint8)
        ys, cs = w * h, (w // 2) * (h // 2)
        self.w, self.h = w, h
        self.y = arr[:ys].reshape(h, w)
        self.u = arr[ys:ys + cs].reshape(h // 2, w // 2)
        self.v = arr[ys + cs:ys + 2 * cs].reshape(h // 2, w // 2)
        self.black = 64 if ten_bit else 16
        self.neutral = 512 if ten_bit else 128

    def blackout(self):
        self.y[:] = self.black
        self.u[:] = self.neutral
        self.v[:] = self.neutral

    def cover(self, box, style: str):
        x0, y0 = int(box[0] * self.w) // 2 * 2, int(box[1] * self.h) // 2 * 2
        x1, y1 = -(-int(np.ceil(box[2] * self.w)) // 2) * 2, -(-int(np.ceil(box[3] * self.h)) // 2) * 2
        x1, y1 = min(x1, self.w), min(y1, self.h)
        if x1 <= x0 or y1 <= y0:
            return
        planes = ((self.y, x0, y0, x1, y1, self.black),
                  (self.u, x0 // 2, y0 // 2, x1 // 2, y1 // 2, self.neutral),
                  (self.v, x0 // 2, y0 // 2, x1 // 2, y1 // 2, self.neutral))
        for plane, a, b, c, d, fill in planes:
            if style == "blur":
                region = plane[b:d, a:c]
                rh, rw = region.shape
                cell = max(2, max(rw, rh) // BLUR_CELLS)
                small = cv2.resize(region, (max(1, rw // cell), max(1, rh // cell)), interpolation=cv2.INTER_AREA)
                big = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_LINEAR)
                k = (2 * cell) | 1
                plane[b:d, a:c] = cv2.GaussianBlur(big, (k, k), 0)
            else:
                plane[b:d, a:c] = fill


def _read_exact(stream, mv: memoryview) -> bool:
    pos = 0
    while pos < len(mv):
        n = stream.readinto(mv[pos:])
        if not n:
            return False
        pos += n
    return True


def render(plan: dict, out_path: str, log=print) -> None:
    info = probe(plan["input"])
    settings = plan["settings"]
    style = settings.get("cover", "box")
    fps = float(info.fps)
    W, H = info.width, info.height

    # Flatten every moment into covers: (start, end, box), where box None = whole frame,
    # and collect the scenes to cut out.
    covers, skips = [], []
    for ev in plan["events"]:
        if ev["type"] != "visual" or ev["action"] == "ignore":
            continue
        if ev["action"] == "skip":
            skips.append((ev["start"], ev["end"]))
        elif ev["action"] == "blackout":
            covers.append((ev["start"], ev["end"], None))
        else:  # "auto" or "box"
            covers += [(c["start"], c["end"], c["box"]) for c in ev["covers"]]
    covers.sort(key=lambda c: c[0])
    skips = _merge_ranges(skips)
    timeline = subtitles.Timeline(skips)

    word_ranges = [[e["start"], e["end"]] for e in plan["events"]
                   if e["type"] == "language" and e["action"] == "censor"]
    lang_filters = set(settings["filters"]) & set(subtitles.language.CATEGORIES)

    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".cleancut-", dir=out_dir)
    decode = ["ffmpeg", "-v", "error", "-i", info.path, "-map", "0:v:0", "-vf", f"fps={info.fps}",
              "-pix_fmt", info.raw_pix_fmt, "-f", "rawvideo", "-"]
    encode = ["ffmpeg", "-y", "-v", "error",
              "-f", "rawvideo", "-pix_fmt", info.raw_pix_fmt, "-s", f"{W}x{H}", "-framerate", str(info.fps),
              *_color_args(info), "-itsoffset", f"{info.video_offset:.3f}", "-i", "-", "-i", info.path]
    maps = ["-map", "0:v"]
    if info.audio_index is not None:
        if word_ranges or skips:
            script = os.path.join(tmp, "audio.txt")
            with open(script, "w") as f:
                f.write(_audio_filter(info, word_ranges, settings.get("audio", "mute"), skips))
            maps += ["-/filter_complex", script, "-map", "[aout]",
                     "-c:a", "ac3" if info.audio_channels <= 6 else "eac3",
                     "-b:a", AUDIO_BITRATE.get(info.audio_channels, "640k")]
        else:
            maps += ["-map", f"1:a:{info.audio_index}", "-c:a", "copy"]

    # Subtitles: text ones are cleaned and re-timed; image ones are copied only if untouched.
    sub_args, n_out, next_input = [], 0, 2
    for st in subtitles.streams(info.path):
        name = st["tags"].get("title") or st["tags"].get("language") or f"#{st['index']}"
        if st["text"] and (lang_filters or skips):
            path, n = subtitles.clean_stream(info.path, st, tmp, lang_filters, timeline)
            encode += ["-i", path]
            maps += ["-map", f"{next_input}:0"]
            next_input += 1
            log(f"  subtitles {name}: {n} words masked")
        elif st["text"] or not (lang_filters or skips):
            maps += ["-map", f"1:s:{st['index']}"]
        else:
            log(f"  subtitles {name}: dropped (image subtitles can't be cleaned)")
            continue
        for key, value in st["tags"].items():
            if key.lower() in ("language", "title"):
                sub_args += [f"-metadata:s:s:{n_out}", f"{key.lower()}={value}"]
        flags = [k for k in ("default", "forced", "hearing_impaired") if st["disposition"].get(k)]
        sub_args += [f"-disposition:s:{n_out}", "+".join(flags) or "0"]
        n_out += 1
    if n_out:
        maps += ["-c:s", "copy"]  # cleaned files are already SRT/ASS

    chapters = ["-map_chapters", "1"]
    if skips:
        meta = os.path.join(tmp, "chapters.txt")
        chapters = ["-map_chapters", str(next_input)] if _chapters_file(info.path, timeline, meta) else []
        if chapters:
            encode += ["-i", meta]
    encode += [*maps, *sub_args, *chapters, "-map_metadata", "1", *_video_codec_args(info), out_path]
    cut = sum(b - a for a, b in skips)
    log(f"Rendering {W}x{H} @ {fps:.3f} fps with {encode[encode.index('-c:v') + 1]}, "
        f"{len(covers)} covers, {len(skips)} scenes cut ({cut:.1f} s), {len(word_ranges)} words")

    dec = subprocess.Popen(decode, stdout=subprocess.PIPE)
    enc = subprocess.Popen(encode, stdin=subprocess.PIPE)
    frame_bytes = (W * H + 2 * (W // 2) * (H // 2)) * (2 if info.ten_bit else 1)
    buf = bytearray(frame_bytes)
    mv = memoryview(buf)
    total = max(1, int(info.duration * fps))
    n, ci, si, last_pct = 0, 0, 0, -2
    active: list = []
    try:
        while _read_exact(dec.stdout, mv):
            t = info.video_offset + n / fps
            n += 1
            while si < len(skips) and skips[si][1] <= t:
                si += 1
            if si < len(skips) and skips[si][0] <= t:
                continue  # inside a cut scene: drop the frame
            while ci < len(covers) and covers[ci][0] <= t:
                active.append(covers[ci])
                ci += 1
            active = [c for c in active if c[1] >= t]
            if active:
                frame = Frame(buf, W, H, info.ten_bit)
                if any(c[2] is None for c in active):
                    frame.blackout()
                else:
                    for c in active:
                        frame.cover(c[2], style)
            enc.stdin.write(buf)
            pct = n / total * 100
            if pct - last_pct >= 2:
                log(f"  rendered {min(pct, 100):5.1f}%")
                last_pct = pct
    finally:
        dec.stdout.close()
        dec.wait()
        enc.stdin.close()
        enc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    if enc.returncode != 0:
        raise RuntimeError(f"ffmpeg encoder failed with exit code {enc.returncode}")
    log(f"Done: {out_path}")


def _merge_ranges(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
