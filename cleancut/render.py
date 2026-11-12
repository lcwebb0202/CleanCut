"""Write the filtered movie: cover video regions and silence/bleep words."""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading

import cv2
import numpy as np

from . import subtitles
from .media import MediaInfo, encoder_works, has_center, probe, run

# AC3 (E-AC3 above 5.1) rather than AAC: ffmpeg writes AAC into MKV ~21 ms late, AC3 only ~5 ms.
AUDIO_BITRATE = {1: "192k", 2: "256k", 6: "640k", 8: "1024k"}
BLEEP_HZ = 1000
# ffmpeg's sine is 1/8 amplitude, so 0.8 peaks around -20 dB. It was 2.0 (-12 dB) and played on
# every speaker of a 5.1 track; the user found that too in-your-face. Now it's quieter and only
# comes from where the voices are (the center speaker, or both speakers in stereo).
BLEEP_VOLUME = 0.8
BLUR_CELLS = 3  # a blurred region is pixelated to about this many blocks across

# Surround tracks mute a word on the center speaker only, unless the voice is on other speakers
# too. Checked per word, in the speech band: other speakers count as carrying the voice when
# they contain a copy of the center (Deadpool's to-camera lines: 0.9+), are nearly as loud as it,
# or their loudness rises and falls with the center's syllables (room echo: Jordan's speeches in
# The Wolf of Wall Street, where Whisper could still make out the swear from the side speakers
# alone; all 31 such words in Wolf and Deadpool scored >= 0.30, the lowest real one 0.37).
VOICE_RATE = 16000
VOICE_COPY = 0.3        # correlation with the center at or above this = the voice is there too
VOICE_OTHERS_DB = -10   # other speakers within this many dB of the center = can't tell, mute all
VOICE_ECHO = 0.25       # loudness following the center's syllables this closely = echo, mute all
VOICE_MARGIN = 0.3      # seconds around the word used for the echo check


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


def _bleep_pan(info: MediaInfo, layout: str) -> str:
    """Route the mono bleep tone to the speakers that carry the dialogue."""
    if info.audio_channels == 1:
        return "aformat=channel_layouts=mono"
    if has_center(info):
        return f"pan={layout}|FC=c0"  # surround: the center speaker only
    return f"pan={layout}|FL=c0|FR=c0"


def _bandpass(x: np.ndarray) -> np.ndarray:
    """Keep 300-3400 Hz, where speech is (drops bass, rumble and hiss)."""
    spec = np.fft.rfft(x, axis=0)
    freq = np.fft.rfftfreq(len(x), 1 / VOICE_RATE)
    spec[(freq < 300) | (freq > 3400)] = 0
    return np.fft.irfft(spec, len(x), axis=0)


def _loudness(x: np.ndarray) -> np.ndarray:
    """Log loudness in 20 ms windows every 10 ms."""
    hop, win = VOICE_RATE // 100, VOICE_RATE // 50
    c = np.cumsum(np.concatenate([[0.0], x * x]))
    i = np.arange(0, len(x) - win, hop)
    return np.log(np.sqrt((c[i + win] - c[i]) / win) + 1e-9)


def _echo(center: np.ndarray, others: np.ndarray) -> float:
    """How closely the other speakers' loudness follows the center's, allowing them to lag up to
    200 ms (room echo). Music doesn't follow someone's syllables; an echo of the voice does."""
    ec, eo = _loudness(center), _loudness(others.sum(axis=1))
    best = -1.0
    for lag in range(21):
        u, v = ec[:len(ec) - lag], eo[lag:]
        if len(u) > 10 and u.std() > 0 and v.std() > 0:
            best = max(best, float(np.corrcoef(u, v)[0, 1]))
    return best


