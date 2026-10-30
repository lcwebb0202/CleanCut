"""Analyze a movie into a plan (what to cover), which render() then applies."""
import json
import os
from datetime import datetime

from . import language, nudity, scenes
from .media import extract_speech_audio, probe

LANGUAGE_FILTERS = set(language.CATEGORIES)
SCENE_FILTERS = set(scenes.CATEGORIES)  # sex, suggestive
VISUAL_FILTERS = {"nudity"} | SCENE_FILTERS
# Older names, still accepted: they are all part of "Sexually suggestive" now.
FILTER_ALIASES = {"underwear": "suggestive", "kissing": "suggestive", "strippers": "suggestive"}
ALL_FILTERS = VISUAL_FILTERS | LANGUAGE_FILTERS | set(FILTER_ALIASES)

# Body-part boxes (NudeNet) need backing from the "is this sexual" check (scenes.nsfw_near),
# unless NudeNet is very sure on its own. Tuned on Deadpool: most shaky detections were hands,
# faces and fire, in frames the sexual check scored near zero.
BOX_ALONE = 0.65        # NudeNet score that is covered with no backing
BOX_BACKED = 0.50       # ...or this score with NSFW backing of at least BACKING
BACKING = 0.50
REVIEW_BACKING = 0.30   # weaker ones are listed in review only with at least this backing
REVIEW_SCORE = 0.35     # ...and a NudeNet score of at least this


def expand_filters(filters: set[str]) -> set[str]:
    return {FILTER_ALIASES.get(f, f) for f in filters}


def analyze(input_path: str, workdir: str, filters: set[str], audio: str = "mute", cover: str = "box",
            sample_fps: float = 4.0, whisper_model: str | None = None, test_faces: bool = False,
            log=print) -> dict:
    os.makedirs(workdir, exist_ok=True)
    filters = expand_filters(filters)
    info = probe(input_path)
    log(f"Analyzing {os.path.basename(input_path)}: {info.width}x{info.height}, "
        f"{info.duration / 60:.1f} min, {info.pix_fmt}")
    plan = {
        "version": 1,
        "input": os.path.abspath(input_path),
        "created": datetime.now().isoformat(timespec="seconds"),
        "settings": {"filters": sorted(filters), "audio": audio, "cover": cover, "sample_fps": sample_fps},
        "events": [],
        "tracks": [],
    }

    lang = filters & LANGUAGE_FILTERS
    words = None
    # The transcript is also needed to tell whether a sex scene has dialogue worth keeping.
    if (lang or "sex" in filters) and info.audio_index is not None:
        log("Step 1/2: listening to speech")
        wav = os.path.join(workdir, "speech.wav")
        extract_speech_audio(info, wav)
        words = language.transcribe(wav, whisper_model, log=log)
        os.remove(wav)
        for w in words:  # the WAV starts at 0; shift to the file's timeline
            w["start"] = round(w["start"] + info.audio_offset, 3)
            w["end"] = round(w["end"] + info.audio_offset, 3)
        with open(os.path.join(workdir, "transcript.json"), "w") as f:
            json.dump(words, f)
        hits = language.find_hits(words, lang) if lang else []
        log(f"  found {len(hits)} words to censor")
        plan["events"] += hits

    groups = filters & {"nudity"} | ({"faces"} if test_faces else set())
    scene_cats = filters & SCENE_FILTERS
    if groups or scene_cats:
        log("Step 2/2: scanning video")
        hdr = info.color.get("color_transfer") in nudity.HDR_TRANSFERS
        samples, cuts = nudity.detect(input_path, info.width, info.height, info.duration, groups, sample_fps,
                                      hdr=hdr, scenes=True, log=log)
        for s in samples:  # sample times count from the first frame; cut times are already absolute
            s["t"] = round(s["t"] + info.video_offset, 3)
        tracks = nudity.build_tracks(samples)
        plan["tracks"] = tracks
        plan["cuts"] = cuts
        visual = nudity.events_from_tracks(tracks, cuts, info.duration)
        with open(os.path.join(workdir, "signals.json"), "w") as f:  # for checking results later
            json.dump([[s["t"], s["scene"]["nsfw"], s["scene"]["nsfw_sex"], s["scene"]["nsfw_sugg"]]
                       for s in samples if "scene" in s], f)
        visual = [ev for ev in visual if _check_body_event(ev, samples)]
        found = scenes.scene_events(samples, cuts, scene_cats, words, info.duration)
        for ev in found:
            how = {"skip": "cut out (no dialogue)", "blackout": "blacked out", "ignore": "left for review"}
            log(f"  {ev['groups'][0]} scene {ev['start']:.1f}-{ev['end']:.1f}s, "
                f"{ev['confidence']:.0%}: {how[ev['action']]}")
        visual = _tidy(visual + found, words)
        covered = [e for e in visual if e["action"] != "ignore"]
        blackouts = sum(c["box"] is None for e in covered for c in e["covers"])
        log(f"  found {len(covered)} moments to cover ({blackouts} full-screen), "
            f"{len(visual) - len(covered)} possible moments left for review")
        plan["events"] += visual

    plan["events"].sort(key=lambda e: e["start"])
    for i, ev in enumerate(plan["events"], 1):
        ev["id"] = i
    save_plan(plan, os.path.join(workdir, "plan.json"))
    return plan


