"""Compare speech-recognition setups against a movie's own text subtitles.

  python tests/lang_compare.py transcribe MOVIE OUT.json --mix downmix|center|dialog [--vad 0.5] [--no-vad]
  python tests/lang_compare.py score SUBS.srt A.json [B.json ...]

`score` counts swears in the subtitles and how many of them each transcript caught (same category,
within a second of the subtitle cue). Numbers only: nothing is played or shown.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cleancut import language, subtitles  # noqa: E402
from cleancut.media import probe  # noqa: E402


def transcribe(a):
    from faster_whisper import WhisperModel
    info = probe(a.movie)
    wav = a.out + ".wav"
    af = {"downmix": [], "center": ["-af", "pan=mono|c0=FC"],
          "dialog": ["-af", "pan=mono|c0=FC+0.3*FL+0.3*FR"]}[a.mix]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", a.movie, "-map", f"0:a:{info.audio_index}", *af,
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], check=True)
    model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
    kw = dict(language="en", word_timestamps=True, beam_size=5, vad_filter=not a.no_vad)
    if a.vad is not None:
        kw["vad_parameters"] = {"threshold": a.vad}
    if a.no_cond:
        kw["condition_on_previous_text"] = False
    segs, _ = model.transcribe(wav, **kw)
    words = [{"word": w.word.strip(), "start": round(w.start + info.audio_offset, 3),
              "end": round(w.end + info.audio_offset, 3)} for s in segs for w in (s.words or [])]
    os.remove(wav)
    json.dump(words, open(a.out, "w"))
    print(len(words), "words")


def backup(a):
    """Whisper transcript (from `transcribe`) + the subtitle double-check, as in pipeline.analyze."""
    from cleancut.media import extract_speech_audio
    info = probe(a.movie)
    wav = a.out + ".wav"
    extract_speech_audio(info, wav)
    words = json.load(open(a.transcript))
    allc = set(language.CATEGORIES) - {"slurs"}
    hits = language.find_hits(words, allc)
    listener = language.Listener(wav, offset=info.audio_offset)
    extra = language.subtitle_backup(subtitles.dialogue_cues(a.movie), words, hits, allc, listener)
    os.remove(wav)
    json.dump(hits + extra, open(a.out, "w"))


def cues(path):
    out = []
    for b in re.split(r"\n\s*\n", open(path, encoding="utf-8-sig").read().strip()):
        lines = b.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, b2 = [subtitles._srt_time(x) for x in lines[1].split("-->")]
        out.append((a, b2, re.sub(r"<[^>]+>", "", " ".join(lines[2:]))))
    return out


def score(a):
    allc = set(language.CATEGORIES) - {"slurs"}
    targets = []
    for s, e, text in cues(a.subs):
        toks = text.split()
        for i in range(len(toks)):
            for c in language.categorize(toks, i):
                if c in allc:
                    targets.append((s, e, c, toks[i]))
    print(len(targets), "swears in the subtitles")
    for path in a.transcripts:
        data = json.load(open(path))
        # A list of hits (from `backup`) or a plain transcript.
        hits = data if data and "category" in data[0] else language.find_hits(data, allc)
        got = sum(1 for s, e, c, w in targets if any(h["category"] == c and s - 1 <= h["start"] <= e + 1 for h in hits))
        print(f"{os.path.basename(path):28s} caught {got}/{len(targets)}  ({len(hits)} words muted in total)")
        if a.misses:
            for s, e, c, w in targets:
                if not any(h["category"] == c and s - 1 <= h["start"] <= e + 1 for h in hits):
                    print(f"   miss {int(s)//60}:{int(s)%60:02d} {c} {w}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("movie"); t.add_argument("out")
    t.add_argument("--mix", default="downmix"); t.add_argument("--vad", type=float)
    t.add_argument("--no-vad", action="store_true"); t.add_argument("--no-cond", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("subs"); s.add_argument("transcripts", nargs="+"); s.add_argument("--misses", action="store_true")
    b = sub.add_parser("backup")
    b.add_argument("movie"); b.add_argument("transcript"); b.add_argument("out")
    a = ap.parse_args()
    {"transcribe": transcribe, "score": score, "backup": backup}[a.cmd](a)
