#!/usr/bin/env bash
# Linux: stop CleanCut (it otherwise keeps running, and restarts with the computer).   Run:  bash stop-cleancut.sh
cd "$(dirname "$0")"
docker compose down
echo "CleanCut is stopped."
