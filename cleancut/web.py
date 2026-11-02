"""The browser app: pick movies, choose filters, watch progress, review, save.

Movies wait in a queue and are worked on one at a time. The heavy work runs as a separate `python -m cleancut` process so it can
be cancelled cleanly (it and its ffmpeg children are killed together) and so the GPU memory is
freed between runs. Its log lines drive the progress bar.
"""
import json
import os
import re
import shutil
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
from .pipeline import ALL_FILTERS, OUTPUT_SUFFIX, jellyfin_note, load_plan, output_name, save_plan

MEDIA_ROOT = os.path.realpath(os.environ.get("MEDIA_ROOT", "/media"))
UPLOAD_DIR = os.path.join(MEDIA_ROOT, "CleanCut Uploads")
# CleanCut's own working files (plans, transcripts, preview frames) live here, not in the user's folders.
WORK_ROOT = os.environ.get("WORK_ROOT", "/work")
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".wmv"}
STATIC = os.path.join(os.path.dirname(__file__), "static")
QUEUE_FILE = os.path.join(WORK_ROOT, "queue.json")  # so the list (and reviews) survive a restart
OLD_JOB_FILE = os.path.join(WORK_ROOT, "job.json")  # the single movie of versions before the queue

# Thumbnail widths in pixels before being blown back up: level 0 is the heaviest blur. The last
# two are only lightly blurred, so whatever the detector found is always painted black (HIDE_GROW).
BLUR_LEVELS = [12, 18, 28, 44, 72, 110, 160]
LIGHT_BLUR = 5      # levels from here on are "light": the page warns that things can slip through
HIDE_GROW = 0.6     # black patches are this much bigger than the detection on every side
THUMB_WIDTH = 320

