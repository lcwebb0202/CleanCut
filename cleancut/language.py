"""Find swear words in the dialogue, with word-level timestamps."""
import re

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
        nxt = normalize(words[i + 1]) if i + 1 < len(words) else ""
        if prev in _EXCLAIM_BEFORE or nxt in _EXCLAIM_AFTER:
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


def transcribe(wav_path: str, model_name: str | None = None, log=print) -> list[dict]:
    """Return [{word, start, end}] for the whole audio file."""
    import ctranslate2
    from faster_whisper import WhisperModel

    gpu = ctranslate2.get_cuda_device_count() > 0
    if model_name is None:
        model_name = "large-v3-turbo" if gpu else "small.en"
    device, compute = ("cuda", "float16") if gpu else ("cpu", "int8")
    log(f"Speech recognition: model {model_name} on {device}")
    model = WhisperModel(model_name, device=device, compute_type=compute)
    segments, info = model.transcribe(wav_path, language="en", word_timestamps=True,
                                      vad_filter=True, beam_size=5)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})
        log(f"  transcribed {seg.end / max(info.duration, 1) * 100:5.1f}%")
    return words


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
