"""Clean text subtitles: mask filtered words (d**n) and move cues to match cut-out scenes.

Image subtitles (Blu-ray PGS, DVD) are pictures of text, so they can't be cleaned without OCR;
they are dropped when they would need changing.
"""
import bisect
import json
import os
import re

from . import language
from .media import run

TEXT_CODECS = {"subrip": "srt", "srt": "srt", "mov_text": "srt", "webvtt": "srt", "text": "srt",
               "ass": "ass", "ssa": "ass"}

# Separators that are not words: ASS {tags}, HTML <tags>, ASS line breaks, whitespace.
_SPLIT = re.compile(r"(\{[^}]*\}|<[^>]*>|\\[Nnh]|\s+)")


def streams(path: str) -> list[dict]:
    """Subtitle streams: [{index (among subtitles), codec, text, tags, disposition}]."""
    out = run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
               "stream=codec_name:stream_tags:stream_disposition", "-of", "json", path]).stdout
    return [{"index": i, "codec": s.get("codec_name", ""), "text": s.get("codec_name") in TEXT_CODECS,
             "tags": s.get("tags", {}), "disposition": s.get("disposition", {})}
            for i, s in enumerate(json.loads(out).get("streams", []))]


def mask_text(text: str, enabled: set[str]) -> tuple[str, int]:
    """Mask every filtered word in one cue's text. Returns (text, words masked)."""
    parts = _SPLIT.split(text)
    word_at = [i for i, p in enumerate(parts) if p and not _SPLIT.fullmatch(p)]
    words = [parts[i] for i in word_at]
    n = 0
    for wi, pi in enumerate(word_at):
        if any(c in enabled for c in language.categorize(words, wi)):
            parts[pi] = language.mask(parts[pi])
            n += 1
    return "".join(parts), n


class Timeline:
    """Maps original times to output times once scenes are cut out."""

    def __init__(self, skips: list[tuple[float, float]]):
        self.skips = sorted(skips)
        self.starts = [a for a, _ in self.skips]
        self.removed_before = [0.0]
        for a, b in self.skips:
            self.removed_before.append(self.removed_before[-1] + (b - a))

    def map(self, t: float) -> float:
        """Output time of original time t (a time inside a cut lands where the cut is)."""
        i = bisect.bisect_right(self.starts, t)
        if i and t < self.skips[i - 1][1]:
            return self.skips[i - 1][0] - self.removed_before[i - 1]
        return t - self.removed_before[i]

    def map_range(self, a: float, b: float):
        """(a, b) in output time, or None if the whole range was cut."""
        a2, b2 = self.map(a), self.map(b)
        return (a2, b2) if b2 - a2 > 0.01 else None


def _srt_time(s: str) -> float:
    h, m, rest = s.strip().replace(".", ",").split(":")
    sec, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def _fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _ass_time(s: str) -> float:
    h, m, sec = s.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def _fmt_ass(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean_srt(text: str, enabled: set[str], timeline: Timeline) -> tuple[str, int]:
    out, masked, n = [], 0, 0
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, b = (_srt_time(x.split()[0]) for x in lines[ti].split("-->"))
        rng = timeline.map_range(a, b)
        if rng is None:
            continue
        body, k = mask_text("\n".join(lines[ti + 1:]), enabled)
        masked += k
        n += 1
        out.append(f"{n}\n{_fmt_srt(rng[0])} --> {_fmt_srt(rng[1])}\n{body}\n")
    return "\n".join(out), masked


def clean_ass(text: str, enabled: set[str], timeline: Timeline) -> tuple[str, int]:
    out, masked = [], 0
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("Dialogue:"):
            head, rest = line.split(":", 1)
            f = rest.split(",", 9)
            rng = timeline.map_range(_ass_time(f[1]), _ass_time(f[2]))
            if rng is None:
                continue
            f[1], f[2] = _fmt_ass(rng[0]), _fmt_ass(rng[1])
            f[9], k = mask_text(f[9], enabled)
            masked += k
            line = head + ":" + ",".join(f)
        out.append(line)
    return "\n".join(out), masked


def clean_stream(path: str, stream: dict, out_dir: str, enabled: set[str], timeline: Timeline) -> tuple[str, int]:
    """Extract one text subtitle stream, clean it, and return (cleaned file, words masked)."""
    fmt = TEXT_CODECS[stream["codec"]]
    raw = os.path.join(out_dir, f"sub{stream['index']}.orig.{fmt}")
    run(["ffmpeg", "-y", "-v", "error", "-i", path, "-map", f"0:s:{stream['index']}", "-c:s", fmt, raw])
    with open(raw, encoding="utf-8", errors="replace") as f:
        text = f.read()
    os.remove(raw)
    cleaned, n = (clean_ass if fmt == "ass" else clean_srt)(text, enabled, timeline)
    out = os.path.join(out_dir, f"sub{stream['index']}.{fmt}")
    with open(out, "w", encoding="utf-8") as f:
        f.write(cleaned)
    return out, n