class _FreshStatic(StaticFiles):
    """Browsers check for a newer copy every time, so an app update never mixes old CSS/JS with new HTML."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app = FastAPI()
app.mount("/static", _FreshStatic(directory=STATIC), name="static")


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


# ---------------------------------------------------------------- the queue

# Each movie is a Job; the Queue runs them one at a time, in list order. A movie with review
# ticked stops after scanning ("review") and the queue moves on; once its review is saved it is
# "ready" and is saved (rendered) the next time nothing else is running.
#   queued -> analyzing -> (review -> ready ->) rendering -> done
# Any of them can end in error or cancelled.
PENDING = ("queued", "ready")             # waiting their turn
RUNNING = ("analyzing", "rendering")
FINISHED = ("done", "error", "cancelled")
SPACE_MARGIN = 2e9  # bytes kept free on top of what a movie needs


def _needed_bytes(input_path: str) -> int:
    """About how much free space saving a movie takes: its size plus a fifth (the filtered copy can
    come out a bit bigger than the original), plus a margin."""
    try:
        return int(os.path.getsize(input_path) * 1.2 + SPACE_MARGIN)
    except OSError:
        return int(SPACE_MARGIN)


def _gb(n: float) -> str:
    return f"{n / 1e9:.0f} GB" if n >= 10e9 else f"{n / 1e9:.1f} GB"


class Job:
    def __init__(self, id: int, input_path: str, out_dir: str, filters: list[str], cover: str, audio: str,
                 review: bool):
        self.id = id
        self.input = input_path
        self.out_dir = out_dir
        self.filters = filters
        self.cover = cover
        self.audio = audio
        self.review = review
        stem = os.path.splitext(os.path.basename(input_path))[0]
        self.workdir = os.path.join(WORK_ROOT, stem)
        self.output = os.path.join(out_dir, output_name(input_path))
        self.state = "queued"
        self.stage = "Waiting its turn"
        self.pct = 0.0
        self.log: list[str] = []
        self.error = ""
        self.failed_in = ""   # "analyze" or "render": where Try again picks up
        self.restarts = 0     # times it was started again after CleanCut was interrupted
        self.added = time.time()
        self.started = 0.0
        self.stage_started = 0.0
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.paused_at = 0.0  # when Pause was clicked; 0 = not paused (not saved: a restart starts it again)

    SAVED = ("id", "input", "out_dir", "filters", "cover", "audio", "review", "workdir", "output", "state", "stage",
             "pct", "log", "error", "failed_in", "restarts", "added", "started")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.SAVED}

    @classmethod
    def from_dict(cls, d: dict):
        try:
            job = cls(d["id"], d["input"], d["out_dir"], d["filters"], d["cover"], d["audio"], d["review"])
        except (KeyError, TypeError):  # unreadable: drop it rather than not start at all
            return None
        for k in cls.SAVED:
            setattr(job, k, d.get(k, getattr(job, k)))
        if job.state in RUNNING:
            # CleanCut stopped mid-movie (restart, sleep, power cut): start that movie again, but not
            # forever, in case it's the movie itself that brings CleanCut down.
            job.restarts += 1
            if job.restarts > 2:
                job.failed_in = "analyze" if job.state == "analyzing" else "render"
                job.state, job.stage = "error", "Interrupted"
                job.error = "CleanCut was interrupted while working on this movie several times."
            else:
                job.state = "ready" if job.state == "rendering" else "queued"
                job.stage = "Waiting its turn (starting again after an interruption)"
                job.pct = 0.0
        return job

    def public(self) -> dict:
        now = time.time()
        return {"id": self.id, "state": self.state, "stage": self.stage, "pct": round(self.pct, 1),
                "log": self.log[-60:] if self.state in RUNNING or self.state == "error" else [],
                "error": self.error, "input": _show(self.input), "input_name": os.path.basename(self.input),
                "output": _show(self.output), "out_dir": _show(self.out_dir), "filters": self.filters,
                "cover": self.cover, "audio": self.audio, "review": self.review, "failed_in": self.failed_in,
                "paused": bool(self.paused_at),
                "elapsed": round(now - self.started) if self.started else 0,
                "stage_elapsed": round(now - self.stage_started) if self.stage_started else 0}

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
        """Scan the movie; then save it right away, or stop for a review."""
        self.state, self.stage, self.pct, self.error = "analyzing", "Starting", 0.0, ""
        self.started = self.stage_started = time.time()
        self.log = []
        QUEUE.save()
        try:
            self._run(["analyze", self.input, "--workdir", self.workdir, "--filters", ",".join(self.filters),
                       "--audio", self.audio, "--cover", self.cover])
        except Exception as e:  # noqa: BLE001 - shown to the user
            return self._fail(e, "analyze")
        if self.review:
            self.state, self.stage, self.pct = "review", "Ready for your review", 100.0
            QUEUE.save()
        else:
            self.render()

    def render(self) -> None:
        self.state, self.stage, self.pct, self.error = "rendering", "Saving the filtered movie", 0.0, ""
        self.stage_started = time.time()
        self.started = self.started or self.stage_started
        QUEUE.save()
        try:
            free = shutil.disk_usage(self.out_dir).free
            need = _needed_bytes(self.input)
            if free < need:
                raise RuntimeError(f"Not enough free space to save it: it needs about {_gb(need)}, and the "
                                   f"drive has {_gb(free)} free. Free up some space, then click Try again.")
            self._run(["render", self.plan_path, "--out", self.out_dir])
        except Exception as e:  # noqa: BLE001
            return self._fail(e, "render")
        self.state, self.stage, self.pct = "done", "Saved", 100.0
        QUEUE.save()

    def _fail(self, e: Exception, where: str) -> None:
        if self.cancelled:
            self.state, self.stage = "cancelled", "Cancelled"
        else:
            self.state, self.stage, self.error = "error", "Something went wrong", str(e)
        self.failed_in = where
        QUEUE.save()

    # Pause freezes the whole process group (CleanCut's worker and its ffmpeg) where it is; Resume
    # lets it carry on from the same frame. The time spent paused doesn't count towards "time left".
    def pause(self) -> None:
        if self.proc and self.proc.poll() is None and not self.paused_at:
            os.killpg(self.proc.pid, signal.SIGSTOP)
            self.paused_at = time.time()

    def resume(self) -> None:
        if self.paused_at:
            if self.proc and self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGCONT)
            gap = time.time() - self.paused_at
            self.stage_started += gap
            self.started += gap
            self.paused_at = 0.0

    def cancel(self) -> None:
        self.cancelled = True
        self.resume()  # a frozen process can't tidy up and exit
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)  # it tidies up its temporary files and exits
            threading.Timer(20, self._kill, [self.proc]).start()

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is None:  # still busy after being asked to stop
            os.killpg(proc.pid, signal.SIGKILL)

    @property
    def plan_path(self) -> str:
        return os.path.join(self.workdir, "plan.json")


class Queue:
    """The list of movies, saved to queue.json so it survives restarts, and the one worker thread
    that goes through it."""

    def __init__(self):
        self.items: list[Job] = []
        self.start_at = 0.0  # the whole queue waits for this (epoch seconds); 0 = go
        self.next_id = 1
        self.lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        try:
            with open(QUEUE_FILE) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = None
        if d is None:
            try:  # a single movie saved by the version before the queue
                with open(OLD_JOB_FILE) as f:
                    old = json.load(f)
                if old.get("state") == "waiting":
                    old["state"] = "queued"
                d = {"items": [dict(old, id=1)], "start_at": old.get("start_at", 0)}
                os.remove(OLD_JOB_FILE)
            except (OSError, ValueError, AttributeError):
                d = {"items": []}
        self.items = [j for j in (Job.from_dict(x) for x in d.get("items", [])) if j]
        self.start_at = float(d.get("start_at") or 0)
        self.next_id = max([j.id for j in self.items] + [0]) + 1

    def save(self) -> None:
        with self.lock:
            data = {"items": [j.to_dict() for j in self.items], "start_at": self.start_at}
        try:
            with open(QUEUE_FILE + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(QUEUE_FILE + ".tmp", QUEUE_FILE)
        except OSError:
            pass

    def get(self, id: int) -> Job:
        job = next((j for j in self.items if j.id == id), None)
        if job is None:
            raise HTTPException(404, "That movie isn't in the list any more.")
        return job

    def running(self) -> Job | None:
        return next((j for j in self.items if j.state in RUNNING), None)

    def has_work(self) -> bool:
        return any(j.state in PENDING + RUNNING for j in self.items)

    def waiting(self) -> bool:
        """Movies are lined up but the start time hasn't come yet."""
        return self.start_at > time.time() and not self.running() and any(j.state in PENDING for j in self.items)

    def busy(self) -> bool:
        """Keep the PC awake and don't update: something is running or lined up."""
        return self.has_work()

    def add(self, jobs: list[Job], start_at: float) -> None:
        with self.lock:
            if not self.has_work():  # a new night: these movies set the start time
                self.start_at = start_at
            self.items += jobs
        self.save()

    def disk_note(self) -> str:
        """A warning when the movies still to be saved won't fit on their drives."""
        need: dict[int, list] = {}
        for j in self.items:
            if j.state in PENDING + ("review", "analyzing"):
                try:
                    dev = os.stat(j.out_dir).st_dev
                except OSError:
                    continue
                n = need.setdefault(dev, [0, j.out_dir, 0])
                n[0] += _needed_bytes(j.input)
                n[2] += 1
        for total, path, count in need.values():
            free = shutil.disk_usage(path).free
            if total > free:
                return (f"{'The movie' if count == 1 else f'The {count} movies'} still to be saved "
                        f"need{'s' if count == 1 else ''} about {_gb(total)} "
                        f"of free space, but the drive they're saved to has only {_gb(free)} free. "
                        "Free up some space, or later movies will stop with a \"not enough space\" message.")
        return ""

    def worker(self) -> None:
        """Runs forever: the next movie whose turn it is, in list order."""
        while True:
            time.sleep(1)
            if self.start_at > time.time():
                continue  # checked every second, so "Start now" and waking up late both work
            with self.lock:
                job = next((j for j in self.items if j.state in PENDING), None)
                if job:
                    job.cancelled = False
                    job.state = "analyzing" if job.state == "queued" else "rendering"  # claimed
            if job is None:
                continue
            try:
                job.analyze() if job.state == "analyzing" else job.render()
            except Exception as e:  # noqa: BLE001 - never let the worker die
                job._fail(e, "analyze" if job.state == "analyzing" else "render")


