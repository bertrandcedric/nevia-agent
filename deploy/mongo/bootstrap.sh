#!/bin/sh
# Runs ON the Nevia MongoDB computer, piped through `nevia exec <computer> -- sh -s`. Idempotent:
#   - installs MongoDB, mongosh and chisel into $HOME (the persistent disk) on first run
#   - starts mongod on 127.0.0.1:27017 (--auth) if it isn't running, creates the root user once
#   - loads the app's seed data (~/hello-mongo/init/*.js, pushed by deploy.sh) once, into an empty database
#   - starts the chisel server on :8080 (published on https 443) if it isn't running
#
# Nevia has no private network between computers and publishes HTTPS only, so the app reaches
# mongod through chisel (MongoDB wire protocol over a WebSocket). Two passwords guard the public
# URL: chisel's, which only lets tunnels reach 127.0.0.1:27017, then MongoDB's.
#
# Env (computer's stored env, see deploy.sh):
#   MONGO_ROOT_USERNAME, MONGO_ROOT_PASSWORD, CHISEL_USER, CHISEL_PASSWORD
set -eu

MONGO_VERSION=8.0.34
MONGOSH_VERSION=2.3.8
CHISEL_VERSION=1.10.1

BASE=$HOME/hello-mongo
BIN=$BASE/bin
mkdir -p "$BIN" "$BASE/data" "$BASE/log" "$BASE/tmp"

say() { echo "[hello-mongo] $*"; }
step() { printf '\n[hello-mongo] ─── %s ───\n' "$*"; }
die() { echo "[hello-mongo] error: $*" >&2; exit 1; }

for v in MONGO_ROOT_USERNAME MONGO_ROOT_PASSWORD CHISEL_USER CHISEL_PASSWORD; do
  eval "[ -n \"\${$v:-}\" ]" || die "missing env $v"
done
[ "$(uname -m)" = x86_64 ] || die "this script handles x86_64 only, got $(uname -m)"

fetch() { curl -fsSL "$1" -o "$2"; }

