"""Find swear words in the dialogue, with word-level timestamps."""
import re

import numpy as np

# Each category is a list of regexes matched against one normalized word
# (lowercase; letters, digits, apostrophes and asterisks only).
CATEGORIES: dict[str, list[str]] = {
    "fword": [r"^f+u+c+k", r"^motherf", r"^f\*+", r"^mother\*+", r"^mf+er"],
    "strong": [
        r"^(bull|horse|dip|ape)?shit", r"^s\*+t", r"^bitch", r"^b\*+h", r"^bastard",
        r"^(dumb|jack|smart|bad|kick)?ass(hole|holes|es)?$", r"^a\*+s?$", r"^asshole",
        r"^dick(s|head|heads)?$", r"^cock(s|sucker|suckers)?$", r"^pussy", r"^pussies",
        r"^cunt", r"^twat", r"^prick(s)?$", r"^whore",
    ],
    "mild": [r"^(god)?damn", r"^(god)?dammit", r"^hell$", r"^d\*+n$"],
    "blasphemy": [r"^goddam", r"^g\*+d"],  # plus the exclamation rules in categorize()
    "slurs": [
        r"^nigg", r"^n\*+a", r"^fag(s|got|gots)?$", r"^retard(s|ed)?$", r"^spic(s)?$",
        r"^chink(s)?$", r"^kike(s)?$", r"^wetback", r"^trann(y|ies)$", r"^dyke(s)?$",
        r"^coon(s)?$", r"^gook(s)?$", r"^beaner",
    ],
}
_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in CATEGORIES.items()}

# "god" / "lord" only count as taking God's name in vain in exclamations.
_EXCLAIM_BEFORE = {"oh", "my", "dear", "good", "for"}
_EXCLAIM_AFTER = {"damn", "dammit", "damnit", "almighty", "sake", "god's"}
_PRAYER = {"please", "help", "bless", "forgive", "thank", "save", "grant", "protect", "give", "we", "our"}

# "Jesus" / "Christ" only count as exclamations ("Jesus!", "Jesus Christ, calm down",
# "oh Jesus", "for Christ's sake"), never in sentences like "Jesus said to them".
_HOLY = {"jesus", "christ", "christ's", "h"}
_INTERJECTIONS = {"oh", "sweet", "good", "my", "ah", "aw", "lord"}


def _sentence_start(words: list[str], i: int) -> bool:
    if i == 0:
        return True
    prev = words[i - 1].strip()
    return prev[-1:] in ".!?" or (normalize(prev) in _INTERJECTIONS and _sentence_start(words, i - 1))


def _holy_exclamation(words: list[str], i: int) -> bool:
    s = e = i
    while s > 0 and normalize(words[s - 1]) in _HOLY:
        s -= 1
    while e + 1 < len(words) and normalize(words[e + 1]) in _HOLY:
        e += 1
    nxt = normalize(words[e + 1]) if e + 1 < len(words) else ""
    if nxt in ("sake", "almighty"):
        return True
    ends_phrase = e + 1 == len(words) or words[e].strip()[-1:] in ",.!?"
    return ends_phrase and _sentence_start(words, s)

PAD_BEFORE = 0.05  # seconds of extra silence around each word
PAD_AFTER = 0.08


def normalize(word: str) -> str:
    return re.sub(r"[^a-z0-9'*]", "", word.lower()).strip("'")


