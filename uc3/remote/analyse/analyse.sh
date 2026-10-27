#!/bin/sh
# Step 3, on the lab computer: mongo2pg and mongosh (release binaries), init + infer + export of
# the database, CONTEXT.md for Claude, a snapshot of the project and the documents of each
# collection then (each run starts from them).
#   sh analyse/analyse.sh
# MongoDB is started first, if the computer slept since seed (nothing else writes to it: the data
# analysed is the data migrated).
STEP_CMD=analyse
. "$(dirname "$0")/../setup/env.sh"

key_on
step "1/4 mongo2pg ($M2P_TAG), mongosh ($MONGOSH_TAG) and the tools, installed on first run only"
install
say "$(mongo2pg --version), mongosh $("$LAB/mongosh/mongosh" --version)"
mongo_up
srcmongo --eval 'db.runCommand({ ping: 1 }).ok' >/dev/null || die "MongoDB not reachable at MONGO_URI (database $MONGO_DB)"
nv pg awake "$PG_NAME" --wait >/dev/null 2>&1 || true

step "2/4 mongo2pg init + infer of the database $MONGO_DB"
if [ ! -f "$CONFIG" ]; then
  # The target is the main database; nvpg writes a config per branch.
  uri=$(nv pg connect --uri "$PG_NAME")
  (umask 077 && mongo2pg init --project-base "$LAB/migration" --project-name "$NAME" --namespace "$MONGO_DB" \
    --source-uri "$MONGO_URI" --target-uri "$uri" >/dev/null)
  unset uri
  # The empty collections out of the project: no table, nothing to map.
  empty=$(srcmongo --eval 'print(JSON.stringify(db.getCollectionNames().filter(c => db.getCollection(c).countDocuments() === 0).sort()))')
  say "excluding $(printf '%s' "$empty" | jq length) empty collections"
  awk -v ex="exclude = $empty" '{ print } /^\[source\]/ { print ex }' "$CONFIG" > "$CONFIG.new" && mv "$CONFIG.new" "$CONFIG"
fi
db=$(nv pg connect --uri "$PG_NAME" | sed -E 's#^[^/]*//[^/]*/([^?]*).*#\1#')
mongo2pg infer -c "$CONFIG" --database-name "$db" --schema-name "$NAME"

step "3/4 mongo2pg export (CSV of every table)"
mongo2pg export -c "$CONFIG" || say "warning: mongo2pg export failed: Claude will look at it"
# report exits non-zero when there are infer warnings, once the report is written.
mongo2pg report -c "$CONFIG" >/dev/null 2>&1 || [ -s "$PROJECT/reports/main.html" ] || say "warning: mongo2pg report failed"

step "4/4 context for Claude: CONTEXT.md and the mongo2pg documentation"
counts > "$LAB/tmp/counts.json"
mkdir -p "$PROJECT/context/docs"
for d in README.md USAGE.md docs/reference/README.md docs/how-to/README.md docs/tutorial/README.md; do
  f=$(printf '%s' "$d" | sed 's#^docs/##; s#/README.md$#.md#')
  fetch "https://raw.githubusercontent.com/$M2P_REPO/$M2P_TAG/$d" "$PROJECT/context/docs/$f" ||
    say "warning: no $d in mongo2pg $M2P_TAG"
done
node "$SCRIPTS/analyse/context.mjs" "$PROJECT" "$LAB/tmp/counts.json" "$PROJECT/context/CONTEXT.md" "$CONFIG" "$NAME"
{ printf '## mongo2pg commands\n\n```\n'
  for c in to-pg export import report; do echo "\$ mongo2pg $c --help"; mongo2pg $c --help; echo; done
  printf '```\n'; } >> "$PROJECT/context/CONTEXT.md"
(umask 077 && tar -czf "$PRISTINE" -C "$LAB/migration" "$NAME")
analyse_sum > "$PRISTINE.sum"
mv "$LAB/tmp/counts.json" "$COUNTS"
say "project: $PROJECT, context: $PROJECT/context/CONTEXT.md ($(wc -l < "$PROJECT/context/CONTEXT.md") lines)"
