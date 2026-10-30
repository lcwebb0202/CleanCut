"""Write a plan with a fake cover box (1-4 s) and a forced full-screen cover (5-6 s), to test covering.

Usage: python3 fake_cover_plan.py INPUT PLAN_OUT
"""
import json
import sys

input_path, out = sys.argv[1:3]
plan = {
    "version": 1,
    "input": input_path,
    "settings": {"filters": ["nudity"], "audio": "mute", "cover": "box", "sample_fps": 4.0},
    "tracks": [],
    "cuts": [],
    "events": [
        {"type": "visual", "groups": ["nudity"], "start": 1.0, "end": 4.0, "action": "auto", "id": 1,
         "covers": [{"start": 1.0, "end": 4.0, "box": [0.2, 0.2, 0.5, 0.7]}]},
        {"type": "visual", "groups": ["nudity"], "start": 5.0, "end": 6.0, "action": "auto", "id": 2,
         "covers": [{"start": 5.0, "end": 6.0, "box": None}]},
    ],
}
json.dump(plan, open(out, "w"), indent=1)