def _voice_on_others(seg: np.ndarray, lo: int, hi: int) -> bool:
    """seg: one word with some time either side, all channels (center is channel 2 in every
    layout that has one); the word itself is seg[lo:hi]."""
    if hi - lo < VOICE_RATE // 20:  # too short to judge
        return True
    bp_all = _bandpass(seg.astype(np.float64))
    if _echo(bp_all[:, 2], np.delete(bp_all, 2, axis=1)) >= VOICE_ECHO:
        return True
    bp = bp_all[lo:hi]
    center, others = bp[:, 2], np.delete(bp, 2, axis=1)
    c_norm = np.linalg.norm(center)
    if c_norm < 1e-6:
        return True  # nothing on the center: the word must be somewhere else
    if 20 * np.log10(np.linalg.norm(others.sum(axis=1)) / c_norm + 1e-12) >= VOICE_OTHERS_DB:
        return True
    n, lag = len(center), int(0.02 * VOICE_RATE)  # copies can be a few ms off
    c_spec = np.fft.rfft(center, 2 * n)
    for k in range(others.shape[1]):
        o_norm = np.linalg.norm(others[:, k])
        if o_norm < 1e-9:
            continue
        xc = np.fft.irfft(c_spec * np.conj(np.fft.rfft(others[:, k], 2 * n)))
        if max(np.abs(xc[:lag + 1]).max(), np.abs(xc[-lag:]).max()) / (c_norm * o_norm) >= VOICE_COPY:
            return True
    return False


def _voice_elsewhere(info: MediaInfo, ranges: list[list[float]]) -> list[bool]:
    """For each word: True if its voice is (or may be) on speakers other than the center, so it
    has to be muted everywhere. One pass over the audio; any trouble = True for the rest (safe)."""
    flags = [True] * len(ranges)
    ch = info.audio_channels
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", info.path, "-map", f"0:a:{info.audio_index}",
                             "-ar", str(VOICE_RATE), "-f", "f32le", "-"], stdout=subprocess.PIPE)
    buf, pos, done = np.zeros((0, ch), np.float32), 0, False  # pos = sample number of buf[0]
    margin = int(VOICE_MARGIN * VOICE_RATE)
    try:
        for k in sorted(range(len(ranges)), key=lambda k: ranges[k][0]):
            a, b = (int((t - info.audio_offset) * VOICE_RATE) for t in ranges[k])
            i, j = max(0, a - margin), b + margin
            while pos + len(buf) < j and not done:
                raw = proc.stdout.read(VOICE_RATE * ch * 4 * 10)
                done = not raw
                usable = len(raw) // (ch * 4) * ch * 4
                buf = np.concatenate([buf, np.frombuffer(raw[:usable], np.float32).reshape(-1, ch)])
            seg = buf[max(0, i - pos):j - pos]
            flags[k] = _voice_on_others(seg, max(0, a) - i, min(len(seg), b - i))
            if i > pos:  # ranges come in start order, so nothing before this one is needed again
                buf, pos = buf[i - pos:], i
    finally:
        proc.kill()
        proc.wait()
    return flags


def _audio_filter(info: MediaInfo, center_only: list, everywhere: list, mode: str, skips: list) -> str:
    """Filtergraph (input 1 = original file) that silences or bleeps the words, then drops the
    skipped scenes. center_only words are silenced on the center speaker only (surround tracks,
    where it carries the dialogue), so music and effects keep playing through them; the others
    on every speaker. The center is silenced by adding an inverted copy of it during the words,
    which leaves every other sample exactly as it was."""
    src = f"[1:a:{info.audio_index}]"
    layout = info.audio_layout if info.audio_layout != "unknown" else "stereo"
    ranges = center_only + everywhere
    if not ranges:
        graph = f"{src}anull[clean]"
    elif center_only:
        hit = f"min(1,{_between(ranges)})"
        quiet = f"volume=volume='1-min(1,{_between(everywhere)})':eval=frame," if everywhere else ""
        parts = (f"{src}{quiet}asplit=2[orig][c];"
                 f"[c]pan={layout}|FC=-1*FC,volume=volume='min(1,{_between(center_only)})':eval=frame[cut];")
        if mode == "mute":
            graph = parts + "[orig][cut]amix=inputs=2:duration=first:normalize=0[clean]"
        else:
            graph = parts + (f"sine=frequency={BLEEP_HZ}:sample_rate={info.audio_rate},"
                             f"volume=volume='{BLEEP_VOLUME}*{hit}':eval=frame,{_bleep_pan(info, layout)}[bleep];"
                             f"[orig][cut][bleep]amix=inputs=3:duration=first:normalize=0[clean]")
    elif mode == "mute":
        graph = f"{src}volume=volume='1-min(1,{_between(ranges)})':eval=frame[clean]"
    else:
        hit = f"min(1,{_between(ranges)})"
        # amix halves both inputs, so the result is doubled back up.
        graph = (f"{src}volume=volume='1-{hit}':eval=frame[speech];"
                 f"sine=frequency={BLEEP_HZ}:sample_rate={info.audio_rate},"
                 f"volume=volume='{BLEEP_VOLUME}*{hit}':eval=frame,"
                 f"{_bleep_pan(info, layout)}[bleep];"
                 f"[speech][bleep]amix=inputs=2:duration=first,volume=2[clean]")
    if skips:
        # Keep the stream's original start offset so it stays in sync with the video. The original
        # audio, kept as a second track, loses the same scenes (and nothing else).
        cut = f"aselect='not({_between(skips)})',asetpts='N/SR/TB+{info.audio_offset:.3f}/TB'"
        graph += f";[clean]{cut}[aout];{src}{cut}[aorig]"
    else:
        graph = graph.replace("[clean]", "[aout]")
    return graph


