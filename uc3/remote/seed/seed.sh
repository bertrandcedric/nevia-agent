#!/bin/sh
# Step 2, on the lab computer: MongoDB started (installed the first time), the customers of seed.mjs
# in its database hello (the collection replaced), then the documents of each collection.
#   sh seed/seed.sh
# Env: SEED_CUSTOMERS, SEED, SEED_STRANGE, SEED_STRANGE_KINDS (seed.mjs).
STEP_CMD=seed
. "$(dirname "$0")/../setup/env.sh"

key_on
step "1/2 MongoDB, mongosh and the tools, installed on first run only"
install
mongo_up

step "2/2 generating the customers of $MONGO_DB"
MONGO_URI=$MONGO_URI MONGO_DB=$MONGO_DB node "$SCRIPTS/seed/seed.mjs"
srcmongo --eval 'db.getCollectionNames().sort().forEach(c => print(`  ${c}: ${db.getCollection(c).countDocuments()}`))'