def categorize(words: list[str], i: int) -> list[str]:
    """Every category word i belongs to ("goddammit" is both mild and blasphemy)."""
    w = normalize(words[i])
    if not w:
        return []
    cats = [cat for cat, pats in _COMPILED.items() if any(p.search(w) for p in pats)]
    if w in ("god", "lord", "god's"):
        prev = normalize(words[i - 1]) if i > 0 else ""
        prev2 = normalize(words[i - 2]) if i > 1 else ""
        nxt = normalize(words[i + 1]) if i + 1 < len(words) else ""
        # "God, I hope not." / "God!" open a sentence as an exclamation; "God, please help us" is a prayer.
        exclaim_start = (w == "god" and _sentence_start(words, i) and words[i].strip()[-1:] in ",!"
                         and nxt not in _PRAYER)
        if prev in _EXCLAIM_BEFORE or nxt in _EXCLAIM_AFTER or exclaim_start or (prev2, prev) == ("swear", "to"):
            cats.append("blasphemy")
    if w in ("jesus", "christ", "christ's") and _holy_exclamation(words, i):
        cats.append("blasphemy")
    return cats


def mask(word: str) -> str:
    """Keep the first and last letters: damn -> d**n, ass -> a*s."""
    letters = [i for i, c in enumerate(word) if c.isalpha()]
    if len(letters) < 3:
        return word[0] + "*" * (len(word) - 1) if word else word
    first, last = letters[0], letters[-1]
    return word[:first + 1] + "".join("*" if c.isalpha() else c for c in word[first + 1:last]) + word[last:]


class Listener:
    """Whisper, loaded once: it listens to the whole film, then to single lines again when the
    subtitles show a swear it missed. Times are shifted by `offset` onto the file's timeline."""

    RATE = 16000

    def __init__(self, wav_path: str, model_name: str | None = None, offset: float = 0.0, log=print):
        import ctranslate2
        from faster_whisper import WhisperModel

        gpu = ctranslate2.get_cuda_device_count() > 0
        if model_name is None:
            model_name = "large-v3-turbo" if gpu else "small.en"
        device, compute = ("cuda", "float16") if gpu else ("cpu", "int8")
        log(f"Speech recognition: model {model_name} on {device}")
        self.model = WhisperModel(model_name, device=device, compute_type=compute)
        self.wav_path, self.offset, self.log = wav_path, offset, log
        self._audio = None

    def _words(self, segments, shift: float) -> list[dict]:
        return [{"word": w.word.strip(), "start": round(w.start + shift, 3), "end": round(w.end + shift, 3)}
                for seg in segments for w in (seg.words or [])]

    def transcribe(self) -> list[dict]:
        """[{word, start, end}] for the whole audio file."""
        segments, info = self.model.transcribe(self.wav_path, language="en", word_timestamps=True,
                                               vad_filter=True, beam_size=5)
        words = []
        for seg in segments:
            words += self._words([seg], self.offset)
            self.log(f"  transcribed {seg.end / max(info.duration, 1) * 100:5.1f}%")
        return words

    def relisten(self, a: float, b: float, prompt: str) -> list[dict]:
        """Words between file times a and b, listening for the given text (a subtitle line).
        No voice-activity filter here: that is what drops shouted lines over loud action."""
        if self._audio is None:
            from faster_whisper import decode_audio
            self._audio = decode_audio(self.wav_path, sampling_rate=self.RATE)
        i, j = (max(0, int((t - self.offset) * self.RATE)) for t in (a, b))
        if j - i < self.RATE // 4:
            return []
        segments, _ = self.model.transcribe(self._audio[i:j], language="en", word_timestamps=True,
                                            vad_filter=False, beam_size=5, initial_prompt=prompt,
                                            condition_on_previous_text=False)
        return self._words(segments, i / self.RATE + self.offset)


# ---------------------------------------------------------------- subtitles as a second opinion
# Whisper drops short shouted lines over loud music and fights ("Ah, shit!" in a car chase). A
# movie's own text subtitles usually have them, so every swear in the subtitles is checked against
# what Whisper heard; missed lines are listened to again, and as a last resort muted from the
# subtitle timing. On Deadpool (2016) Whisper alone caught 90 of the 138 subtitle swears.

SUB_SLACK = 1.0       # seconds around a subtitle line where a heard word counts as that line's word
APPROX_BEFORE, APPROX_AFTER = 0.45, 0.55  # mute this much around a word placed from subtitle timing


