#!/bin/sh
# Runs ON the MongoDB computer (lib.sh + this, through nevia exec): installs MongoDB, mongosh and
# chisel, starts mongod (127.0.0.1:27017, --auth) and the chisel server (:8080, https 443), creates
# the root user and loads the seed data (~/hello-mongo/init/*.js) once, into an empty database.
# The app reaches mongod through chisel: Nevia publishes HTTPS only.
# Env: MONGO_ROOT_USERNAME, MONGO_ROOT_PASSWORD, CHISEL_USER, CHISEL_PASSWORD.
set -eu
TAG=hello-mongo
MONGO_VERSION=8.0.34
MONGOSH_VERSION=2.3.8
BASE=$HOME/hello-mongo
BIN=$BASE/bin
mkdir -p "$BIN" "$BASE/data" "$BASE/log" "$BASE/tmp"

for v in MONGO_ROOT_USERNAME MONGO_ROOT_PASSWORD CHISEL_USER CHISEL_PASSWORD; do
  eval "[ -n \"\${$v:-}\" ]" || die "missing env $v"
done

step "1/2 MongoDB $MONGO_VERSION, mongosh and chisel, installed on first run only"
if [ ! -x "$BIN/mongod" ]; then
  # The debian12 build runs on the Nevia image (Debian 13).
  url="https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-debian12-$MONGO_VERSION.tgz"
  say "downloading $url"
  fetch "$url" "$BASE/tmp/mongodb.tgz"
  echo "$(curl -fsSL "$url.sha256" | cut -d' ' -f1)  $BASE/tmp/mongodb.tgz" | sha256sum -c - >/dev/null ||
    die "MongoDB checksum mismatch"
  tar -xzf "$BASE/tmp/mongodb.tgz" -C "$BASE/tmp" && cp "$BASE"/tmp/mongodb-linux-*/bin/mongod "$BIN/" && rm -rf "$BASE"/tmp/*
fi
if [ ! -x "$BIN/mongosh" ]; then
  url="https://downloads.mongodb.com/compass/mongosh-$MONGOSH_VERSION-linux-x64.tgz"
  say "downloading $url"
  fetch "$url" "$BASE/tmp/mongosh.tgz"
  tar -xzf "$BASE/tmp/mongosh.tgz" -C "$BASE/tmp" && cp "$BASE"/tmp/mongosh-*/bin/mongosh "$BIN/" && rm -rf "$BASE"/tmp/*
fi
install_chisel "$BASE"

step "2/2 starting mongod and the chisel server"
mongosh_admin() { "$BIN/mongosh" --quiet "mongodb://127.0.0.1:27017/admin" "$@"; }
mongosh_hello() {
  "$BIN/mongosh" --quiet "mongodb://127.0.0.1:27017/hello" --authenticationDatabase admin \
    -u "$MONGO_ROOT_USERNAME" -p "$MONGO_ROOT_PASSWORD" "$@"
}

# A branch or a restore doesn't copy processes: start whatever isn't running.
if ! running "$BASE/mongod.pid"; then
  say "starting mongod"
  "$BIN/mongod" --dbpath "$BASE/data" --bind_ip 127.0.0.1 --port 27017 --auth \
    --wiredTigerCacheSizeGB 0.25 --fork --logpath "$BASE/log/mongod.log" --logappend \
    --pidfilepath "$BASE/mongod.pid" >/dev/null || { tail -30 "$BASE/log/mongod.log" >&2; die "mongod failed to start"; }
fi
i=0
until mongosh_admin --eval 'db.runCommand({ ping: 1 }).ok' >/dev/null 2>&1; do
  i=$((i + 1)); [ $i -lt 30 ] || { tail -30 "$BASE/log/mongod.log" >&2; die "mongod not answering after 30s"; }
  sleep 1
done

# The root user, through the localhost exception (open until a user exists).
if [ ! -f "$BASE/data/.hello-initialized" ]; then
  say "creating root user $MONGO_ROOT_USERNAME"
  mongosh_admin --eval 'db.createUser({ user: process.env.MONGO_ROOT_USERNAME, pwd: process.env.MONGO_ROOT_PASSWORD, roles: ["root"] })' >/dev/null
  touch "$BASE/data/.hello-initialized"
fi

# The seed data, once, only into a database with no collection.
if [ ! -f "$BASE/data/.hello-seeded" ] && ls "$BASE"/init/*.js >/dev/null 2>&1; then
  if [ "$(mongosh_hello --eval 'db.getCollectionNames().length')" = 0 ]; then
    for f in "$BASE"/init/*.js; do say "loading $(basename "$f") into hello"; mongosh_hello "$f" >/dev/null; done
  fi
  touch "$BASE/data/.hello-seeded"
fi

# Tunnels may only reach 127.0.0.1:27017; chisel reloads its authfile.
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
say "ready"
