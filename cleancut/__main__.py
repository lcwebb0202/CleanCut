"""Command line: python -m cleancut {process,analyze,render} ..."""
import argparse
import os
import signal
import sys

from .pipeline import ALL_FILTERS, analyze, jellyfin_note, load_plan, output_name
from .render import render


def _filters(text: str) -> set[str]:
    chosen = {f.strip() for f in text.split(",") if f.strip()}
    unknown = chosen - ALL_FILTERS
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown filters {sorted(unknown)}; choose from {sorted(ALL_FILTERS)}")
    return chosen


def _note(input_path: str, out_dir: str) -> None:
    note = jellyfin_note(input_path, out_dir)
    if note:
        print("Jellyfin note: " + note)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cleancut", description="Cover nudity and silence swearing in a movie.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def analyze_opts(sp):
        sp.add_argument("input")
        sp.add_argument("--filters", type=_filters, default=set(ALL_FILTERS),
                        help=f"comma-separated, default all: {','.join(sorted(ALL_FILTERS))}")
        sp.add_argument("--audio", choices=["mute", "bleep"], default="mute")
        sp.add_argument("--cover", choices=["box", "blur"], default="box")
        sp.add_argument("--sample-fps", type=float, default=4.0, help="video frames checked per second")
        sp.add_argument("--whisper-model", default=None)
        sp.add_argument("--test-faces", action="store_true", help="also cover faces (for testing on clean clips)")

    sp = sub.add_parser("process", help="analyze and render in one go")
    analyze_opts(sp)
    sp.add_argument("--out", required=True, help="output folder")

    sp = sub.add_parser("analyze", help="only make the plan (for review)")
    analyze_opts(sp)
    sp.add_argument("--workdir", required=True)

    sp = sub.add_parser("render", help="render a reviewed plan")
    sp.add_argument("plan")
    sp.add_argument("--out", required=True, help="output folder")
    sp.add_argument("--audio", choices=["mute", "bleep"], help="override the plan's setting")
    sp.add_argument("--cover", choices=["box", "blur"], help="override the plan's setting")

    sp = sub.add_parser("serve", help="run the browser app")
    sp.add_argument("--port", type=int, default=8800)

    a = p.parse_args(argv)
    # Cancel in the web app stops this process with SIGTERM: exit through the normal cleanup (temporary
    # files, the half-saved movie) instead of dying on the spot.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if a.cmd == "serve":
        from .web import serve
        print(f"CleanCut is running. Open http://localhost:{a.port} in your browser.", flush=True)
        serve(a.port)
        return 0
    if a.cmd == "render":
        plan = load_plan(a.plan)
        for key in ("audio", "cover"):
            if getattr(a, key):
                plan["settings"][key] = getattr(a, key)
        _note(plan["input"], a.out)
        render(plan, os.path.join(a.out, output_name(plan["input"])))
        return 0

    workdir = a.workdir if a.cmd == "analyze" else os.path.join(
        a.out, ".cleancut-work", os.path.splitext(os.path.basename(a.input))[0])
    plan = analyze(a.input, workdir, a.filters, a.audio, a.cover, a.sample_fps, a.whisper_model, a.test_faces)
    if a.cmd == "process":
        _note(a.input, a.out)
        render(plan, os.path.join(a.out, output_name(a.input)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
