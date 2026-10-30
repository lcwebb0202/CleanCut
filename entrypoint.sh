#!/bin/sh
# On Linux, run as the person's own user (PUID/PGID from .env) so the movies CleanCut saves
# belong to them, not to root. On Windows (no PUID set) it runs as root, which is fine there.
set -e
if [ -n "$PUID" ] && [ "$PUID" != "0" ]; then
  PGID="${PGID:-$PUID}"
  mkdir -p /models /work
  chown -R "$PUID:$PGID" /models /work 2>/dev/null || true   # model cache and working files
  export HOME=/tmp
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python3 -m cleancut "$@"
fi
exec python3 -m cleancut "$@"
