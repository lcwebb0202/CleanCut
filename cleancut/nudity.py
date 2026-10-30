"""Find nudity in sampled frames, link detections into tracks, and turn them into covers."""
import bisect
import os
import re
import subprocess
import tempfile

import numpy as np

# NudeNet class -> CleanCut filter group.
CLASS_GROUPS = {
    "FEMALE_BREAST_EXPOSED": "nudity",
    "FEMALE_GENITALIA_EXPOSED": "nudity",
    "MALE_GENITALIA_EXPOSED": "nudity",
    "BUTTOCKS_EXPOSED": "nudity",
    "ANUS_EXPOSED": "nudity",
    # NudeNet can't tell underwear from swimwear; these go through review.
    "FEMALE_BREAST_COVERED": "underwear",
    "FEMALE_GENITALIA_COVERED": "underwear",
    "BUTTOCKS_COVERED": "underwear",
    # Only used with --test-faces, to check the pipeline on clean footage.
    "FACE_FEMALE": "faces",
    "FACE_MALE": "faces",
}
MIN_SCORE = {"nudity": 0.40, "underwear": 0.55, "faces": 0.40}  # needed to start covering
KEEP_SCORE = 0.25  # once covering, keep following it down to this score

DETECT_WIDTH = 960
MODEL_PATH = "/opt/models/640m.onnx"

MAX_GAP_SAMPLES = 3     # a track survives this many missed samples
EVENT_MERGE_GAP = 2.0

# Covers: big, early, and still. Better to cover too much and too soon than too little too late.
# Grow each detection by these multiples of its own size (left/right, up, down), so a chest
# detection covers the whole torso to the waist, and by at least these shares of the frame.
COVER_GROW_SIDES, COVER_GROW_UP, COVER_GROW_DOWN = 1.5, 1.0, 2.0
COVER_MIN_X, COVER_MIN_Y = 0.06, 0.08
BLACKOUT_AREA = 0.45    # a cover bigger than this share of the frame becomes a full blackout
CUT_MARGIN = 0.05       # start covers a frame before a cut, so rounding never shows the cut frame
PRE_ROLL = 5.0          # start covering up to this long before the first detection (stops at a cut)
MIN_PRE_ROLL = 0.75     # ...but always at least this long, in case a cut was misdetected
POST_ROLL = 2.0
MIN_POST_ROLL = 0.75
SCENE_THRESHOLD = 4     # ffmpeg scdet score (0-100) for a camera cut; dark films cut at ~5-8, noise < 2


def _load_detector(log):
    import os
    import onnxruntime as ort
    from nudenet import NudeDetector

    from .media import onnx_providers
    providers = onnx_providers()
    big = os.path.exists(MODEL_PATH)
    model = MODEL_PATH if big else os.path.join(os.path.dirname(__import__("nudenet").__file__), "320n.onnx")
    # NudeDetector ignores its providers argument, so build its session ourselves.
    det = NudeDetector.__new__(NudeDetector)
    det.onnx_session = ort.InferenceSession(model, providers=providers)
    det.input_width = det.input_height = 640 if big else 320
    det.input_name = det.onnx_session.get_inputs()[0].name
    used = det.onnx_session.get_providers()[0]
    log(f"Nudity detection on {'GPU' if used.startswith('CUDA') else 'CPU'} ({os.path.basename(model)})")
    return det


# HDR video looks dim and flat if read as normal video, and the detector misses things,
# so convert it to a normal (SDR) picture first.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,")


def sample_frames(path: str, width: int, height: int, sample_fps: float, hdr: bool = False,
                  cuts_file: str | None = None):
    """Yield (time, BGR frame) at sample_fps, scaled to DETECT_WIDTH wide.

    With cuts_file, every frame is also checked for camera cuts in the same pass
    (read them back with read_cuts)."""
    w = min(DETECT_WIDTH, width)
    h = int(round(height * w / width / 2)) * 2
    sample = f"fps={sample_fps},scale={w}:{h}," + (TONEMAP if hdr else "") + "format=bgr24"
    if cuts_file:
        graph = (f"[0:v:0]split=2[c][s];"
                 f"[c]scale=480:-2:flags=fast_bilinear,scdet=threshold={SCENE_THRESHOLD},"
                 f"metadata=mode=print:key=lavfi.scd.time:file={cuts_file},nullsink;"
                 f"[s]{sample}[out]")
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-filter_complex", graph, "-map", "[out]",
               "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    else:
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0",
               "-vf", sample, "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w * h * 3
    i = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield i / sample_fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.wait()


