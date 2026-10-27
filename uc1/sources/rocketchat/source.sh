#!/bin/sh
# The Rocket.Chat source, on the lab computer (pushed to ~/m2p/scripts/source by rc-lab.sh, run by
# rc-lab.sh up and seed, and by analyse and migrate before they start):
#   sh source.sh up           MongoDB (replica set) + Rocket.Chat, podman, host network; MongoDB's
#                             TTL expiry on again
#   sh source.sh seed         Rocket.Chat data, through its REST API (seed.mjs)
#   sh source.sh before-run   before analyse and a migration: Rocket.Chat stopped (memory), MongoDB
#                             started, its TTL expiry off (instances and rocketchat_notification_queue
#                             would lose documents between analyse and the end of the run)
#   sh source.sh status       its containers and admin
# Env: LAB_URL (the computer's URL), SEED_* (seed.mjs).
set -eu
TAG=rocketchat
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/../lib.sh"
STATE=$HOME/rocketchat
mkdir -p "$STATE"

MONGO_IMAGE=${MONGO_IMAGE:-docker.io/library/mongo:8.0.34}
RC_IMAGE=${RC_IMAGE:-docker.io/rocketchat/rocket.chat:8.7.1}
RC_URL=${LAB_URL:-http://localhost:3000}

mongosh_() { podman exec rc-mongo mongosh --quiet "$@"; }
# ttl <true|false>: MongoDB's TTL monitor (until mongod restarts: up sets it again).
ttl() { mongosh_ --eval "db.adminCommand({ setParameter: 1, ttlMonitorEnabled: $1 }).ok" >/dev/null; }
wait_mongo() {
  i=0
  until mongosh_ --eval 'db.runCommand({ ping: 1 }).ok' >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -lt 60 ] || { podman logs --tail 30 rc-mongo >&2; die "mongod not answering after 60 s"; }
    sleep 1
  done
}

up() {
  step "1/3 MongoDB ($MONGO_IMAGE), replica set rs0 on 127.0.0.1:27017"
  # The computer's network (a pod would need catatonit, not on the image): mongod on 127.0.0.1
  # only, Rocket.Chat on :3000, which Nevia publishes on 443.
  if podman container exists rc-mongo; then
    podman start rc-mongo >/dev/null
  else
    say "pulling and starting $MONGO_IMAGE"
    podman run -d --network host --name rc-mongo -v rc-mongo-data:/data/db "$MONGO_IMAGE" \
      --replSet rs0 --bind_ip 127.0.0.1 --port 27017 --wiredTigerCacheSizeGB 0.25 >/dev/null
  fi
  wait_mongo
  # Rocket.Chat needs a replica set (change streams); one member, initiated once.
  mongosh_ --eval 'try { rs.status().ok } catch (e) { rs.initiate({ _id: "rs0", members: [{ _id: 0, host: "127.0.0.1:27017" }] }).ok }' >/dev/null
  i=0
  until [ "$(mongosh_ --eval 'db.hello().isWritablePrimary' 2>/dev/null)" = true ]; do
    i=$((i + 1)); [ $i -lt 60 ] || die "the replica set has no primary after 60 s"
    sleep 1
  done
  ttl true

  step "2/3 Rocket.Chat ($RC_IMAGE) on :3000, $RC_URL"
  if [ ! -s "$STATE/rc-admin" ]; then
    (umask 077 && printf "admin %s\n" "$(od -An -tx1 -N16 /dev/urandom | tr -d " \n")" > "$STATE/rc-admin")
  fi
  read -r RC_ADMIN_USER RC_ADMIN_PASS < "$STATE/rc-admin"
  if podman container exists rc-app; then
    podman start rc-app >/dev/null
  else
    say "pulling and starting $RC_IMAGE (first time: a few minutes)"
    # The admin is created at the first start; the wizard, 2FA and the rate limiter are off so
    # that seed.mjs can drive the REST API.
    podman run -d --network host --name rc-app \
      -e MONGO_URL='mongodb://127.0.0.1:27017/rocketchat?replicaSet=rs0' \
      -e ROOT_URL="$RC_URL" -e PORT=3000 -e DEPLOY_METHOD=docker \
      -e ADMIN_USERNAME="$RC_ADMIN_USER" -e ADMIN_PASS="$RC_ADMIN_PASS" -e ADMIN_EMAIL=admin@example.com \
      -e OVERWRITE_SETTING_Show_Setup_Wizard=completed \
      -e OVERWRITE_SETTING_Accounts_TwoFactorAuthentication_Enabled=false \
      -e OVERWRITE_SETTING_API_Enable_Rate_Limiter=false \
      -e OVERWRITE_SETTING_Accounts_Password_Policy_Enabled=false \
      "$RC_IMAGE" >/dev/null
  fi

  step "3/3 waiting for Rocket.Chat"
  i=0
  until curl -fsS http://127.0.0.1:3000/api/info >/dev/null 2>&1; do
    i=$((i + 1)); [ $i -lt 120 ] || { podman logs --tail 40 rc-app >&2; die "Rocket.Chat not answering after 10 min"; }
    sleep 5
  done
  say "Rocket.Chat $(curl -fsS http://127.0.0.1:3000/api/info | jq -r .version) ready: $RC_URL (user $RC_ADMIN_USER, password in ~/rocketchat/rc-admin)"
}

seed() {
  curl -fsS http://127.0.0.1:3000/api/info >/dev/null 2>&1 || die "Rocket.Chat is not running: ./rc-lab.sh up"
  read -r RC_ADMIN_USER RC_ADMIN_PASS < "$STATE/rc-admin"
  step "generating Rocket.Chat data"
  RC_URL=http://127.0.0.1:3000 RC_ADMIN_USER=$RC_ADMIN_USER RC_ADMIN_PASS=$RC_ADMIN_PASS node "$HERE/seed.mjs"
  say "collections in rocketchat (documents):"
  mongosh_ rocketchat --eval 'db.getCollectionNames().sort().map(c => [c, db.getCollection(c).estimatedDocumentCount()]).filter(([, n]) => n > 0).forEach(([c, n]) => print(`  ${c}: ${n}`))'
}

case ${1:-} in
  up) up ;;
  seed) seed ;;
  # The data must not change from analyse to the end of a migration, and Claude needs the memory.
  before-run) podman stop rc-app >/dev/null 2>&1 || true; podman start rc-mongo >/dev/null; wait_mongo; ttl false ;;
  status) [ ! -s "$STATE/rc-admin" ] || say "Rocket.Chat admin: $(cut -d' ' -f1 "$STATE/rc-admin") (password: cat ~/rocketchat/rc-admin)" ;;
  *) die "unknown command '${1:-}'" ;;
esac
