"""Analyze a movie into a plan (what to cover), which render() then applies."""
import json
import os
from datetime import datetime

from . import language, nudity, scenes
from .media import extract_speech_audio, probe

LANGUAGE_FILTERS = set(language.CATEGORIES)
SCENE_FILTERS = set(scenes.CATEGORIES)  # sex, kissing, strippers
VISUAL_FILTERS = {"nudity", "underwear"} | SCENE_FILTERS
# "Sexually suggestive" is one checkbox covering underwear, intense kissing and strippers.
GROUPED_FILTERS = {"suggestive": {"underwear", "kissing", "strippers"}}
ALL_FILTERS = VISUAL_FILTERS | LANGUAGE_FILTERS | set(GROUPED_FILTERS)


def expand_filters(filters: set[str]) -> set[str]:
    out = set(filters)
    for name, members in GROUPED_FILTERS.items():
        if name in out:
            out = (out - {name}) | members
    return out


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

    groups = filters & {"nudity", "underwear"} | ({"faces"} if test_faces else set())
    scene_cats = filters & SCENE_FILTERS
    if groups or scene_cats:
        log("Step 2/2: scanning video")
        hdr = info.color.get("color_transfer") in nudity.HDR_TRANSFERS
        samples, cuts = nudity.detect(input_path, info.width, info.height, info.duration, groups, sample_fps,
                                      hdr=hdr, scenes=bool(scene_cats), log=log)
        for s in samples:  # sample times count from the first frame; cut times are already absolute
            s["t"] = round(s["t"] + info.video_offset, 3)
        tracks = nudity.build_tracks(samples)
        plan["tracks"] = tracks
        plan["cuts"] = cuts
        visual = nudity.events_from_tracks(tracks, cuts, info.duration)
        if scene_cats:
            shots = scenes.shot_scores(samples, cuts, info.duration)
            with open(os.path.join(workdir, "shots.json"), "w") as f:
                json.dump(shots, f, indent=0)
            if "sex" in scene_cats:
                # A shaky body-part detection in a shot the scene check finds ordinary is often
                # a false alarm (a hand by a face). Still covered, but flagged for review.
                for ev in visual:
                    if ev["action"] == "auto" and "nudity" in ev["groups"] and ev["confidence"] < nudity.SURE_SCORE:
                        near = [s["score"]["sex"] for s in shots if s["start"] < ev["end"] and s["end"] > ev["start"]]
                        if max(near, default=0) < scenes.REVIEW_SCORE["sex"]:
                            ev["unsure"].append("scene looks normal - maybe a false alarm")
            found = scenes.scene_events(shots, scene_cats, words, info.duration)
            for ev in found:
                how = {"skip": "cut out (no dialogue)", "blackout": "blacked out", "ignore": "left for review"}
                log(f"  {ev['groups'][0]} scene {ev['start']:.1f}-{ev['end']:.1f}s, "
                    f"{ev['confidence']:.0%}: {how[ev['action']]}")
            visual += found
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