LANGUAGES = {"eng": "English", "spa": "Spanish", "fre": "French", "fra": "French", "ger": "German",
             "deu": "German", "ita": "Italian", "por": "Portuguese", "jpn": "Japanese", "kor": "Korean",
             "chi": "Chinese", "zho": "Chinese", "rus": "Russian", "dut": "Dutch", "nld": "Dutch"}


def _audio_titles(info: MediaInfo) -> tuple[str, str]:
    """Track names for the filtered and the original audio: "English - Filtered", "English - Original"."""
    lang = LANGUAGES.get(info.audio_language.lower(), "")
    return (f"{lang} - Filtered", f"{lang} - Original") if lang else ("Filtered", "Original")


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


# Frames pass from the decoding ffmpeg through Python to the encoding one. Read one at a time with
# the default 64 KB pipes, that took twice as long as ffmpeg alone (a 4K minute: 66 s vs 32 s).
# Bigger pipes plus reading the next frames while this one is written: 36 s, same picture.
PIPE_SIZE = 1 << 20   # Linux's usual limit for an ordinary user
READ_AHEAD = 4


def _big_pipe(f) -> None:
    try:
        import fcntl
        fcntl.fcntl(f.fileno(), 1031, PIPE_SIZE)  # F_SETPIPE_SZ
    except (ImportError, OSError):
        pass  # not Linux, or not allowed: just slower


