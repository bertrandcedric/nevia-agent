#!/usr/bin/env bash
# One-time setup of a MongoDB computer, either agent's: mongo-host.sh IMAGE...
# Rootless podman, catatonit as the pod's init, pymongo for m2p's seed.py, and the images pulled.
set -euo pipefail
TAG=mongo
source ~/.nevia-agent/lib.sh

# Rootless podman on Nevia's overlay root needs chown errors ignored.
mkdir -p ~/.config/containers
printf '[storage]\ndriver = "overlay"\n[storage.options.overlay]\nignore_chown_errors = "true"\n' \
  > ~/.config/containers/storage.conf

command -v catatonit >/dev/null && python3 -c 'import pymongo' 2>/dev/null || apt_install catatonit python3-pymongo
for image in "$@"; do podman pull -q "$image" >/dev/null; done
echo "host ready: $(podman --version)"