def read_cuts(cuts_file: str) -> list[float]:
    with open(cuts_file) as f:
        return sorted(round(float(m), 3) for m in re.findall(r"lavfi\.scd\.time=([0-9.]+)", f.read()))


def detect(path: str, width: int, height: int, duration: float, groups: set[str],
           sample_fps: float = 4.0, hdr: bool = False, scenes: bool = False,
           log=print) -> tuple[list[dict], list[float]]:
    """Return (samples, cuts).

    samples: [{t, dets: [{group, cls, score, box: [x0,y0,x1,y1] normalized}], scene?: {category: p}}]
    cuts: times of camera cuts.
    With scenes, some samples also get CLIP scene scores (2 a second on GPU, 1 on CPU).
    """
    det = _load_detector(log) if groups else None
    scene_det, scene_every = None, 0
    if scenes:
        from .scenes import SceneDetector
        scene_det = SceneDetector(log)
        scene_every = max(1, round(sample_fps / (2.0 if scene_det.gpu else 1.0)))
    if hdr:
        log("  HDR video: converting to SDR for detection")
    fd, cuts_file = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    samples = []
    last_pct = -5
    for i, (t, frame) in enumerate(sample_frames(path, width, height, sample_fps, hdr, cuts_file)):
        fh, fw = frame.shape[:2]
        dets = []
        for d in det.detect(frame) if det else []:
            group = CLASS_GROUPS.get(d["class"])
            if group in groups and d["score"] >= KEEP_SCORE:
                x, y, bw, bh = d["box"]
                dets.append({"group": group, "cls": d["class"], "score": round(float(d["score"]), 3),
                             "box": [round(x / fw, 4), round(y / fh, 4),
                                     round((x + bw) / fw, 4), round((y + bh) / fh, 4)]})
        samples.append({"t": round(t, 3), "dets": dets})
        if scene_det and i % scene_every == 0:
            samples[-1]["scene"] = scene_det.score(frame)
        pct = t / max(duration, 1) * 100
        if pct - last_pct >= 5:
            log(f"  scanned video {pct:5.1f}%")
            last_pct = pct
    cuts = read_cuts(cuts_file)
    os.remove(cuts_file)
    log(f"  found {len(cuts)} camera cuts")
    return samples, cuts


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _center_dist(a, b) -> float:
    return float(np.hypot((a[0] + a[2] - b[0] - b[2]) / 2, (a[1] + a[3] - b[1] - b[3]) / 2))


def build_tracks(samples: list[dict]) -> list[dict]:
    """Link detections in consecutive samples into tracks: [{group, keys: [[t, x0,y0,x1,y1], ...]}]."""
    tracks: list[dict] = []
    for si, s in enumerate(samples):
        live = [tr for tr in tracks if si - tr["last"] <= MAX_GAP_SAMPLES]
        taken = set()
        for d in sorted(s["dets"], key=lambda d: -d["score"]):
            best, best_score = None, 0.0
            for tr in live:
                if id(tr) in taken or tr["group"] != d["group"]:
                    continue
                prev = tr["keys"][-1][1:]
                score = _iou(prev, d["box"])
                if score < 0.05 and _center_dist(prev, d["box"]) < 0.12:
                    score = 0.05
                if score > 0 and max(tr["scores"]) >= MIN_SCORE[tr["group"]]:
                    score += 1.0  # extend a confident track before a faint one
                if score > best_score:
                    best, best_score = tr, score
            if best is None:
                best = {"group": d["group"], "keys": [], "scores": [], "last": si}
                tracks.append(best)
            taken.add(id(best))
            best["keys"].append([s["t"], *d["box"]])
            best["scores"].append(d["score"])
            best["last"] = si
    for tr in tracks:
        del tr["last"]
        # A track that never reaches MIN_SCORE is only a hint: shown in review, not covered.
        tr["weak"] = max(tr["scores"]) < MIN_SCORE[tr["group"]]
    return tracks