def _frames(stream, frame_bytes: int):
    """Yield raw frames read by a helper thread. Each buffer is reused once the next few have been
    taken, so a caller must be done with a frame before asking for the one READ_AHEAD later."""
    free: queue.Queue = queue.Queue()
    full: queue.Queue = queue.Queue()
    for _ in range(READ_AHEAD + 2):
        free.put(bytearray(frame_bytes))
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                buf = free.get()
                if buf is None or not _read_exact(stream, memoryview(buf)):
                    break
                full.put(buf)
        except (OSError, ValueError):  # the decoder was stopped (cancel, error)
            pass
        full.put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        while (buf := full.get()) is not None:
            yield buf
            free.put(buf)
    finally:
        stop.set()
        free.put(None)


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
    _remove_leftovers(out_dir)
    # Written inside a hidden temporary folder and moved into place only when complete, so a
    # cancelled or failed save never leaves a half movie that Jellyfin would list.
    tmp = tempfile.mkdtemp(prefix=TMP_PREFIX, dir=out_dir)
    partial = os.path.join(tmp, "movie.mkv")
    decode = ["ffmpeg", "-v", "error", "-i", info.path, "-map", "0:v:0", "-vf", f"fps={info.fps}",
              "-pix_fmt", info.raw_pix_fmt, "-f", "rawvideo", "-"]
    encode = ["ffmpeg", "-y", "-v", "error",
              "-f", "rawvideo", "-pix_fmt", info.raw_pix_fmt, "-s", f"{W}x{H}", "-framerate", str(info.fps),
              *_color_args(info), "-itsoffset", f"{info.video_offset:.3f}", "-i", "-", "-i", info.path]
    maps = ["-map", "0:v"]
    if info.audio_index is not None:
        if word_ranges or skips:
            script = os.path.join(tmp, "audio.txt")
            everywhere, center_only = word_ranges, []
            if word_ranges and has_center(info):
                log("Checking which words are only on the center speaker")
                elsewhere = _voice_elsewhere(info, word_ranges)
                center_only = [r for r, e in zip(word_ranges, elsewhere) if not e]
                everywhere = [r for r, e in zip(word_ranges, elsewhere) if e]
                log(f"  {len(center_only)} words muted on the center speaker only (music keeps playing), "
                    f"{len(everywhere)} on every speaker (the voice is on other speakers too)")
            with open(script, "w") as f:
                f.write(_audio_filter(info, center_only, everywhere, settings.get("audio", "mute"), skips))
            # Two tracks: the filtered audio first, as the default, and the untouched original after
            # it, for anyone who wants to switch back (like picking another language). The original
            # is copied as is, unless scenes were cut: then it loses the same scenes, re-encoded.
            codec = ["ac3" if info.audio_channels <= 6 else "eac3", AUDIO_BITRATE.get(info.audio_channels, "640k")]
            filtered, original = _audio_titles(info)
            maps += ["-/filter_complex", script, "-map", "[aout]", "-c:a:0", codec[0], "-b:a:0", codec[1],
                     "-map", "[aorig]" if skips else f"1:a:{info.audio_index}"]
            maps += ["-c:a:1", codec[0], "-b:a:1", codec[1]] if skips else ["-c:a:1", "copy"]
            for n, title in enumerate((filtered, original)):
                maps += [f"-metadata:s:a:{n}", f"title={title}", f"-disposition:a:{n}", "default" if n == 0 else "0"]
                if info.audio_language:
                    maps += [f"-metadata:s:a:{n}", f"language={info.audio_language}"]
        else:
            maps += ["-map", f"1:a:{info.audio_index}", "-c:a", "copy"]

    # Subtitles: text ones are cleaned and re-timed; image ones are copied only if untouched.
    sub_args, n_out, next_input = [], 0, 2
    for st in subtitles.streams(info.path):
        name = st["tags"].get("title") or st["tags"].get("language") or f"#{st['index']}"
        if st["text"] and (lang_filters or skips):
            path, n = subtitles.clean_stream(info.path, st, tmp, lang_filters, timeline)
            if path is None:
                log(f"  subtitles {name}: left out (no lines in it)")
                continue
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
    encode += [*maps, *sub_args, *chapters, "-map_metadata", "1", *_video_codec_args(info), partial]
    cut = sum(b - a for a, b in skips)
    log(f"Rendering {W}x{H} @ {fps:.3f} fps with {encode[encode.index('-c:v') + 1]}, "
        f"{len(covers)} covers, {len(skips)} scenes cut ({cut:.1f} s), {len(word_ranges)} words")

    dec = subprocess.Popen(decode, stdout=subprocess.PIPE)
    enc = subprocess.Popen(encode, stdin=subprocess.PIPE)
    _big_pipe(dec.stdout)
    _big_pipe(enc.stdin)
    frame_bytes = (W * H + 2 * (W // 2) * (H // 2)) * (2 if info.ten_bit else 1)
    total = max(1, int(info.duration * fps))
    n, ci, si, last_pct = 0, 0, 0, -2
    active: list = []
    try:
        for buf in _frames(dec.stdout, frame_bytes):
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
            try:
                enc.stdin.write(buf)
            except BrokenPipeError:  # the encoder stopped (disk full, ...): say why below
                break
            pct = n / total * 100
            if pct - last_pct >= 2:
                log(f"  rendered {min(pct, 100):5.1f}%")
                last_pct = pct
        dec.stdout.close()
        dec.wait()
        try:
            enc.stdin.close()
        except BrokenPipeError:
            pass
        enc.wait()
        if enc.returncode != 0:
            raise RuntimeError(_encode_error(enc.returncode, out_dir))
        if dec.returncode != 0:  # the movie couldn't be read to the end: don't save a shortened copy
            raise RuntimeError(f"couldn't read the movie to the end (ffmpeg decoder exit code {dec.returncode})")
        os.replace(partial, out_path)
    finally:
        for p in (dec, enc):
            if p.poll() is None:
                p.kill()
            p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    log(f"Done: {out_path}")


TMP_PREFIX = ".cleancut-"


def _remove_leftovers(out_dir: str) -> None:
    """Temporary folders of saves that were cut off (power cut, crash) still hold a partial movie."""
    for e in os.scandir(out_dir):
        if e.is_dir() and e.name.startswith(TMP_PREFIX):
            shutil.rmtree(e.path, ignore_errors=True)


def _encode_error(code: int, out_dir: str) -> str:
    free = shutil.disk_usage(out_dir).free
    if free < 2e9:
        return f"the disk is full ({free / 1e9:.1f} GB free where the movie is saved). Free up space and save again"
    return f"ffmpeg encoder failed with exit code {code}"


def _merge_ranges(ranges):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
