"""The browser app: pick a movie, choose filters, watch progress, review, save.

One movie at a time. The heavy work runs as a separate `python -m cleancut` process so it can
be cancelled cleanly (it and its ffmpeg children are killed together) and so the GPU memory is
freed between runs. Its log lines drive the progress bar.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import language, scenes
from .media import probe
from .nudity import HDR_TRANSFERS, TONEMAP
from .pipeline import ALL_FILTERS, CLEAN_SUFFIX, jellyfin_note, load_plan, output_name, save_plan

MEDIA_ROOT = os.path.realpath(os.environ.get("MEDIA_ROOT", "/media"))
UPLOAD_DIR = os.path.join(MEDIA_ROOT, "CleanCut Uploads")
# CleanCut's own working files (plans, transcripts, preview frames) live here, not in the user's folders.
WORK_ROOT = os.environ.get("WORK_ROOT", "/work")
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv"}
STATIC = os.path.join(os.path.dirname(__file__), "static")
JOB_FILE = os.path.join(WORK_ROOT, "job.json")  # so a review survives a restart

# Thumbnail widths in pixels before being blown back up: level 0 is the heaviest blur.
BLUR_LEVELS = [12, 18, 28, 44, 72]
THUMB_WIDTH = 320

app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- paths

# The folders shared with CleanCut. The movie folder is always there; a separate save folder
# (OUTPUT_DIR in .env) is added when set. The browser sees paths as "<folder name>/sub/file.mkv".

def _roots() -> list[dict]:
    roots = []
    for container, host_env in ((MEDIA_ROOT, "HOST_MEDIA_DIR"), (os.environ.get("OUTPUT_ROOT", "/output"), "HOST_OUTPUT_DIR")):
        host = os.environ.get(host_env, "").rstrip("/\\")
        if not os.path.isdir(container) or any(r["host"] == host and host for r in roots):
            continue
        name = os.path.basename(host) or os.path.basename(container)
        while any(r["name"] == name for r in roots):
            name += " (2)"
        roots.append({"name": name, "path": os.path.realpath(container), "host": host})
    return roots


ROOTS = _roots()
OUTPUT_HOST = os.environ.get("HOST_OUTPUT_DIR", "").rstrip("/\\")
DEFAULT_OUT = next((r for r in ROOTS if r["host"] == OUTPUT_HOST and r["path"] != MEDIA_ROOT), None)


def _real(vpath: str) -> str:
    """Browser path ("<folder name>/sub/x.mkv") -> real path, refusing anything outside the shared folders."""
    parts = vpath.replace("\\", "/").strip("/").split("/", 1)
    root = next((r for r in ROOTS if r["name"] == parts[0]), None)
    if root is None:
        raise HTTPException(400, "That folder isn't one CleanCut can see.")
    path = os.path.realpath(os.path.join(root["path"], parts[1] if len(parts) > 1 else ""))
    if path != root["path"] and not path.startswith(root["path"] + os.sep):
        raise HTTPException(400, "That folder isn't one CleanCut can see.")
    return path


def _root_of(path: str) -> dict:
    return next(r for r in ROOTS if path == r["path"] or path.startswith(r["path"] + os.sep))


def _rel(path: str) -> str:
    root = _root_of(path)
    rest = os.path.relpath(path, root["path"]).replace(os.sep, "/")
    return root["name"] + ("" if rest == "." else "/" + rest)


def _show(path: str) -> str:
    """How a path looks on the user's computer (e.g. C:/Users/.../Movies/x.mkv)."""
    try:
        root = _root_of(path)
    except StopIteration:
        return path
    rest = os.path.relpath(path, root["path"]).replace(os.sep, "/")
    return (root["host"] or root["path"]) + ("" if rest == "." else "/" + rest)


# ---------------------------------------------------------------- the job

