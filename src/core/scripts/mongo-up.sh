#!/usr/bin/env bash
# MongoDB in a Podman pod on this computer, either agent's: mongo-up.sh
# ~/mongo.env (written by the agent) sets MONGO_IMAGE, MONGO_CACHE_GB (WiredTiger's cache: computers have
# 2 GiB and no swap), MONGO_POD and MONGO_CONTAINER, and for an app's MongoDB (acc's) MONGO_DB, MONGO_PUBLISH
# and CHISEL_VERSION. The data is in ~/mongo-data; a branch or a restore has it, not the processes: run this
# again there.
# Env (the computer's), for an app's MongoDB: MONGO_ROOT_USERNAME and MONGO_ROOT_PASSWORD turn auth on, and
# the image's first start loads ~/mongo-init/*.js into MONGO_DB; CHISEL_USER and CHISEL_PASSWORD run a chisel
# server on :8080, the way in for the app (Nevia publishes HTTPS only), to 127.0.0.1:27017 only.
set -euo pipefail
TAG=mongo
source ~/.nevia-agent/lib.sh
cd ~
[ -f ~/mongo.env ] && . ~/mongo.env
MONGO_IMAGE=${MONGO_IMAGE:?MONGO_IMAGE in ~/mongo.env} MONGO_CACHE_GB=${MONGO_CACHE_GB:?MONGO_CACHE_GB in ~/mongo.env}
pod=${MONGO_POD:?MONGO_POD in ~/mongo.env} container=${MONGO_CONTAINER:?MONGO_CONTAINER in ~/mongo.env}
auth=; [ -z "${MONGO_ROOT_USERNAME:-}${MONGO_ROOT_PASSWORD:-}" ] || auth=1
if [ -n "$auth" ] && { [ -z "${MONGO_ROOT_USERNAME:-}" ] || [ -z "${MONGO_ROOT_PASSWORD:-}" ]; }; then
  die "MONGO_ROOT_USERNAME and MONGO_ROOT_PASSWORD go together"
fi

mkdir -p ~/mongo-data ~/mongo-init   # podman creates neither
fresh=; [ -n "$(ls -A ~/mongo-data)" ] || fresh=1

if ! podman pod exists "$pod"; then
  podman pod create --name "$pod" ${MONGO_PUBLISH:+-p 127.0.0.1:27017:27017} >/dev/null
fi
if ! podman container exists "$container"; then
  set -- -v "$HOME/mongo-data:/data/db"
  # The image's entrypoint makes the root user and runs the init scripts on a first start, then turns auth on.
  [ -z "$auth" ] || set -- "$@" -v "$HOME/mongo-init:/docker-entrypoint-initdb.d:ro" \
    -e MONGO_INITDB_ROOT_USERNAME -e MONGO_INITDB_ROOT_PASSWORD -e "MONGO_INITDB_DATABASE=${MONGO_DB:?MONGO_DB with auth}"
  MONGO_INITDB_ROOT_USERNAME=${MONGO_ROOT_USERNAME:-} MONGO_INITDB_ROOT_PASSWORD=${MONGO_ROOT_PASSWORD:-} \
    podman create --pod "$pod" --name "$container" "$@" "$MONGO_IMAGE" \
      --bind_ip_all --wiredTigerCacheSizeGB "$MONGO_CACHE_GB" >/dev/null
fi
podman start "$container" >/dev/null
# A first start with init scripts answers pings from a temporary mongod before it is done: wait for the end.
if [ -n "$auth" ] && [ -n "$fresh" ]; then
  for i in $(seq 120); do
    podman logs "$container" 2>&1 | grep -q 'MongoDB init process complete' && break
    [ "$i" -lt 120 ] || { podman logs --tail 30 "$container" >&2; die "MongoDB's init did not end in 2 min"; }
    sleep 1
  done
fi
until podman exec "$container" mongosh --quiet --eval 'db.runCommand({ ping: 1 }).ok' 2>/dev/null | grep -q 1; do sleep 2; done

if [ -n "${CHISEL_USER:-}" ]; then
  [ -n "${CHISEL_PASSWORD:-}" ] && [ -n "${CHISEL_VERSION:-}" ] || die "CHISEL_USER without CHISEL_PASSWORD or CHISEL_VERSION"
  install_chisel ~/mongo "$CHISEL_VERSION"
  umask 077
  # Tunnels may only reach 127.0.0.1:27017; chisel reloads its authfile.
  printf '{"%s:%s": ["^127\\\\.0\\\\.0\\\\.1:27017$"]}\n' "$CHISEL_USER" "$CHISEL_PASSWORD" > ~/mongo/chisel-auth.json
  mkdir -p ~/mongo/log
  if ! running ~/mongo/chisel.pid; then
    say "starting the chisel server on :8080"
    nohup ~/mongo/bin/chisel server --host 0.0.0.0 --port 8080 --authfile ~/mongo/chisel-auth.json \
      >> ~/mongo/log/chisel.log 2>&1 < /dev/null &
    echo $! > ~/mongo/chisel.pid
    sleep 1
    running ~/mongo/chisel.pid || { tail -30 ~/mongo/log/chisel.log >&2; die "chisel exited"; }
  fi
fi
say "ready${auth:+ (auth)}${CHISEL_USER:+, chisel on :8080}"
