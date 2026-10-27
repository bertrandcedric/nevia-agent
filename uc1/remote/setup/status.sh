#!/bin/sh
# On the lab computer, for ./rc-lab.sh status: containers, the source's status, the project, the
# last runs.
#   sh setup/status.sh
. "$(dirname "$0")/env.sh"

! command -v podman >/dev/null || podman ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null || true
[ ! -f "$SOURCE_SH" ] || sh "$SOURCE_SH" status
say "source: database $MONGO_DB; project: $PROJECT$( [ -f "$CONFIG" ] || echo ' (not analysed yet)')"
ls -t "$LAB"/runs/*[0-9].log 2>/dev/null | head -n 5 | sed 's/^/  run log: /' || true
