#!/bin/sh
# Step 1, on the lab computer (after lab.sh setup made it): the tools (MongoDB, mongo2pg, mongosh,
# nvpg, the node packages, psql), installed once, and MongoDB started, empty until seed.
#   sh setup/setup.sh
STEP_CMD=setup
. "$(dirname "$0")/env.sh"

key_on
install
mongo_up
say "$(mongo2pg --version), mongosh $("$LAB/mongosh/mongosh" --version), MongoDB $MONGO_VERSION on 127.0.0.1:27018"
