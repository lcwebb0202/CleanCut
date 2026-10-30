#!/usr/bin/env bash
# Linux: start CleanCut and open it in the browser.   Run:  bash start-cleancut.sh
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "First time setup."
  read -r -p "Full path of your movie folder (for example /home/$USER/Movies): " dir
  dir="${dir%/}"
  if [ ! -d "$dir" ]; then
    echo "That folder doesn't exist: $dir"
    exit 1
  fi
  {
    echo "# The folder with your movies. CleanCut can only see (and save to) this folder and the folders inside it."
    echo "MEDIA_DIR=$dir"
    echo "# Save files as you, not as root."
    echo "PUID=$(id -u)"
    echo "PGID=$(id -g)"
    echo "# Only with an NVIDIA graphics card and the NVIDIA Container Toolkit installed: remove the # below."
    echo "# COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml"
  } > .env
  echo "Saved your settings in .env (you can edit it later)."
fi

if ! docker info >/dev/null 2>&1; then
  echo
  echo "Docker isn't running, or your user isn't allowed to use it."
  echo "  - Start it:            sudo systemctl start docker"
  echo "  - Allow your user:     sudo usermod -aG docker \$USER   (then log out and back in)"
  exit 1
fi

echo "Starting CleanCut..."
echo "The very first time, this downloads about 13 GB and can take 30 minutes or more."
docker compose up -d --build

echo "CleanCut is running at http://localhost:8800"
sleep 3
xdg-open http://localhost:8800 >/dev/null 2>&1 || true