def _check_body_event(ev: dict, samples: list[dict]) -> bool:
    """Decide a body-part moment by NudeNet's score and the sexual check around it.
    Returns False to drop it altogether (too weak and unbacked to be worth a look)."""
    if "faces" in ev["groups"]:
        return True
    backing = scenes.nsfw_near(samples, ev["start"], ev["end"])
    ev["backing"] = round(backing, 3)
    score = ev["confidence"]
    if score >= BOX_ALONE or (score >= BOX_BACKED and backing >= BACKING):
        ev["action"] = "auto"
        if backing < BACKING:
            ev["unsure"].append("scene doesn't look sexual - maybe a false alarm")
        return True
    if score >= REVIEW_SCORE and backing >= REVIEW_BACKING:
        ev["action"] = "ignore"
        if "possible - not covered" not in ev["unsure"]:
            ev["unsure"] = ["possible - not covered"] + [u for u in ev["unsure"] if u != "very brief"]
        return True
    return False


def _overlaps(a: dict, b: dict, slack: float = 0.0) -> bool:
    return a["start"] < b["end"] + slack and b["start"] < a["end"] + slack


def _tidy(events: list[dict], words: list[dict] | None) -> list[dict]:
    """One review item per moment: overlapping covered scenes (e.g. a strip club that looks both
    "sex" and "suggestive") merge into one; anything already inside a covered scene is dropped."""
    covered = sorted((e for e in events if "covers" in e and e["action"] in ("blackout", "skip")
                      and set(e["groups"]) & SCENE_FILTERS), key=lambda e: e["start"])
    merged: list[dict] = []
    for ev in covered:
        if merged and _overlaps(merged[-1], ev, slack=1.0):
            m = merged[-1]
            m["start"], m["end"] = min(m["start"], ev["start"]), max(m["end"], ev["end"])
            m["groups"] = sorted(set(m["groups"]) | set(ev["groups"]), key=["sex", "suggestive"].index)
            m["confidence"] = max(m["confidence"], ev["confidence"])
            m["action"] = "skip" if m["action"] == ev["action"] == "skip" else "blackout"
            m["covers"] = [{"start": m["start"], "end": m["end"], "box": None}]
        else:
            merged.append(dict(ev))
    for m in merged:
        if words is not None:
            m["dialogue_words"] = sum(1 for w in words if m["start"] <= w["start"] < m["end"]
                                      and w["word"].strip(",.!?'\"-").lower() not in scenes.FILLER)
            if m["action"] == "skip" and m["dialogue_words"] >= scenes.DIALOGUE_WORDS:
                m["action"] = "blackout"
    rest = [e for e in events if e not in covered
            and not any(m["start"] <= e["start"] and e["end"] <= m["end"] for m in merged)
            and not (e["action"] == "ignore" and set(e["groups"]) & SCENE_FILTERS and any(_overlaps(m, e) for m in merged))]
    return merged + rest


def save_plan(plan: dict, path: str) -> None:
    clean = {**plan, "events": [{k: v for k, v in e.items() if not k.startswith("_")} for e in plan["events"]]}
    with open(path, "w") as f:
        json.dump(clean, f, indent=1)


def load_plan(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:  # tolerate a BOM from Windows editors
        return json.load(f)


def output_name(input_path: str) -> str:
    """'Movie (2010).mkv' -> 'Movie (2010) - Filtered.mkv' (Jellyfin shows it as a version)."""
    stem = os.path.splitext(os.path.basename(input_path))[0]
    for suffix in (" - Original", " - original"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return f"{stem} - Filtered.mkv"