QUEUE = Queue()
threading.Thread(target=QUEUE.worker, daemon=True).start()


# Updates. CleanCut can't update itself from inside Docker, so the CleanCut helper on Windows
# (cleancut-helper.ps1) checks GitHub, reports here every 10 s, and checks again or runs the update
# when the page asks. Copies kept with Git (Luke's) can't be updated from the page.
STARTED = time.time()  # changes when CleanCut restarts, which is how the page sees an update finish
HELPER = {"seen": 0.0, "git": False, "current": "", "latest": "", "news": [], "checked": 0,
          "status": "", "error": "", "requested": False, "check": False, "check_asked": 0.0}
HELPER_ALIVE = 40  # seconds since its last report
HELPER_LOG: list[str] = []  # its recent reports, for looking into update problems (/api/helper-log)


def _update_info() -> dict:
    alive = time.time() - HELPER["seen"] < HELPER_ALIVE
    status = "requested" if HELPER["requested"] else HELPER["status"] if alive else ""
    return {"available": bool(HELPER["current"] and HELPER["latest"] and HELPER["current"] != HELPER["latest"]),
            "known": bool(HELPER["current"] and HELPER["latest"]), "git": HELPER["git"],
            "checking": alive and int(HELPER["check_asked"]) > HELPER["checked"] and time.time() - HELPER["check_asked"] < 60,
            "news": HELPER["news"], "checked": HELPER["checked"], "helper": alive,
            "status": status, "error": HELPER["error"], "started": STARTED}