def _tokens(text: str) -> list[str]:
    return [t for t in re.sub(r"^\s*-\s*|\s-\s", " ", text).split() if normalize(t)]


def subtitle_offset(cues: list[tuple], words: list[dict]) -> float | None:
    """How much later the speech is than the subtitles (seconds), or None if they don't match up
    (wrong language, a different cut of the film...). Found by lining up the first two words of
    each subtitle line with the transcript."""
    seq = [normalize(w["word"]) for w in words]
    starts = [w["start"] for w in words]
    at: dict[tuple, list[int]] = {}
    for i in range(len(seq) - 1):
        at.setdefault((seq[i], seq[i + 1]), []).append(i)
    diffs = []
    for s, _, text in cues:
        toks = [normalize(t) for t in _tokens(text)]
        if len(toks) < 2 or len(toks[0]) + len(toks[1]) < 5:
            continue
        near = [starts[i] - s for i in at.get((toks[0], toks[1]), []) if abs(starts[i] - s) < 20]
        if len(near) == 1:
            diffs.append(near[0])
    if len(diffs) < 15:
        return None
    off = float(np.median(diffs))
    return off if np.mean(np.abs(np.array(diffs) - off) <= 1.0) >= 0.6 else None


def subtitle_backup(cues: list[tuple], words: list[dict], hits: list[dict], enabled: set[str],
                    listener: "Listener | None", log=print) -> list[dict]:
    """Extra language events for swears that are in the subtitles but weren't heard."""
    off = subtitle_offset(cues, words)
    if off is None:
        log("  subtitles don't line up with the speech, so they weren't used to double-check")
        return []
    found: list[dict] = []
    relistened = approx = 0
    for s, e, text in cues:
        s, e = s + off, e + off
        toks = _tokens(text)
        want = [(i, c) for i in range(len(toks)) for c in categorize(toks, i)[:1] if c in enabled]
        if not want:
            continue

        def missing(pool):
            got = [h for h in pool if s - SUB_SLACK <= h["start"] <= e + SUB_SLACK]
            out = []
            for i, c in want:
                same = [h for h in got if h["category"] == c]
                if same:
                    got.remove(same[0])
                else:
                    out.append((i, c))
            return out

        if not missing(hits + found):
            continue
        if listener is not None:
            heard = listener.relisten(s - 0.6, e + 0.6, text)
            relistened += 1
            for h in find_hits(heard, enabled):
                if any(_overlap(h, x) for x in hits + found):
                    continue
                h["source"] = "subtitles"
                found.append(h)
        # Still missing: place the word by where it sits in the line.
        total = max(1, sum(len(t) + 1 for t in toks))
        for i, c in missing(hits + found):
            mid = (sum(len(t) + 1 for t in toks[:i]) + len(toks[i]) / 2) / total
            t = s + (e - s) * mid
            found.append({"type": "language", "category": c, "word": mask(normalize(toks[i])),
                          "start": round(max(s, t - APPROX_BEFORE), 3), "end": round(min(e, t + APPROX_AFTER), 3),
                          "action": "censor", "source": "subtitles", "approx": True})
            approx += 1
    if found:
        log(f"  subtitles caught {len(found)} more words ({relistened} lines listened to again, "
            f"{approx} placed from subtitle timing)")
    return found


def _overlap(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def find_hits(words: list[dict], enabled: set[str]) -> list[dict]:
    """Words to silence, as plan events."""
    texts = [w["word"] for w in words]
    hits = []
    for i, w in enumerate(words):
        cats = [c for c in categorize(texts, i) if c in enabled]
        if cats:
            hits.append({
                "type": "language",
                "category": cats[0],
                "word": mask(normalize(w["word"])),
                "start": round(max(0.0, w["start"] - PAD_BEFORE), 3),
                "end": round(w["end"] + PAD_AFTER, 3),
                "action": "censor",
            })
    return hits
