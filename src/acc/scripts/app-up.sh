#!/usr/bin/env bash
# On the app computer: app-up.sh DB_KIND PORT JDK_MAJOR MAVEN_VERSION CHISEL_VERSION
# After acc has put the Maven project in ~/app/src (or a jar built on the agent in ~/app/src/app.jar):
# installs the JDK and Maven (and chisel for mongo) once, builds the jar without the tests (they ran on the
# agent), then (re)starts the tunnel to MongoDB, if any, and the app, until GET /healthz answers.
# Env (the computer's): MONGODB_URI, MONGO_TUNNEL_URL, CHISEL_USER, CHISEL_PASSWORD for mongo;
# DATABASE_URL, PG_CA_FILE for postgres.
set -euo pipefail
TAG=app
source ~/.nevia-agent/lib.sh
kind=${1:?usage: app-up.sh DB_KIND PORT JDK_MAJOR MAVEN_VERSION CHISEL_VERSION} port=$2 jdk=$3 maven=$4 chisel=$5
base=$HOME/app
mkdir -p "$base/log"
case $kind in
  mongo)    required="MONGODB_URI MONGO_TUNNEL_URL CHISEL_USER CHISEL_PASSWORD" ;;
  postgres) required="DATABASE_URL PG_CA_FILE" ;;
  *)        die "database.kind must be mongo or postgres, got '$kind'" ;;
esac
for v in $required; do [ -n "${!v:-}" ] || die "missing env $v"; done

install_java "$base" "$jdk" "$maven"
[ "$kind" != mongo ] || install_chisel "$base" "$chisel"

if [ -f "$base/src/app.jar" ]; then
  say "jar built on the agent"
  cp "$base/src/app.jar" "$base/app.jar"
else
  [ -f "$base/src/pom.xml" ] || die "no sources in $base/src"
  (cd "$base/src" && mvn -q -B -DskipTests package) >> "$base/log/build.log" 2>&1 ||
    { tail -40 "$base/log/build.log" >&2; die "build failed"; }
  jar=$(app_jar "$base/src")
  [ -n "$jar" ] || die "no jar in src/target"
  cp "$jar" "$base/app.jar"
fi

# The tunnel to MongoDB: restarted when its URL changes, stopped for Postgres.
if running "$base/chisel.pid" && { [ "$kind" = postgres ] || [ "$(cat "$base/chisel.url" 2>/dev/null)" != "$MONGO_TUNNEL_URL" ]; }; then
  kill "$(cat "$base/chisel.pid")"; sleep 1
fi
if [ "$kind" = mongo ] && ! running "$base/chisel.pid"; then
  say "starting the chisel client to $MONGO_TUNNEL_URL"
  echo "$MONGO_TUNNEL_URL" > "$base/chisel.url"
  AUTH="$CHISEL_USER:$CHISEL_PASSWORD" nohup "$base/bin/chisel" client "$MONGO_TUNNEL_URL" \
    127.0.0.1:27017:127.0.0.1:27017 >> "$base/log/chisel.log" 2>&1 < /dev/null &
  echo $! > "$base/chisel.pid"
  sleep 1
  running "$base/chisel.pid" || { tail -30 "$base/log/chisel.log" >&2; die "chisel client exited"; }
fi

# Any app still running goes first, and the port with it: this computer's, or one a restored checkpoint
# brought back running (its processes come with it). A new app that can't take the port dies, while the old
# one goes on answering /healthz for a while.
stop_app() {
  local pids i
  pids=$(pgrep -f -- "-jar $base/app.jar" || true)
  [ -z "$pids" ] || { say "stopping the app running ($(echo $pids))"; kill $pids 2>/dev/null || true; }
  for i in $(seq 30); do
    pgrep -f -- "-jar $base/app.jar" >/dev/null || break
    [ "$i" -lt 30 ] || pkill -9 -f -- "-jar $base/app.jar" || true
    sleep 1
  done
  for i in $(seq 30); do
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/" || return 0
    sleep 1
  done
  die "port $port still answers after the app stopped"
}
stop_app
# From src: PG_CA_FILE is relative to it.
(cd "$base/src" && HOST=0.0.0.0 PORT=$port nohup java -Xmx256m -jar "$base/app.jar" >> "$base/log/app.log" 2>&1 < /dev/null &
 echo $! > "$base/app.pid")
for i in $(seq 60); do
  curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break
  running "$base/app.pid" || { tail -30 "$base/log/app.log" >&2; die "app exited"; }
  [ "$i" -lt 60 ] || { tail -30 "$base/log/app.log" >&2; die "app not healthy after 60 s"; }
  sleep 1
done
# The app answering is this one: still alive a moment after.
sleep 2
running "$base/app.pid" || { tail -30 "$base/log/app.log" >&2; die "app exited after its first answer"; }
say "ready (pid $(cat "$base/app.pid"))"
