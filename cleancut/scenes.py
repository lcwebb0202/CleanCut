"""Scene-level checks: sex scenes and sexually suggestive scenes (strippers, underwear, kissing).

NudeNet only sees exposed body parts, so a sex scene shot with bare backs or under sheets goes
unnoticed, and it can't judge a whole scene. Here every checked frame is turned into a CLIP
picture embedding and asked two questions (see frame_signals). A scene is flagged only when the
signal holds across a camera shot for a few seconds, never on a single frame.
"""
import bisect
import os

import cv2
import numpy as np

MODEL_DIR = "/opt/models/clip"
SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
LOGIT_SCALE = 100.0

CATEGORIES = ("sex", "suggestive")
SIGNAL = {"sex": "nsfw_sex", "suggestive": "nsfw_sugg"}

# Scene rules, tuned on Deadpool (2016) against known scenes (tests/tune_scan.py):
# a shot counts when enough of its frames pass the bar; nearby counting shots join into a scene.
STRONG, STRONG_SHARE, STRONG_SECONDS = 0.8, 0.4, 3.0        # covered without asking
POSSIBLE, POSSIBLE_SHARE, POSSIBLE_SECONDS = 0.7, 0.3, 1.5  # listed in review, not covered
# Short scenes are covered too when they are very clear: VERY_SECONDS of frames >= VERY, or
# CLEAR_SECONDS of frames >= STRONG anywhere in the scene (The Wolf of Wall Street had several
# 4-9 s sex scenes at 93-99% that the 3 s rule left for review).
VERY, VERY_SECONDS, CLEAR_SECONDS = 0.9, 1.0, 2.0
STEADY, STEADY_SECONDS = 0.8, 1.5   # ...or strong all the way through: average >= STEADY over 1.5 s+
SCENE_GAP = 20.0   # seconds between counting shots that still makes them one scene (a sex scene
                   # intercut with calmer shots stays one scene; Oppenheimer 23:00 has 15 s gaps)
# A covered scene also takes in nearby shots that are only fairly sexual (Deadpool 25:28: a bare
# backside the strict bar missed between two covered parts of a montage), and covered scenes this
# close together become one.
CONTINUE, CONTINUE_SHARE = 0.5, 0.5
BRIDGE_GAP = 45.0
NEAR = 2.0         # seconds around a body-part detection to look for agreement

# ---------------------------------------------------------------- frame signals (v2)
# Two questions per frame, answered by two models on the same CLIP picture embedding:
#  1. "Is this sexual at all?" - LAION's NSFW detector, trained on many labeled images
#     (explicit 100%, lingerie/bikini ~95%, ordinary photos ~6% on LAION's test set).
#  2. "What kind?" - CLIP text descriptions: sex, nudity, suggestive, swimsuit or ordinary.
#     The ordinary list includes the things that fooled the old version (logos, credits, red
#     superhero suits, fights).

NSFW_WEIGHTS = os.path.join(os.path.dirname(__file__), "models", "laion_nsfw_l14.npz")

KIND_PROMPTS = {
    "sex": [
        "a couple having sex",
        "two naked people in bed together",
        "people making love",
        "a naked woman on top of a man",
        "an explicit sex scene",
    ],
    "nude": [
        "a naked woman",
        "a topless woman",
        "a naked man",
        "bare breasts",
        "a nude body",
    ],
    "suggestive": [
        "a woman in lingerie",
        "a woman in her underwear",
        "a woman in a bra and panties",
        "a stripper dancing on a pole",
        "a strip club with dancers",
        "a lap dance",
        "a couple kissing passionately",
    ],
    "swimsuit": [
        "a woman in a bikini at the beach",
        "people in swimsuits at a pool",
    ],
    "other": [
        "people talking", "two people having a conversation", "a close-up of a face",
        "a man in a suit", "a woman in a dress", "a man and a woman sitting together",
        "a couple hugging", "people at a party", "a crowd of people", "people in a bar",
        "people sitting at a table", "a man lying in bed alone", "a man shirtless",
        "a superhero in a red costume", "a man in a red suit and mask", "a man with a scarred face",
        "a fight scene", "a car chase", "an explosion", "a man holding a gun", "soldiers",
        "a movie studio logo", "film credits on a black background", "text on a screen", "a title card",
        "a cartoon", "a city street", "a landscape", "a room interior", "a dark room", "a black screen",
        "a hospital", "an office", "a laboratory", "a kitchen", "the inside of a car",
        # Fights fooled it (wrestling and a skin-colored suit look like bodies). Adding these
        # removed 4 of 6 fight false alarms in Deadpool; every real scene stayed covered.
        "two people fighting", "a man punching another man", "a woman fighting a man", "a martial arts fight",
        "people wrestling on the ground", "a man in a metal suit", "a giant chrome man",
        "a man in a red costume fighting", "a battle in a scrapyard", "a man getting hit", "a man being tortured",
        "a man strapped to a table", "a man in a hospital bed", "a burned man without a shirt",
    ],
}
KINDS = tuple(KIND_PROMPTS)