class Job:
    def __init__(self, input_path: str, out_dir: str, filters: list[str], cover: str, audio: str, review: bool):
        self.input = input_path
        self.out_dir = out_dir
        self.filters = filters
        self.cover = cover
        self.audio = audio
        self.review = review
        stem = os.path.splitext(os.path.basename(input_path))[0]
        self.workdir = os.path.join(WORK_ROOT, stem)
        self.output = os.path.join(out_dir, output_name(input_path))
        self.state = "analyzing"  # analyzing, review, rendering, done, error, cancelled
        self.stage = "Starting"
        self.pct = 0.0
        self.log: list[str] = []
        self.error = ""
        self.started = time.time()
        self.stage_started = time.time()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False

    SAVED = ("input", "out_dir", "filters", "cover", "audio", "review", "workdir", "output", "state", "stage",
             "pct", "log", "error", "started")

    def save(self) -> None:
        try:
            with open(JOB_FILE, "w") as f:
                json.dump({k: getattr(self, k) for k in self.SAVED}, f)
        except OSError:
            pass

    @classmethod
    def restore(cls):
        try:
            with open(JOB_FILE) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        job = cls.__new__(cls)
        cls.__init__(job, d["input"], d["out_dir"], d["filters"], d["cover"], d["audio"], d["review"])
        for k in cls.SAVED:
            setattr(job, k, d[k])
        if job.state in ("analyzing", "rendering"):
            job.state, job.stage = "error", "Interrupted"
            job.error = "CleanCut was restarted (or the computer slept or shut down) while it was working. Please start again."
        return job

    def public(self) -> dict:
        return {"state": self.state, "stage": self.stage, "pct": round(self.pct, 1),
                "log": self.log[-60:], "error": self.error, "input": _show(self.input),
                "input_name": os.path.basename(self.input), "output": _show(self.output),
                "out_dir": _show(self.out_dir), "filters": self.filters, "cover": self.cover,
                "audio": self.audio, "review": self.review,
                "elapsed": round(time.time() - self.started), "stage_elapsed": round(time.time() - self.stage_started)}

    # Progress lines from the CLI, e.g. "  scanned video  45.0%".
    STAGES = [("Step 1/2", "Listening to speech"), ("Step 2/2", "Scanning video"), ("Rendering", "Saving the filtered movie")]
    PROGRESS = re.compile(r"(transcribed|scanned video|rendered)\s+([\d.]+)%")

    def _run(self, args: list[str]) -> None:
        self.proc = subprocess.Popen([sys.executable, "-u", "-m", "cleancut", *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line or "onnxruntime" in line or "Memcpy" in line or "session_options" in line:
                continue
            m = self.PROGRESS.search(line)
            if m:
                self.pct = min(100.0, float(m.group(2)))
                continue
            for key, stage in self.STAGES:
                if line.startswith(key):
                    self.stage, self.pct, self.stage_started = stage, 0.0, time.time()
            self.log.append(line.strip())
        self.proc.wait()
        if self.cancelled:
            raise RuntimeError("cancelled")
        if self.proc.returncode != 0:
            tail = [l for l in self.log[-15:] if l]
            raise RuntimeError(tail[-1] if tail else f"stopped with code {self.proc.returncode}")

    def analyze(self) -> None:
        try:
            self._run(["analyze", self.input, "--workdir", self.workdir, "--filters", ",".join(self.filters),
                       "--audio", self.audio, "--cover", self.cover])
            if self.review:
                self.state, self.stage, self.pct = "review", "Ready for review", 100.0
                self.save()
            else:
                self.render()
        except Exception as e:  # noqa: BLE001 - shown to the user
            self._fail(e)

    def render(self) -> None:
        try:
            self.state, self.stage, self.pct, self.stage_started = "rendering", "Saving the filtered movie", 0.0, time.time()
            self.save()
            self._run(["render", self.plan_path, "--out", self.out_dir])
            self.state, self.stage, self.pct = "done", "Finished", 100.0
            self.save()
        except Exception as e:  # noqa: BLE001
            self._fail(e)

    def _fail(self, e: Exception) -> None:
        if self.cancelled:
            self.state, self.stage = "cancelled", "Cancelled"
        else:
            self.state, self.stage, self.error = "error", "Something went wrong", str(e)
        self.save()

    def cancel(self) -> None:
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)

    @property
    def plan_path(self) -> str:
        return os.path.join(self.workdir, "plan.json")


JOB: Job | None = Job.restore()
LOCK = threading.Lock()


def _job() -> Job:
    if JOB is None:
        raise HTTPException(404, "No movie is being processed.")
    return JOB


# ---------------------------------------------------------------- pages and settings

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def state():
    return {"job": JOB.public() if JOB else None, "media_root": _show(MEDIA_ROOT),
            "default_out": {"path": DEFAULT_OUT["name"], "shown": DEFAULT_OUT["host"] or DEFAULT_OUT["path"]} if DEFAULT_OUT else None,
            "filters": sorted(ALL_FILTERS), "blur_levels": len(BLUR_LEVELS)}


# ---------------------------------------------------------------- choosing files

@app.get("/api/browse")
def browse(path: str = ""):
    if not path.strip("/") and len(ROOTS) > 1:
        # Top level: the shared folders themselves.
        return {"path": "", "shown": "Your CleanCut folders", "parent": None,
                "dirs": [r["name"] for r in ROOTS], "files": []}
    real = _real(path or ROOTS[0]["name"])
    if not os.path.isdir(real):
        raise HTTPException(404, "Folder not found.")
    dirs, files = [], []
    for e in sorted(os.scandir(real), key=lambda e: e.name.lower()):
        if e.name.startswith("."):
            continue
        if e.is_dir():
            dirs.append(e.name)
        elif os.path.splitext(e.name)[1].lower() in VIDEO_EXTS and not e.name.endswith((CLEAN_SUFFIX + ".mkv", " - Filtered.mkv")):
            files.append({"name": e.name, "size": e.stat().st_size})
    at_root = real == _root_of(real)["path"]
    parent = ("" if len(ROOTS) > 1 else None) if at_root else _rel(os.path.dirname(real))
    return {"path": _rel(real), "shown": _show(real), "parent": parent, "dirs": dirs, "files": files}


@app.get("/api/output")
def output_info(input: str, out_dir: str):
    """The file name the clean copy will get, and a note if Jellyfin wouldn't group it with the original."""
    path, out = _real(input), _real(out_dir)
    folder = os.path.basename(_show(os.path.dirname(path)).rstrip("/\\"))  # real name, not "media"
    return {"name": output_name(path), "note": jellyfin_note(path, out, folder)}


@app.post("/api/find")
async def find(req: Request):
    """A file was dropped on the page. Browsers don't tell us where it lives, so look for a file
    with the same name and size in the shared folders; that saves copying a huge movie."""
    body = await req.json()
    name, size = body.get("name", ""), body.get("size", -1)
    for r in ROOTS:
        for root, dirs, files in os.walk(r["path"]):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if name in files:
                path = os.path.join(root, name)
                if os.path.getsize(path) == size:
                    return {"path": _rel(path), "shown": _show(path), "dir": _rel(root)}
    raise HTTPException(404, "Not in the shared folders.")


@app.put("/api/upload")
async def upload(req: Request, name: str):
    """Copy a dropped file into the media folder (only for files that aren't there already)."""
    name = os.path.basename(name)
    if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
        raise HTTPException(400, "That doesn't look like a movie file.")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dest = os.path.join(UPLOAD_DIR, name)
    part = dest + ".part"
    with open(part, "wb") as f:
        async for chunk in req.stream():
            f.write(chunk)
    os.replace(part, dest)
    return {"path": _rel(dest), "shown": _show(dest), "dir": _rel(UPLOAD_DIR)}


@app.post("/api/folder")
async def make_folder(req: Request):
    body = await req.json()
    name = re.sub(r'[<>:"/\\|?*]', "", body.get("name", "")).strip()
    if not name:
        raise HTTPException(400, "Please type a folder name.")
    path = os.path.join(_real(body.get("path", "")), name)
    os.makedirs(path, exist_ok=True)
    return {"path": _rel(path)}


# ---------------------------------------------------------------- running

@app.post("/api/start")
async def start(req: Request):
    global JOB
    body = await req.json()
    with LOCK:
        if JOB and JOB.state in ("analyzing", "rendering"):
            raise HTTPException(409, "A movie is already being processed.")
        input_path = _real(body["input"])
        out_dir = _real(body.get("out_dir", ""))
        if not os.path.isfile(input_path):
            raise HTTPException(400, "Movie file not found.")
        filters = [f for f in body.get("filters", []) if f in ALL_FILTERS]
        if not filters:
            raise HTTPException(400, "Tick at least one filter.")
        if os.path.abspath(os.path.join(out_dir, output_name(input_path))) == os.path.abspath(input_path):
            raise HTTPException(400, "The output would replace the movie itself. Pick another folder.")
        JOB = Job(input_path, out_dir, filters, body.get("cover", "box"), body.get("audio", "mute"),
                  bool(body.get("review", True)))
        JOB.save()
        threading.Thread(target=JOB.analyze, daemon=True).start()
    return JOB.public()


@app.post("/api/cancel")
def cancel():
    _job().cancel()
    return {"ok": True}


@app.post("/api/reset")
def reset():
    global JOB
    with LOCK:
        if JOB and JOB.state in ("analyzing", "rendering"):
            raise HTTPException(409, "Cancel the current movie first.")
        JOB = None
        try:
            os.remove(JOB_FILE)
        except OSError:
            pass
    return {"ok": True}


# ---------------------------------------------------------------- review

LABELS = {"nudity": "Nudity", "sex": "Sex scene", "suggestive": "Sexually suggestive", "faces": "Face (test)",
          # from plans made by older versions
          "underwear": "Underwear or swimsuit", "kissing": "Intense kissing", "strippers": "Stripper"}
SCENE_GROUPS = set(scenes.CATEGORIES) | {"kissing", "strippers"}


def _shown_cover(ev: dict) -> dict:
    """The cover a thumbnail shows: the longest one."""
    return max(ev.get("covers") or [{"start": ev["start"], "end": ev["end"], "box": None}],
               key=lambda c: c["end"] - c["start"])


@app.get("/api/plan")
def plan():
    job = _job()
    if job.state not in ("review", "rendering", "done"):
        raise HTTPException(409, "Not ready for review yet.")
    p = load_plan(job.plan_path)
    visual, words = [], []
    for ev in p["events"]:
        if ev["type"] == "visual":
            scene = bool(set(ev["groups"]) & SCENE_GROUPS)
            c = _shown_cover(ev)
            visual.append({"id": ev["id"], "label": " + ".join(LABELS.get(g, g) for g in ev["groups"]),
                           "scene": scene, "start": ev["start"], "end": ev["end"], "action": ev["action"],
                           "unsure": ev.get("unsure", []), "confidence": ev.get("confidence"),
                           "dialogue_words": ev.get("dialogue_words"), "box": c["box"],
                           "full": any(cv["box"] is None for cv in ev.get("covers", [])),
                           "boxes": len(ev.get("covers", []))})
        elif ev["type"] == "language":
            words.append({"id": ev["id"], "word": ev["word"], "category": ev["category"],
                          "start": ev["start"], "action": ev["action"]})
    return {"visual": visual, "words": words, "settings": p["settings"],
            "categories": {c: c for c in language.CATEGORIES}}


@app.post("/api/plan")
async def update_plan(req: Request):
    """Save review choices: {"actions": {id: action}, "cover": ..., "audio": ...}."""
    job = _job()
    if job.state != "review":
        raise HTTPException(409, "Not in review.")
    body = await req.json()
    p = load_plan(job.plan_path)
    actions = {int(k): v for k, v in body.get("actions", {}).items()}
    allowed = {"visual": {"auto", "box", "blackout", "skip", "ignore"}, "language": {"censor", "ignore"}}
    for ev in p["events"]:
        a = actions.get(ev["id"])
        if a in allowed.get(ev["type"], ()):
            ev["action"] = a
    for key, options in (("cover", {"box", "blur"}), ("audio", {"mute", "bleep"})):
        if body.get(key) in options:
            p["settings"][key] = body[key]
            setattr(job, key, body[key])
    save_plan(p, job.plan_path)
    job.save()
    return {"ok": True}


@app.post("/api/render")
def render():
    job = _job()
    with LOCK:
        if job.state != "review":
            raise HTTPException(409, "Not in review.")
        job.state = "rendering"
        threading.Thread(target=job.render, daemon=True).start()
    return job.public()


@app.get("/api/thumb/{event_id}")
def thumb(event_id: int, level: int = 0):
    """A HEAVILY blurred still of a flagged moment. The sharp frame never leaves the server."""
    job = _job()
    ev = next((e for e in load_plan(job.plan_path)["events"] if e["id"] == event_id), None)
    if ev is None or ev["type"] != "visual":
        raise HTTPException(404)
    level = max(0, min(level, len(BLUR_LEVELS) - 1))
    cache = os.path.join(job.workdir, "thumbs", f"{event_id}.npy")
    if os.path.exists(cache):
        small = np.load(cache)
    else:
        c = _shown_cover(ev)
        small = _grab(job.input, (c["start"] + c["end"]) / 2)
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        np.save(cache, small)
    small = _brighten(small)
    h, w = small.shape[:2]
    bw = BLUR_LEVELS[level]
    tiny = cv2.resize(small, (bw, max(1, round(bw * h / w))), interpolation=cv2.INTER_AREA)
    img = cv2.resize(tiny, (w, h), interpolation=cv2.INTER_LINEAR)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return Response(jpg.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _brighten(img: np.ndarray) -> np.ndarray:
    """Stretch dark frames (night scenes) so shapes are visible once blurred. At most 4x."""
    lo, hi = np.percentile(img, (1, 99.5))
    gain = min(4.0, 235.0 / max(hi - lo, 1.0))
    return np.clip((img.astype(np.float32) - lo) * gain + 10, 0, 255).astype(np.uint8)


def _grab(path: str, t: float) -> np.ndarray:
    """One frame at time t, already shrunk to THUMB_WIDTH (HDR converted to normal colors)."""
    info = probe(path)
    h = int(round(info.height * THUMB_WIDTH / info.width / 2)) * 2
    vf = f"scale={THUMB_WIDTH}:{h}," + (TONEMAP if info.color.get("color_transfer") in HDR_TRANSFERS else "") + "format=bgr24"
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-map", "0:v:0",
                          "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"], capture_output=True).stdout
    if len(out) < THUMB_WIDTH * h * 3:
        return np.zeros((h, THUMB_WIDTH, 3), np.uint8)
    return np.frombuffer(out[:THUMB_WIDTH * h * 3], np.uint8).reshape(h, THUMB_WIDTH, 3)


def serve(port: int = 8800) -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