def big_box(box) -> list[float]:
    """Grow a detection to cover much more of the body, mostly sideways and downward."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    px = max(w * COVER_GROW_SIDES, COVER_MIN_X)
    up, down = max(h * COVER_GROW_UP, COVER_MIN_Y), max(h * COVER_GROW_DOWN, COVER_MIN_Y)
    return [max(0.0, x0 - px), max(0.0, y0 - up), min(1.0, x1 + px), min(1.0, y1 + down)]


def _shot_bounds(cuts: list[float], t: float, duration: float) -> tuple[float, float]:
    i = bisect.bisect_right(cuts, t)
    return (cuts[i - 1] if i > 0 else 0.0), (cuts[i] if i < len(cuts) else duration)


def build_covers(tracks: list[dict], cuts: list[float], duration: float) -> list[dict]:
    """One big still box per shot, from shortly before the first detection (back to the
    shot's start, up to PRE_ROLL) until after the last. Too big a box = blackout (box None)."""
    by_shot: dict[tuple, list] = {}
    for tr in tracks:
        for t, *box in tr["keys"]:
            by_shot.setdefault(_shot_bounds(cuts, t, duration), []).append((t, box))
    covers = []
    for (s0, s1), hits in sorted(by_shot.items()):
        times = [t for t, _ in hits]
        first, last = min(times), max(times)
        # Early rather than late: back to the shot start (within PRE_ROLL), and never less
        # than MIN_PRE_ROLL even if the cut detection is wrong.
        start = min(max(s0 - CUT_MARGIN, first - PRE_ROLL), first - MIN_PRE_ROLL)
        end = max(min(s1, last + POST_ROLL), last + MIN_POST_ROLL)
        boxes = [big_box(b) for _, b in hits]
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        area = (box[2] - box[0]) * (box[3] - box[1])
        covers.append({"start": round(max(0.0, start), 3), "end": round(min(duration, end), 3),
                       "box": None if area > BLACKOUT_AREA else [round(v, 4) for v in box]})
    return covers


SURE_SCORE = 0.60    # below this the detector is guessing
SURE_SAMPLES = 3     # seen in fewer samples than this = maybe a one-frame false alarm


def _unsure_reasons(tracks: list[dict]) -> list[str]:
    reasons = []
    groups = {tr["group"] for tr in tracks}
    if "underwear" in groups and "nudity" not in groups:
        reasons.append("underwear or swimsuit?")
    best = max(max(tr["scores"]) for tr in tracks)
    if "nudity" in groups and best < SURE_SCORE:
        reasons.append(f"low confidence ({best:.0%})")
    if sum(len(tr["keys"]) for tr in tracks) < SURE_SAMPLES:
        reasons.append("very brief")
    return reasons


def events_from_tracks(tracks: list[dict], cuts: list[float], duration: float) -> list[dict]:
    """Group tracks into reviewable moments, each with its covers, flagging the ones worth a look.

    Weak tracks become their own moment that is NOT covered by default
    ("possible - not covered"), so review can switch it on. Weak tracks inside a
    covered moment are folded into it, since they are probably real.
    """
    strong_tracks = [tr for tr in tracks if not tr["weak"]]
    strong = _group_tracks(strong_tracks, "auto")
    weak = []
    for ev, trs in _group_tracks([tr for tr in tracks if tr["weak"]], "ignore", members=True):
        host = next((s for s in strong if ev["start"] < s["end"] + EVENT_MERGE_GAP
                     and s["start"] - EVENT_MERGE_GAP < ev["end"]), None)
        if host:
            host["_tracks"] += trs
        else:
            ev["unsure"] = ["possible - not covered"]
            ev["_tracks"] = trs
            weak.append(ev)
    events = sorted(strong + weak, key=lambda e: e["start"])
    for ev in events:
        ev["covers"] = build_covers(ev.pop("_tracks"), cuts, duration)
        ev["start"] = min(c["start"] for c in ev["covers"])
        ev["end"] = max(c["end"] for c in ev["covers"])
    return events


def _group_tracks(tracks: list[dict], action: str, members: bool = False):
    events: list[dict] = []
    groups: list[list[dict]] = []
    for tr in sorted(tracks, key=lambda tr: tr["keys"][0][0]):
        a, b = tr["keys"][0][0], tr["keys"][-1][0]
        if events and a - events[-1]["end"] <= EVENT_MERGE_GAP:
            ev = events[-1]
            ev["end"] = max(ev["end"], b)
            if tr["group"] not in ev["groups"]:
                ev["groups"].append(tr["group"])
            groups[-1].append(tr)
        else:
            events.append({"type": "visual", "groups": [tr["group"]], "start": a, "end": b, "action": action})
            groups.append([tr])
    for ev, trs in zip(events, groups):
        ev["confidence"] = round(max(max(tr["scores"]) for tr in trs), 3)
        ev["unsure"] = _unsure_reasons(trs)
        ev["_tracks"] = list(trs)
    return list(zip(events, groups)) if members else events