# ---------------------------------------------------------------- pages and settings

@app.get("/")
def index():
    # Each CSS/JS link gets its file's change time in the address, so a browser holding an old copy
    # has to fetch the new one after an update.
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = re.sub(r'/static/([\w.-]+\.(?:css|js|svg))"',
                  lambda m: f'/static/{m[1]}?v={int(os.path.getmtime(os.path.join(STATIC, m[1])))}"', html)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def state():
    return {"queue": {"items": [j.public() for j in QUEUE.items], "start_at": QUEUE.start_at,
                      "waiting": QUEUE.waiting(), "has_work": QUEUE.has_work(), "disk_note": QUEUE.disk_note()},
            "media_root": _show(MEDIA_ROOT),
            "default_out": {"path": DEFAULT_OUT["name"], "shown": DEFAULT_OUT["host"] or DEFAULT_OUT["path"]} if DEFAULT_OUT else None,
            "filters": sorted(ALL_FILTERS), "blur_levels": len(BLUR_LEVELS), "light_blur": LIGHT_BLUR,
            "update": _update_info()}


@app.post("/api/helper")
async def helper(req: Request):
    """The CleanCut helper's report. The reply tells it whether to keep the PC awake and whether to
    start an update."""
    body = await req.json()
    news = [{"title": str(n.get("title", ""))[:300], "text": str(n.get("text", ""))[:2000]}
            for n in body.get("news") or [] if isinstance(n, dict)][:30]
    HELPER.update(seen=time.time(), git=bool(body.get("git")), current=str(body.get("current") or ""),
                  latest=str(body.get("latest") or ""), news=news, checked=int(body.get("checked") or 0),
                  status=str(body.get("status") or ""), error=str(body.get("error") or ""))
    line = (f'{time.strftime("%H:%M:%S")} current={HELPER["current"][:7]} latest={HELPER["latest"][:7]} '
            f'news={len(news)} status={HELPER["status"]!r} error={HELPER["error"][:80]!r}')
    if not HELPER_LOG or HELPER_LOG[-1][9:] != line[9:]:
        HELPER_LOG[:] = HELPER_LOG[-49:] + [line]
    update, HELPER["requested"] = HELPER["requested"], False
    check, HELPER["check"] = HELPER["check"], False
    if update:
        HELPER["status"] = "updating"  # until the helper's next report says so itself
    return {"busy": QUEUE.busy(), "update": update, "check": check}


@app.post("/api/update-check")
def update_check():
    """The check button: ask the helper to look on GitHub now."""
    if time.time() - HELPER["seen"] >= HELPER_ALIVE:
        raise HTTPException(409, "The CleanCut helper isn't running, so it can't check.")
    HELPER["check"], HELPER["check_asked"] = True, time.time()
    return {"ok": True}