_text_cache: dict = {}
_nsfw_cache: dict = {}


def kind_text_embeds() -> tuple[np.ndarray, np.ndarray]:
    """(text embeddings, kind label per row) for KIND_PROMPTS, computed once."""
    if "emb" not in _text_cache:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        text = ort.InferenceSession(os.path.join(MODEL_DIR, "text_model.onnx"), providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
        rows, labels = [], []
        for kind, prompts in KIND_PROMPTS.items():
            for p in prompts:
                ids = np.array([tok.encode(p).ids], np.int64)
                feeds = {i.name: (ids if i.name == "input_ids" else np.ones_like(ids)) for i in text.get_inputs()}
                out = dict(zip([o.name for o in text.get_outputs()], text.run(None, feeds)))
                e = out.get("text_embeds", next(iter(out.values())))[0]
                rows.append(e / np.linalg.norm(e))
                labels.append(kind)
        _text_cache["emb"] = (np.stack(rows).astype(np.float32), np.array(labels))
    return _text_cache["emb"]


def nsfw_score(emb: np.ndarray) -> np.ndarray:
    """LAION NSFW probability for L2-normalized CLIP ViT-L/14 image embeddings (..., 768)."""
    if "w" not in _nsfw_cache:
        _nsfw_cache["w"] = dict(np.load(NSFW_WEIGHTS))
    w = _nsfw_cache["w"]
    x = (emb - w["mean"]) / np.sqrt(np.maximum(w["var"], 1e-7))
    for i in (1, 2, 3):
        x = np.maximum(x @ w[f"w{i}"] + w[f"b{i}"], 0)
    z = (x @ w["w4"] + w["b4"])[..., 0]
    return 1.0 / (1.0 + np.exp(-z))


def kind_probs(emb: np.ndarray) -> dict[str, np.ndarray]:
    """Share of each kind (sex, nude, ...) for normalized embeddings (..., 768)."""
    text, labels = kind_text_embeds()
    logits = LOGIT_SCALE * emb @ text.T
    p = np.exp(logits - logits.max(axis=-1, keepdims=True))
    p /= p.sum(axis=-1, keepdims=True)
    return {k: p[..., labels == k].sum(axis=-1) for k in KINDS}


NUDE_CLASSES = {"FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
                "BUTTOCKS_EXPOSED", "ANUS_EXPOSED"}


def frame_signals(emb: np.ndarray, valid: np.ndarray, dets: list) -> dict[str, np.ndarray]:
    """Per-frame signals from crop embeddings (N, crops, 768, normalized; `valid` marks real
    crops) and NudeNet detections ([[class, score, ...], ...] per frame). Each crop is scored on
    its own and the worst crop wins, so something at the side of a wide frame still counts."""
    nsfw = np.where(valid, nsfw_score(emb), 0.0)
    kinds = kind_probs(emb)
    out = {"nsfw": nsfw.max(axis=1)}
    for k in ("sex", "nude", "suggestive", "swimsuit"):
        out[k] = np.where(valid, kinds[k], 0.0).max(axis=1)
    # Sexual AND of a sexual kind (not a swimsuit, not ordinary), per crop.
    sexual = kinds["sex"] + kinds["nude"]
    out["nsfw_sex"] = np.where(valid, nsfw * np.clip(sexual / np.maximum(sexual + kinds["other"] + kinds["swimsuit"], 1e-6), 0, 1), 0).max(axis=1)
    out["nsfw_sugg"] = np.where(valid, nsfw * np.clip(kinds["suggestive"] / np.maximum(kinds["suggestive"] + kinds["other"] + kinds["swimsuit"], 1e-6), 0, 1), 0).max(axis=1)
    out["nudenet"] = np.array([max([d[1] for d in fd if d[0] in NUDE_CLASSES], default=0.0) for fd in dets])
    return out


class SceneDetector:
    """CLIP picture embeddings for sampled frames, turned into frame_signals."""

    def __init__(self, log=print):
        import onnxruntime as ort

        from .media import onnx_providers
        self.vision = ort.InferenceSession(os.path.join(MODEL_DIR, "vision_model.onnx"), providers=onnx_providers())
        self.gpu = self.vision.get_providers()[0].startswith("CUDA")
        self.input = self.vision.get_inputs()[0].name
        kind_text_embeds()
        nsfw_score(np.zeros((1, 768), np.float32))
        log(f"Scene detection on {'GPU' if self.gpu else 'CPU'} (CLIP ViT-L/14 + LAION NSFW detector)")

    def _crops(self, bgr: np.ndarray) -> np.ndarray:
        """Square crops covering the whole frame (wide films get left, middle and right)."""
        h, w = bgr.shape[:2]
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        n = 1 if w <= h * 1.3 else 3
        side = min(h, w)
        xs = [0] if n == 1 else np.linspace(0, w - side, n).astype(int)
        crops = []
        for x in xs:
            c = cv2.resize(rgb[:, x:x + side] if w >= h else rgb[(h - side) // 2:(h + side) // 2],
                           (SIZE, SIZE), interpolation=cv2.INTER_CUBIC)
            crops.append(((c.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1))
        return np.stack(crops)

    def embed(self, bgr: np.ndarray) -> np.ndarray:
        """Normalized CLIP embedding of each crop, (crops, 768)."""
        out = self.vision.run(None, {self.input: self._crops(bgr)})
        names = [o.name for o in self.vision.get_outputs()]
        emb = dict(zip(names, out)).get("image_embeds", out[0])
        return emb / np.linalg.norm(emb, axis=1, keepdims=True)

    def score(self, bgr: np.ndarray) -> dict[str, float]:
        emb = self.embed(bgr)[None]
        sig = frame_signals(emb, np.ones(emb.shape[:2], bool), [[]])
        return {k: round(float(v[0]), 3) for k, v in sig.items() if k != "nudenet"}


def _scene_dt(samples: list[dict]) -> float:
    ts = [s["t"] for s in samples if "scene" in s]
    return float(np.median(np.diff(ts))) if len(ts) > 1 else 0.5


def find_scenes(samples: list[dict], cuts: list[float], duration: float, category: str) -> list[dict]:
    """[{start, end, score, sure}] for one category. Each camera shot counts as strong or possible
    when enough of its checked frames pass the bar; counting shots within SCENE_GAP join up.
    A scene is `sure` with at least STRONG_SECONDS of strong frames, otherwise it's only listed
    for review if it has POSSIBLE_SECONDS of possible frames."""
    sig = SIGNAL[category]
    dt = _scene_dt(samples)
    bounds = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
    starts = bounds[:-1]
    per_shot: dict[int, list[float]] = {}
    for s in samples:
        if "scene" in s:
            per_shot.setdefault(max(0, bisect.bisect_right(starts, s["t"]) - 1), []).append(s["scene"][sig])
    scenes: list[dict] = []
    for i, vals in sorted(per_shot.items()):
        v = np.array(vals)
        strong, possible = int((v >= STRONG).sum()), int((v >= POSSIBLE).sum())
        if not (strong and strong / len(v) >= STRONG_SHARE) and not (possible and possible / len(v) >= POSSIBLE_SHARE):
            continue
        a, b = bounds[i], bounds[i + 1]
        strong_s = strong * dt if strong / len(v) >= STRONG_SHARE else 0.0
        very = int((v >= VERY).sum())
        if scenes and a - scenes[-1]["end"] <= SCENE_GAP:
            sc = scenes[-1]
            sc["end"], sc["strong_s"], sc["possible_s"] = b, sc["strong_s"] + strong_s, sc["possible_s"] + possible * dt
            sc["score"], sc["very"], sc["strong_n"] = max(sc["score"], float(v.max())), sc["very"] + very, sc["strong_n"] + strong
            sc["sum"], sc["n"] = sc["sum"] + float(v.sum()), sc["n"] + len(v)
        else:
            scenes.append({"start": a, "end": b, "strong_s": strong_s, "possible_s": possible * dt, "score": float(v.max()),
                           "very": very, "strong_n": strong, "sum": float(v.sum()), "n": len(v)})
    for sc in scenes:
        sc["sure"] = (sc["strong_s"] >= STRONG_SECONDS or sc["very"] * dt >= VERY_SECONDS
                      or sc["strong_n"] * dt >= CLEAR_SECONDS
                      or (sc["n"] * dt >= STEADY_SECONDS and sc["sum"] / sc["n"] >= STEADY))
    scenes = [sc for sc in scenes if sc["sure"] or sc["possible_s"] >= POSSIBLE_SECONDS]
    sure = [sc for sc in scenes if sc["sure"]]
    if not sure:
        return scenes
    weak = [(bounds[i], bounds[i + 1]) for i, vals in per_shot.items()
            if np.mean(np.array(vals) >= CONTINUE) >= CONTINUE_SHARE]
    for sc in sure:
        grew = True
        while grew:
            grew = False
            for a, b in weak:
                if sc["end"] < b and 0 <= a - sc["end"] <= SCENE_GAP:
                    sc["end"], grew = b, True
                if a < sc["start"] and 0 <= sc["start"] - b <= SCENE_GAP:
                    sc["start"], grew = a, True
    joined = [sure[0]]
    for sc in sure[1:]:
        if sc["start"] - joined[-1]["end"] <= BRIDGE_GAP:
            prev = joined[-1]
            prev["start"], prev["end"] = min(prev["start"], sc["start"]), max(prev["end"], sc["end"])
            prev["score"] = max(prev["score"], sc["score"])
        else:
            joined.append(sc)
    maybe = [sc for sc in scenes if not sc["sure"]
             and not any(j["start"] <= sc["start"] and sc["end"] <= j["end"] for j in joined)]
    return sorted(joined + maybe, key=lambda sc: sc["start"])


def nsfw_near(samples: list[dict], a: float, b: float) -> float:
    """Highest "sexual, and sex or nudity" score from NEAR seconds before a to NEAR seconds after b.
    (The plain NSFW score alone is also high on red superhero suits and some credits.)"""
    return max((s["scene"]["nsfw_sex"] for s in samples if "scene" in s and a - NEAR <= s["t"] <= b + NEAR), default=0.0)


# Sounds, not dialogue: a scene whose transcript is only these has nothing worth keeping.
FILLER = {"oh", "ah", "ahh", "aah", "mm", "mmm", "mhm", "hmm", "uh", "um", "huh", "ooh", "ow", "yes", "yeah",
          "no", "god", "please", "baby", "okay", "ok", "wow", "hey", "shh"}
DIALOGUE_WORDS = 6   # a sex scene with at least this many real words is blacked out, not skipped
CUT_MARGIN = 0.05


def dialogue_words(words: list[dict], a: float, b: float) -> int:
    """Real words (not FILLER sounds) spoken between a and b."""
    return sum(1 for w in words if a <= w["start"] < b and w["word"].strip(",.!?'\"-").lower() not in FILLER)


def scene_events(samples: list[dict], cuts: list[float], categories: set[str], words: list[dict] | None,
                 duration: float) -> list[dict]:
    """Plan events for flagged scenes.

    Sex scenes with no meaningful dialogue are skipped (cut out); with dialogue, or when there
    is no transcript, they are blacked out so the conversation stays. Suggestive scenes are
    blacked out. Scenes that were only possible are listed for review but not covered.
    """
    events = []
    for cat in sorted(categories & set(CATEGORIES)):
        for sc in find_scenes(samples, cuts, duration, cat):
            start = round(max(0.0, sc["start"] - CUT_MARGIN), 3)
            end = round(min(duration, sc["end"]), 3)
            ev = {"type": "visual", "groups": [cat], "start": start, "end": end,
                  "confidence": round(sc["score"], 3), "unsure": [],
                  "covers": [{"start": start, "end": end, "box": None}]}
            if words is not None:
                ev["dialogue_words"] = dialogue_words(words, start, end)
            name = "sex" if cat == "sex" else "suggestive"
            if not sc["sure"]:
                ev["action"] = "ignore"
                ev["unsure"].append(f"possible {name} scene - not covered")
            elif cat == "sex" and words is not None and ev["dialogue_words"] < DIALOGUE_WORDS:
                ev["action"] = "skip"
            else:
                ev["action"] = "blackout"
            events.append(ev)
    return events
