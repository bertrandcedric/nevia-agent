#!/bin/sh
# Runs ON the app computer (lib.sh + this, through nevia exec), after deploy.sh has unpacked the
# Maven project into ~/app/src: installs JDK, Maven (and chisel for mongo), builds the jar without
# the tests (they ran on the agent, on the whole repo), unless ~/app/src/app.jar was pushed
# already built, then (re)starts the tunnel if any and the app.
# Env: DB_KIND (mongo or postgres), then MONGODB_URI, MONGO_TUNNEL_URL, CHISEL_USER,
# CHISEL_PASSWORD for mongo; DATABASE_URL, PG_CA_FILE for postgres.
set -eu
TAG=app
BASE=$HOME/app
mkdir -p "$BASE/log"

case ${DB_KIND:-} in
  mongo)    required="MONGODB_URI MONGO_TUNNEL_URL CHISEL_USER CHISEL_PASSWORD" ;;
  postgres) required="DATABASE_URL PG_CA_FILE" ;;
  *)        die "DB_KIND must be mongo or postgres, got '${DB_KIND:-}'" ;;
esac
for v in $required; do eval "[ -n \"\${$v:-}\" ]" || die "missing env $v"; done

step "1/3 tools, installed on first run only"
install_java "$BASE"
[ "$DB_KIND" != mongo ] || install_chisel "$BASE"

step "2/3 the jar"
if [ -f "$BASE/src/app.jar" ]; then
  say "built on the agent"
  cp "$BASE/src/app.jar" "$BASE/app.jar"
else
  [ -f "$BASE/src/pom.xml" ] || die "no sources in $BASE/src"
  (cd "$BASE/src" && mvn -q -B -DskipTests package) >> "$BASE/log/build.log" 2>&1 ||
    { tail -40 "$BASE/log/build.log" >&2; die "build failed"; }
  # The runnable jar, not the ones Maven writes alongside it.
  jar=$(ls -t "$BASE"/src/target/*.jar 2>/dev/null | grep -v -e '/original-' -e '-sources\.jar$' -e '-javadoc\.jar$' | head -1)
  [ -n "$jar" ] || die "no jar in src/target"
  cp "$jar" "$BASE/app.jar"
fi

step "3/3 starting the app on :8080"
# The tunnel to MongoDB: restarted when its URL changes, stopped for Postgres.
if running "$BASE/chisel.pid" && { [ "$DB_KIND" = postgres ] || [ "$(cat "$BASE/chisel.url" 2>/dev/null)" != "$MONGO_TUNNEL_URL" ]; }; then
  kill "$(cat "$BASE/chisel.pid")"; sleep 1
fi
if [ "$DB_KIND" = mongo ] && ! running "$BASE/chisel.pid"; then
  say "starting chisel client -> $MONGO_TUNNEL_URL"
  echo "$MONGO_TUNNEL_URL" > "$BASE/chisel.url"
  AUTH="$CHISEL_USER:$CHISEL_PASSWORD" nohup "$BASE/bin/chisel" client "$MONGO_TUNNEL_URL" \
    127.0.0.1:27017:127.0.0.1:27017 >> "$BASE/log/chisel.log" 2>&1 < /dev/null &
  echo $! > "$BASE/chisel.pid"
  sleep 1
  running "$BASE/chisel.pid" || { tail -30 "$BASE/log/chisel.log" >&2; die "chisel client exited"; }
fi

if running "$BASE/app.pid"; then kill "$(cat "$BASE/app.pid")"; sleep 2; fi
# From src: PG_CA_FILE is relative to it.
(cd "$BASE/src" && HOST=0.0.0.0 PORT=8080 nohup java -Xmx256m -jar "$BASE/app.jar" >> "$BASE/log/app.log" 2>&1 < /dev/null &
 echo $! > "$BASE/app.pid")
i=0
until curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; do
  i=$((i + 1))
  running "$BASE/app.pid" || { tail -30 "$BASE/log/app.log" >&2; die "app exited"; }
  [ $i -lt 60 ] || { tail -30 "$BASE/log/app.log" >&2; die "app not healthy after 60s"; }
  sleep 1
done
say "ready (pid $(cat "$BASE/app.pid"))"