@app.get("/api/helper-log")
def helper_log():
    return Response("\n".join(HELPER_LOG) + "\n", media_type="text/plain")


@app.post("/api/update")
def update():
    with QUEUE.lock:
        if QUEUE.busy():
            raise HTTPException(409, "CleanCut is busy with movies. You can update once they're finished.")
        if HELPER["git"]:
            raise HTTPException(409, "This copy of CleanCut is kept up to date with Git (git pull).")
        if time.time() - HELPER["seen"] >= HELPER_ALIVE:
            raise HTTPException(409, "The CleanCut helper isn't running. Double-click \"Update CleanCut\" "
                                     "in your CleanCut folder instead.")
        HELPER["requested"], HELPER["error"] = True, ""
    return {"ok": True}


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
        elif os.path.splitext(e.name)[1].lower() in VIDEO_EXTS and not e.name.endswith((OUTPUT_SUFFIX + ".mkv", " - Clean.mkv")):
            files.append({"name": e.name, "size": e.stat().st_size})
    at_root = real == _root_of(real)["path"]
    parent = ("" if len(ROOTS) > 1 else None) if at_root else _rel(os.path.dirname(real))
    return {"path": _rel(real), "shown": _show(real), "parent": parent, "dirs": dirs, "files": files}


@app.get("/api/output")
def output_info(input: str, out_dir: str):
    """The file name the filtered copy will get, and a note if Jellyfin wouldn't group it with the original."""
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
    try:
        with open(part, "wb") as f:
            async for chunk in req.stream():
                f.write(chunk)
        os.replace(part, dest)
    finally:
        if os.path.exists(part):  # copying was cut off
            os.remove(part)
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
    """Add one or more movies to the queue, all with the same settings. The start time only counts
    when nothing else is lined up; otherwise they simply join the end of the list."""
    body = await req.json()
    if _update_info()["status"] in ("requested", "updating"):
        raise HTTPException(409, "CleanCut is updating. Try again once the page has refreshed.")
    inputs = body.get("inputs") or ([body["input"]] if body.get("input") else [])
    if not inputs:
        raise HTTPException(400, "Choose a movie first.")
    # No folder given: each movie is saved in its own folder, next to the original.
    out_dir = _real(body["out_dir"]) if body.get("out_dir") else None
    filters = [f for f in body.get("filters", []) if f in ALL_FILTERS]
    if not filters:
        raise HTTPException(400, "Tick at least one filter.")
    cover = body.get("cover") if body.get("cover") in ("box", "blur") else "box"
    audio = body.get("audio") if body.get("audio") in ("mute", "bleep") else "mute"
    try:
        start_at = max(0.0, float(body.get("start_at") or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "That start time doesn't look right.")
    if start_at > time.time() + 7 * 86400:
        raise HTTPException(400, "Pick a start time within the next week.")
    with QUEUE.lock:
        lined_up = {j.input for j in QUEUE.items if j.state not in FINISHED}
        jobs = []
        for vpath in inputs:
            path = _real(vpath)
            if not os.path.isfile(path):
                raise HTTPException(400, f"Movie file not found: {os.path.basename(path)}")
            dest = out_dir or os.path.dirname(path)
            if os.path.abspath(os.path.join(dest, output_name(path))) == os.path.abspath(path):
                raise HTTPException(400, "The output would replace the movie itself. Pick another folder.")
            if path in lined_up:
                continue  # already in the list
            lined_up.add(path)
            jobs.append(Job(QUEUE.next_id, path, dest, filters, cover, audio, bool(body.get("review", False))))
            QUEUE.next_id += 1
        if not jobs:
            raise HTTPException(400, "Those movies are already in the list.")
        QUEUE.add(jobs, start_at)
    return {"added": len(jobs)}


@app.post("/api/items/{item_id}/cancel")
def cancel(item_id: int):
    """Stop the movie being worked on; the queue moves on to the next one."""
    QUEUE.get(item_id).cancel()
    return {"ok": True}


@app.post("/api/items/{item_id}/pause")
def pause(item_id: int):
    """Freeze the movie being worked on (the next one doesn't start meanwhile)."""
    job = QUEUE.get(item_id)
    if job.state not in RUNNING:
        raise HTTPException(409, "It isn't being worked on.")
    job.pause()
    return {"ok": True}


@app.post("/api/items/{item_id}/resume")
def resume(item_id: int):
    QUEUE.get(item_id).resume()
    return {"ok": True}


@app.post("/api/items/{item_id}/remove")
def remove(item_id: int):
    with QUEUE.lock:
        job = QUEUE.get(item_id)
        if job.state in RUNNING:
            raise HTTPException(409, "Cancel it first.")
        QUEUE.items.remove(job)
    QUEUE.save()
    return {"ok": True}


@app.post("/api/items/{item_id}/move")
def move(item_id: int, by: int):
    """Move a movie up (by=-1) or down (by=1) the list."""
    with QUEUE.lock:
        job = QUEUE.get(item_id)
        i = QUEUE.items.index(job)
        k = max(0, min(len(QUEUE.items) - 1, i + by))
        QUEUE.items.insert(k, QUEUE.items.pop(i))
    QUEUE.save()
    return {"ok": True}


@app.post("/api/items/{item_id}/retry")
def retry(item_id: int):
    """Try a movie that failed or was cancelled again, picking up after the scan when that was done."""
    with QUEUE.lock:
        job = QUEUE.get(item_id)
        if job.state not in ("error", "cancelled"):
            raise HTTPException(409, "It isn't stopped.")
        scanned = job.failed_in == "render" and os.path.exists(job.plan_path)
        job.state = "ready" if scanned else "queued"
        job.stage, job.pct, job.error, job.restarts = "Waiting its turn", 0.0, "", 0
        if not QUEUE.running() and not any(j.state in PENDING and j is not job for j in QUEUE.items):
            QUEUE.start_at = 0  # nothing else lined up: go now, not at tonight's start time
    QUEUE.save()
    return {"ok": True}


@app.post("/api/clear")
def clear():
    """Take saved, failed and cancelled movies off the list."""
    with QUEUE.lock:
        QUEUE.items = [j for j in QUEUE.items if j.state not in FINISHED]
    QUEUE.save()
    return {"ok": True}


@app.post("/api/start-now")
def start_now():
    """Skip the rest of the wait for a scheduled start."""
    QUEUE.start_at = 0
    QUEUE.save()
    return {"ok": True}


@app.post("/api/stop")
def stop():
    """Cancel everything still to do: the running movie and the lined-up ones (reviews are kept)."""
    with QUEUE.lock:
        for j in QUEUE.items:
            if j.state in PENDING:
                j.failed_in = "render" if j.state == "ready" else "analyze"
                j.state, j.stage = "cancelled", "Cancelled"
            elif j.state in RUNNING:
                j.cancel()
    QUEUE.save()
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


def _frames(ev: dict) -> list[dict]:
    """The preview frames of a moment: [{t, hide}]. Plans from older versions have none, so they
    get one frame from the middle of the longest cover, with nothing marked to hide."""
    if ev.get("frames"):
        return ev["frames"]
    c = _shown_cover(ev)
    return [{"t": (c["start"] + c["end"]) / 2, "hide": []}]


def _max_level(ev: dict) -> int:
    """Light blur only for plans that know what to paint black (made by this version or later)."""
    return len(BLUR_LEVELS) - 1 if ev.get("frames") else LIGHT_BLUR - 1


def _cover_at(ev: dict, t: float):
    """The box covering the picture at time t: a box, None for the whole screen, False for nothing."""
    for c in ev.get("covers", []):
        if c["start"] <= t <= c["end"]:
            return c["box"]
    return False


@app.get("/api/items/{item_id}/plan")
def plan(item_id: int):
    job = QUEUE.get(item_id)
    if job.state not in ("review", "ready", "rendering", "done") or not os.path.exists(job.plan_path):
        raise HTTPException(409, "Not ready for review yet.")
    p = load_plan(job.plan_path)
    visual, words = [], []
    for ev in p["events"]:
        if ev["type"] == "visual":
            scene = bool(set(ev["groups"]) & SCENE_GROUPS)
            visual.append({"id": ev["id"], "label": " + ".join(LABELS.get(g, g) for g in ev["groups"]),
                           "scene": scene, "start": ev["start"], "end": ev["end"], "action": ev["action"],
                           "suggested": ev.get("suggested", ev["action"]),
                           "unsure": ev.get("unsure", []), "confidence": ev.get("confidence"),
                           "dialogue_words": ev.get("dialogue_words"),
                           "frames": [{"t": f["t"], "box": _cover_at(ev, f["t"])} for f in _frames(ev)],
                           "max_level": _max_level(ev),
                           "full": any(cv["box"] is None for cv in ev.get("covers", [])),
                           "boxes": len(ev.get("covers", []))})
        elif ev["type"] == "language":
            words.append({"id": ev["id"], "word": ev["word"], "category": ev["category"],
                          "start": ev["start"], "action": ev["action"], "approx": ev.get("approx", False),
                          "line": ev.get("line", False), "subtitles": ev.get("source") == "subtitles"})
    return {"visual": visual, "words": words, "settings": p["settings"],
            "categories": {c: c for c in language.CATEGORIES}}


@app.post("/api/items/{item_id}/plan")
async def update_plan(item_id: int, req: Request):
    """Save review choices: {"actions": {id: action}, "cover": ..., "audio": ...}."""
    job = QUEUE.get(item_id)
    if job.state not in ("review", "ready"):
        raise HTTPException(409, "Not in review.")
    body = await req.json()
    p = load_plan(job.plan_path)
    actions = {int(k): v for k, v in body.get("actions", {}).items()}
    allowed = {"visual": {"auto", "box", "blackout", "skip", "ignore"}, "language": {"censor", "ignore"}}
    for ev in p["events"]:
        a = actions.get(ev["id"])
        if a in allowed.get(ev["type"], ()):
            ev.setdefault("suggested", ev["action"])  # what CleanCut proposed, kept for the record
            ev["action"] = a
    for key, options in (("cover", {"box", "blur"}), ("audio", {"mute", "bleep"})):
        if body.get(key) in options:
            p["settings"][key] = body[key]
            setattr(job, key, body[key])
    save_plan(p, job.plan_path)
    QUEUE.save()
    return {"ok": True}


@app.post("/api/items/{item_id}/render")
def render(item_id: int):
    """Review done: save it as soon as nothing else is being worked on."""
    with QUEUE.lock:
        job = QUEUE.get(item_id)
        if job.state != "review":
            raise HTTPException(409, "Not in review.")
        job.state, job.stage, job.pct = "ready", "Reviewed, waiting its turn to be saved", 0.0
    QUEUE.save()
    return job.public()


@app.get("/api/items/{item_id}/thumb/{event_id}")
def thumb(item_id: int, event_id: int, level: int = 0, f: int = 0):
    """A blurred still of a flagged moment (frame f of its preview frames), with everything the
    detector found painted black. The sharp frame never leaves the server."""
    job = QUEUE.get(item_id)
    ev = next((e for e in load_plan(job.plan_path)["events"] if e["id"] == event_id), None)
    if ev is None or ev["type"] != "visual":
        raise HTTPException(404)
    level = max(0, min(level, _max_level(ev)))
    frames = _frames(ev)
    fr = frames[max(0, min(f, len(frames) - 1))]
    cache = os.path.join(job.workdir, "thumbs", f"{event_id}_{fr['t']:.3f}.npy")
    if os.path.exists(cache):
        small = np.load(cache)
    else:
        small = _grab(job.input, fr["t"])
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        np.save(cache, small)
    small = _brighten(small)
    h, w = small.shape[:2]
    for x0, y0, x1, y1 in fr["hide"]:
        gx, gy = (x1 - x0) * HIDE_GROW, (y1 - y0) * HIDE_GROW
        cv2.rectangle(small, (int((x0 - gx) * w), int((y0 - gy) * h)), (int((x1 + gx) * w), int((y1 + gy) * h)),
                      (0, 0, 0), -1)
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
