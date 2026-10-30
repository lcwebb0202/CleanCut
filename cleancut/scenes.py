"""Scene-level checks with CLIP: sex scenes, intense kissing, strippers.

NudeNet only sees exposed body parts, so a sex scene shot with bare backs, shoulders or under
sheets goes unnoticed. CLIP looks at the whole picture and scores it against text descriptions
("a couple having sex in bed", "people talking", ...). Each sampled frame gets a probability
per category; shots are averaged, and runs of flagged shots become one scene.
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
# Raw match strength (cosine x 100) a category's best description must reach to count at all.
# "an erotic scene" matches any film still a little (~17-18, even a desert); real sex scenes
# reach 21-24. Between GATE_LO and GATE_HI the score fades in.
GATE_LO, GATE_HI = 18.5, 20.5

# What to look for. Every prompt competes with every other one (softmax), so the long
# "other" list is what keeps ordinary shots from scoring high: a dark close-up of a face
# has to look more like "a couple having sex" than like "a close-up of a man's face".
PROMPTS = {
    "sex": [
        "a sex scene in a movie",
        "a couple having sex in bed",
        "a naked couple in bed",
        "a naked woman on top of a man",
        "two people making love",
        "an erotic scene",
        "a nude woman",
        "a topless woman",
    ],
    "kissing": [
        "a couple kissing passionately",
        "two people making out",
    ],
    "strippers": [
        "a stripper dancing",
        "a woman pole dancing in a strip club",
        "a lap dance",
    ],
    "other": [
        "people talking",
        "two people having a conversation",
        "a close-up of a man's face",
        "a close-up of a woman's face",
        "a man in a suit",
        "a woman in a dress",
        "a man and a woman sitting together",
        "a man lying in bed alone",
        "a couple hugging",
        "people at a party",
        "a crowd of people",
        "people sitting at a table",
        "a man reading a book",
        "a man smoking",
        "a city street",
        "a landscape",
        "an explosion",
        "a dark room",
        "a black screen",
        "a blurry photo",
        "a man and a woman dancing",
        "a woman in a swimsuit at the beach",
        "people in an office",
        "a classroom",
        "a man shirtless",
        "a man and a woman standing in a room",
        "a woman wearing a robe",
    ],
}
CATEGORIES = ("sex", "kissing", "strippers")

# Per-shot average probability to cover a shot without asking, and to list it for review.
COVER_SCORE = {"sex": 0.50, "kissing": 0.55, "strippers": 0.55}
REVIEW_SCORE = {"sex": 0.25, "kissing": 0.30, "strippers": 0.30}
SCENE_GAP = 10.0         # flagged shots this close together (seconds) are one scene


class SceneDetector:
    def __init__(self, log=print):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        from .media import onnx_providers
        providers = onnx_providers()
        self.vision = ort.InferenceSession(os.path.join(MODEL_DIR, "vision_model.onnx"), providers=providers)
        self.gpu = self.vision.get_providers()[0].startswith("CUDA")
        text = ort.InferenceSession(os.path.join(MODEL_DIR, "text_model.onnx"), providers=providers)
        tok = Tokenizer.from_file(os.path.join(MODEL_DIR, "tokenizer.json"))
        self.labels, embeds = [], []
        for cat, prompts in PROMPTS.items():
            for p in prompts:
                ids = np.array([tok.encode(p).ids], np.int64)
                feeds = {i.name: (ids if i.name == "input_ids" else np.ones_like(ids)) for i in text.get_inputs()}
                out = dict(zip([o.name for o in text.get_outputs()], text.run(None, feeds)))
                e = out.get("text_embeds", next(iter(out.values())))[0]
                embeds.append(e / np.linalg.norm(e))
                self.labels.append(cat)
        del text
        self.text = np.stack(embeds).astype(np.float32)
        self.labels = np.array(self.labels)
        self.input = self.vision.get_inputs()[0].name
        log(f"Scene detection on {'GPU' if self.gpu else 'CPU'} (CLIP ViT-L/14, {len(self.labels)} descriptions)")

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

    def score(self, bgr: np.ndarray) -> dict[str, float]:
        """Probability of each category; the highest-scoring crop wins."""
        out = self.vision.run(None, {self.input: self._crops(bgr)})
        names = [o.name for o in self.vision.get_outputs()]
        emb = dict(zip(names, out)).get("image_embeds", out[0])
        emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
        logits = LOGIT_SCALE * emb @ self.text.T
        probs = np.exp(logits - logits.max(axis=1, keepdims=True))
        probs /= probs.sum(axis=1, keepdims=True)
        out = {}
        for cat in CATEGORIES:
            mine = self.labels == cat
            gate = np.clip((logits[:, mine].max(axis=1) - GATE_LO) / (GATE_HI - GATE_LO), 0, 1)
            out[cat] = round(float((probs[:, mine].sum(axis=1) * gate).max()), 3)
        return out


def shot_scores(samples: list[dict], cuts: list[float], duration: float) -> list[dict]:
    """Average each category over the scene-checked samples of each camera shot."""
    bounds = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
    shots = [{"start": a, "end": b, "n": 0, "sum": dict.fromkeys(CATEGORIES, 0.0), "peak": dict.fromkeys(CATEGORIES, 0.0)}
             for a, b in zip(bounds, bounds[1:]) if b > a]
    starts = [s["start"] for s in shots]
    for s in samples:
        if "scene" not in s:
            continue
        shot = shots[max(0, bisect.bisect_right(starts, s["t"]) - 1)]
        shot["n"] += 1
        for cat, p in s["scene"].items():
            shot["sum"][cat] += p
            shot["peak"][cat] = max(shot["peak"][cat], p)
    out = []
    for s in shots:
        n = max(s["n"], 1)
        out.append({"start": round(s["start"], 3), "end": round(s["end"], 3), "samples": s["n"],
                    "score": {c: round(s["sum"][c] / n, 3) for c in CATEGORIES},
                    "peak": {c: round(s["peak"][c], 3) for c in CATEGORIES}})
    return out


def find_scenes(shots: list[dict], category: str) -> list[dict]:
    """Runs of shots that look like `category`.

    Returns [{start, end, score, sure}]: `sure` scenes have at least one shot above COVER_SCORE;
    the rest only reached REVIEW_SCORE and are offered in review.
    """
    scenes: list[dict] = []
    for s in shots:
        p = s["score"][category]
        if p < REVIEW_SCORE[category] or not s["samples"]:
            continue  # unflagged shots shorter than SCENE_GAP between flagged ones still join the scene
        if scenes and s["start"] - scenes[-1]["end"] <= SCENE_GAP:
            sc = scenes[-1]
            sc["end"] = s["end"]
            sc["score"] = max(sc["score"], p)
            sc["shots"] += 1
            sc["strong"] += s["samples"] if p >= COVER_SCORE[category] else 0
        else:
            scenes.append({"start": s["start"], "end": s["end"], "score": p, "shots": 1,
                           "strong": s["samples"] if p >= COVER_SCORE[category] else 0})
    for sc in scenes:
        # Covered without asking only if clearly seen in more than one checked frame.
        sc["sure"] = sc["strong"] >= 2
    return scenes


# Sounds, not dialogue: a scene whose transcript is only these has nothing worth keeping.
FILLER = {"oh", "ah", "ahh", "aah", "mm", "mmm", "mhm", "hmm", "uh", "um", "huh", "ooh", "ow", "yes", "yeah",
          "no", "god", "please", "baby", "okay", "ok", "wow", "hey", "shh"}
DIALOGUE_WORDS = 6   # a sex scene with at least this many real words is blacked out, not skipped
CUT_MARGIN = 0.05


def scene_events(shots: list[dict], categories: set[str], words: list[dict] | None,
                 duration: float) -> list[dict]:
    """Plan events for flagged scenes.

    Sex scenes with no meaningful dialogue are skipped (cut out); with dialogue, or when there
    is no transcript, they are blacked out so the conversation stays. Kissing and stripper
    scenes are blacked out. Scenes that only reached REVIEW_SCORE are listed but not covered.
    """
    events = []
    for cat in sorted(categories & set(CATEGORIES)):
        for sc in find_scenes(shots, cat):
            start = round(max(0.0, sc["start"] - CUT_MARGIN), 3)
            end = round(min(duration, sc["end"]), 3)
            ev = {"type": "visual", "groups": [cat], "start": start, "end": end,
                  "confidence": sc["score"], "unsure": [],
                  "covers": [{"start": start, "end": end, "box": None}]}
            if words is not None:
                ev["dialogue_words"] = sum(1 for w in words if start <= w["start"] < end
                                           and w["word"].strip(",.!?'\"-").lower() not in FILLER)
            if not sc["sure"]:
                ev["action"] = "ignore"
                ev["unsure"].append(f"possible {cat} scene - not covered")
            elif cat == "sex" and words is not None and ev["dialogue_words"] < DIALOGUE_WORDS:
                ev["action"] = "skip"
            else:
                ev["action"] = "blackout"
                if sc["score"] < COVER_SCORE[cat] + 0.15:
                    ev["unsure"].append(f"low confidence ({sc['score']:.0%})")
            events.append(ev)
    return events