# --- install -----------------------------------------------------------------
step "1/2 MongoDB $MONGO_VERSION, mongosh and chisel, installed on first run only"
if [ ! -x "$BIN/mongod" ]; then
  # No debian13 build: the debian12 one runs on the Nevia image (Debian 13).
  url="https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-debian12-$MONGO_VERSION.tgz"
  say "downloading $url"
  fetch "$url" "$BASE/tmp/mongodb.tgz"
  echo "$(curl -fsSL "$url.sha256" | cut -d' ' -f1)  $BASE/tmp/mongodb.tgz" | sha256sum -c - >/dev/null ||
    die "MongoDB checksum mismatch"
  tar -xzf "$BASE/tmp/mongodb.tgz" -C "$BASE/tmp"
  cp "$BASE"/tmp/mongodb-linux-*/bin/mongod "$BIN/"
  rm -rf "$BASE"/tmp/*
  if ldd "$BIN/mongod" | grep -q 'not found'; then
    ldd "$BIN/mongod" | grep 'not found' >&2
    rm -f "$BIN/mongod"
    die "mongod is missing shared libraries on this image"
  fi
fi

# mongosh publishes no checksum file; the version is pinned and fetched over HTTPS.
if [ ! -x "$BIN/mongosh" ]; then
  url="https://downloads.mongodb.com/compass/mongosh-$MONGOSH_VERSION-linux-x64.tgz"
  say "downloading $url"
  fetch "$url" "$BASE/tmp/mongosh.tgz"
  tar -xzf "$BASE/tmp/mongosh.tgz" -C "$BASE/tmp"
  cp "$BASE"/tmp/mongosh-*/bin/mongosh "$BIN/"
  rm -rf "$BASE"/tmp/*
fi

if [ ! -x "$BIN/chisel" ]; then
  name="chisel_${CHISEL_VERSION}_linux_amd64.gz"
  rel="https://github.com/jpillora/chisel/releases/download/v$CHISEL_VERSION"
  say "downloading $rel/$name"
  fetch "$rel/$name" "$BASE/tmp/$name"
  fetch "$rel/chisel_${CHISEL_VERSION}_checksums.txt" "$BASE/tmp/checksums.txt"
  (cd "$BASE/tmp" && grep " $name\$" checksums.txt | sha256sum -c - >/dev/null) || die "chisel checksum mismatch"
  gunzip -c "$BASE/tmp/$name" > "$BIN/chisel"
  chmod +x "$BIN/chisel"
  rm -rf "$BASE"/tmp/*
fi

# --- run -----------------------------------------------------------------------
step "2/2 starting mongod and the chisel server"
running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
mongosh_local() { "$BIN/mongosh" --quiet "mongodb://127.0.0.1:27017/admin" "$@"; }

# Sleep/wake keeps processes; a branch or a restore doesn't, so start whatever isn't running.
if ! running "$BASE/mongod.pid"; then
  say "starting mongod"
  rm -f "$BASE/mongod.pid"
  # Small WiredTiger cache: the default (half the RAM) is more than this app needs.
  "$BIN/mongod" --dbpath "$BASE/data" --bind_ip 127.0.0.1 --port 27017 --auth \
    --wiredTigerCacheSizeGB 0.25 --fork --logpath "$BASE/log/mongod.log" --logappend \
    --pidfilepath "$BASE/mongod.pid" >/dev/null || { tail -30 "$BASE/log/mongod.log" >&2; die "mongod failed to start"; }
fi

i=0
until mongosh_local --eval 'db.runCommand({ ping: 1 }).ok' >/dev/null 2>&1; do
  i=$((i + 1)); [ $i -lt 30 ] || { tail -30 "$BASE/log/mongod.log" >&2; die "mongod not answering after 30s"; }
  sleep 1
done

# First start: create the root user through the localhost exception (open until a user exists).
if [ ! -f "$BASE/data/.hello-initialized" ]; then
  say "creating root user $MONGO_ROOT_USERNAME"
  mongosh_local --eval 'db.createUser({ user: process.env.MONGO_ROOT_USERNAME, pwd: process.env.MONGO_ROOT_PASSWORD, roles: ["root"] })' >/dev/null
  touch "$BASE/data/.hello-initialized"
fi

# The app's seed data (its local/mongo-init/*.js, pushed by deploy.sh into $BASE/init), loaded
# into the hello database once, and only while it has no collection: existing data is never touched.
if [ ! -f "$BASE/data/.hello-seeded" ] && ls "$BASE"/init/*.js >/dev/null 2>&1; then
  mongosh_hello() {
    "$BIN/mongosh" --quiet "mongodb://127.0.0.1:27017/hello" --authenticationDatabase admin \
      -u "$MONGO_ROOT_USERNAME" -p "$MONGO_ROOT_PASSWORD" "$@"
  }
  if [ "$(mongosh_hello --eval 'db.getCollectionNames().length')" = 0 ]; then
    for f in "$BASE"/init/*.js; do
      say "loading $(basename "$f") into hello"
      mongosh_hello "$f" >/dev/null
    done
  else
    say "hello already has data: local/mongo-init not loaded"
  fi
  touch "$BASE/data/.hello-seeded"
fi

# Tunnels may only reach 127.0.0.1:27017. Rewritten on each run; chisel reloads its authfile.
umask 077
printf '{"%s:%s": ["^127\\\\.0\\\\.0\\\\.1:27017$"]}\n' "$CHISEL_USER" "$CHISEL_PASSWORD" > "$BASE/chisel-auth.json"

if ! running "$BASE/chisel.pid"; then
  say "starting chisel server on :8080"
  nohup "$BIN/chisel" server --host 0.0.0.0 --port 8080 --authfile "$BASE/chisel-auth.json" \
    >> "$BASE/log/chisel.log" 2>&1 < /dev/null &
  echo $! > "$BASE/chisel.pid"
  sleep 1
  running "$BASE/chisel.pid" || { tail -30 "$BASE/log/chisel.log" >&2; die "chisel exited"; }
fi

say "ready (mongod pid $(cat "$BASE/mongod.pid"), chisel pid $(cat "$BASE/chisel.pid"))"
