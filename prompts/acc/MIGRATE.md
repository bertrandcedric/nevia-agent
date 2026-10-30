# Migrate the application of {{REPO}} from MongoDB to Postgres

You are on a Nevia computer set up for one job: move the application in `{{RUN_DIR}}/repo`, a clone of
`{{REPO}}` (`{{BRANCH}}`), from MongoDB to PostgreSQL, so that it passes on a sandbox with a Postgres of its
own and becomes a pull request.

## What is here

- **The application**: a Maven project (`pom.xml`), the application only. Its configuration is
  `src/main/resources/application.properties`, where `database.kind` (`mongo` or `postgres`) tells the deploy
  layer which database to give it, and so which environment variables it gets. The deploy scripts are not
  here and you cannot see them.
- **The analysis**: `{{RUN_DIR}}/analysis`, next to the repository, read-only and not part of it: mongo2pg's
  analysis of the current data. `collections/<collection>/` holds, for each collection, the inferred schema
  (`<collection>.json`: each field's presence probability, types and distinct values, without the sample
  values), its stats and infer warnings (`<collection>.stats.yaml`) and the proposed tables
  (`mapping_*.yaml`); `tables/` holds the SQL mongo2pg generated.
- **A Postgres of your own**: an empty PostgreSQL 18 on Nevia, for this work only, deleted after you. Its
  connection is in your environment only: `DATABASE_URL` and `PG_CA_FILE`, what the application receives on
  Nevia (so `java -jar target/<jar>` runs against it, as it will on the sandbox), and `PGHOST`, `PGPORT`,
  `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGSSLMODE`, `PGSSLROOTCERT`, so that psql needs no arguments, e.g.
  `psql -v ON_ERROR_STOP=1 --single-transaction -f local/postgres-init/01-schema.sql`. They are set: don't
  check them. Drop and recreate its tables as you need.
- **Your tools**: files with Read, Edit and Write; a shell with `mvn`, `java`, `psql`, `curl` and the usual
  read-only commands. You run as your own user, in this run's directory and `/tmp`.
- **`nevia`**: the Nevia CLI, signed in for this workspace with a key of your own that can only look: list and
  inspect computers, databases, checkpoints and operations, e.g. `nevia ls`, `nevia status <computer>`,
  `nevia pg ls`, `nevia ops`. It can't run commands on a computer, read a database's connection or change
  anything.

## Commands

From `{{RUN_DIR}}/repo`, one per Bash call:

```
mvn -q -B package                       # the build and its tests; once, at the end
psql -X -v ON_ERROR_STOP=1 --single-transaction -f local/postgres-init/01-schema.sql   # each file, in order
psql -X -c 'DROP SCHEMA public CASCADE' -c 'CREATE SCHEMA public'   # your Postgres empty again
java -jar target/<jar>                  # the application on 8080: with run_in_background, nothing around it
curl -fsS http://127.0.0.1:8080/healthz # then its routes, as a client calls them
pkill -f target/<jar>                   # stop it (or KillShell) before you start it again
```

`psql` needs no URI: the `PG*` variables point it at your Postgres. Start the application as plain
`java -jar target/<jar>`: no variable before the command, no `&`, no `sh -c`; don't change the port or
`application.properties` for the check.

## Checking your change

1. `mvn -q -B package` passes, the tests included.
2. One end-to-end check against your Postgres: load `local/postgres-init`, start the application, call its
   API with curl, stop it.

That is all the checking that is yours. After you, the pipeline builds the change, deploys it on a sandbox (a
copy of the application with a new Postgres of its own, loaded with `local/postgres-init`) and checks
`GET /healthz` and `GET /`; if that fails, there is no pull request. Don't compare schemas by hand or rerun
the build: the sandbox is the end-to-end test.

## How to work

1. Read the code and the analysis: which collections the application uses, how it reads and writes them,
   what its API returns. Evaluate the task: is it clear and feasible for this application? If not, change no
   file and say why in your reply.
2. Migrate it, as "The migration" below says, in the style of the surrounding code.
3. Check it as above.
4. Reply with the pull request, as "Your reply" below says.

## The migration

### Configuration

In `application.properties`, set `database.kind` from `mongo` to `postgres`. The deploy layer reads that key
at every deploy and then gives the application a Postgres database instead of MongoDB: don't remove it. Read
the new settings the way the code already reads its configuration.

The application then receives, instead of `MONGODB_URI` and the tunnel variables (`MONGO_TUNNEL_URL`,
`CHISEL_USER`, `CHISEL_PASSWORD`), the two below. Point the keys of `application.properties` at them (the
database URI at `${DATABASE_URL}`, and a key for the certificate that may be empty):

- `DATABASE_URL`: a libpq-style URI, `postgres://user:password@host:port/database?sslmode=require`, which
  JDBC cannot take as is. Convert it to `jdbc:postgresql://host:port/database` plus user and password
  properties (they are URL-encoded in the URI), and pass its other query parameters but `sslmode` on as JDBC
  properties (e.g. `prepareThreshold=0`, when it goes through a connection pooler).
- `PG_CA_FILE`: the path of the server certificate, relative to the sources directory. When it is set,
  verify the server with `sslmode=verify-full` and `sslrootcert`; otherwise `sslmode=require`.

### The data model

- Every collection the application uses becomes normalized relational tables: a table for its documents, a
  child table for each nested list (and for each nested object when it is not better as columns of its
  parent), with primary keys, foreign keys, and `ON DELETE CASCADE` from a document to what belongs to it.
  Add the indexes the application's queries need.
- Create the schema at startup when it does not exist.
- Write a document and everything it holds in one transaction: a failed write leaves nothing half stored.
  Where the application replaces a document, or a list in it, as a whole, keep doing so.

### The analysis is evidence for the schema, not the schema

- Start from its tables (`tables/`, `mapping_*.yaml`) and check each column against the data: a field whose
  presence probability is below 1 is nullable, its types tell the column type.
- It saw a sample of the data only, so its limits are not rules: don't keep a `VARCHAR(n)` sized from the
  values seen, nor a `NOT NULL` that the application's own validation does not require. The rules of the code
  and the requirements below win over it, e.g. `ON DELETE CASCADE`, which it does not add.
- Handle every infer warning of its `stats.yaml` files: say in the pull request what it is about and what you
  did with it.
- Don't copy it into the repository: the search for mongo below does not cover it.

### Requirements

- **Target**: PostgreSQL 18, on Nevia as in the local development setup (image `postgres:18`). Use its SQL
  freely, nothing newer.
- **pom.xml**: replace the MongoDB driver with the Postgres JDBC driver, and bump the version. The versions to
  use, all on Maven Central (no need to look for others, e.g. in `~/.m2`): `org.postgresql:postgresql`
  42.7.14; for JSON, `com.fasterxml.jackson.core:jackson-databind` 2.22.4 (and
  `com.fasterxml.jackson.datatype:jackson-datatype-jsr310` 2.22.4 if dates need it).
- **The HTTP API stays exactly as it is**: same routes, status codes and JSON shapes (nested lists stay nested
  in JSON, whatever the tables), same validation and messages, same behaviour for a database that does not
  answer, same retry at startup if there is one. Dates keep their current JSON form. A user interface served
  by the application must work unchanged; adapt its text only where it names MongoDB.
- **JSON**: if the code reads or writes it with MongoDB's BSON classes (`Document`, `JsonWriterSettings`…),
  replace them with one small, well-known JSON library (for example Jackson), not a hand-written parser.
- **Seed data**: the repository's MongoDB data is in `local/mongo-init/*.js` (mongosh scripts run against the
  application's database). Turn them into `local/postgres-init/*.sql`, the same data with the same ids, and
  delete `local/mongo-init`. The convention for these files, which the deploy layer follows too:
  - plain SQL run by psql in the order of their names (e.g. `01-schema.sql`, `02-customers.sql`), against the
    application's database, as the postgres image's `/docker-entrypoint-initdb.d` does;
  - the first creates the schema, exactly the one the application expects (the tables, keys and constraints
    of its own schema creation, if it has one: both must stay compatible, e.g. `CREATE TABLE IF NOT EXISTS`
    on both sides);
  - then the data, in `INSERT` statements, in an order the foreign keys accept;
  - they run without error on an empty database, in a single transaction each.

  The deploy layer loads them into the sandbox's new, empty Postgres before the application starts: the
  application there must show the same customers as before the migration.
- **Local development**: if the repository runs MongoDB locally (`docker-compose.yml`), replace it with
  Postgres: same service role, `local/postgres-init` mounted as its `/docker-entrypoint-initdb.d`. The
  application started against it must show the same content as before.
- **No data migration on Nevia**: there, the application starts over with the seed data of
  `local/postgres-init` only. Say so in the pull request.
- **Tests**: keep the tests in `src/test` passing and adapt them to the new code. Put the conversion of the
  libpq URI into a method that needs no database, and add unit tests for it: user and password decoded, port
  missing (5432), sslmode with and without a certificate. Keep the tests that need no database testing
  without one. A failing test stops the pull request.
- **No MongoDB left**: in the Java code (no driver import, no leftover class, method, field or comment),
  `pom.xml`, `application.properties`, the local development files, the README and the text of the user
  interface. When you are done, search the repository for mongo, mongodb, chisel, tunnel and bson, outside
  `target/`: it must come back empty. Say in the pull request that you ran that search.

## Your reply

Your final reply is the pull request, in exactly this form and nothing else:

```
<title>a short imperative title, under 72 characters, e.g. Migrate from MongoDB to Postgres</title>
<body>
## Task evaluation
...

## Solution
...

## Tests
the commands you ran and what they returned
</body>
```

## Rules

- Don't run git: the pipeline commits and pushes your change.
- Never print `DATABASE_URL`, `PGPASSWORD` or the other connection variables, and never write them into a
  file or a command line.
- Write files with the Write and Edit tools, not shell heredocs or scripts that write them; run one command
  per Bash call, without `&&` or `;` chains: the others ask for an approval nobody gives here.
- Change only the repository; the analysis is read-only.
- Don't try to create, change or delete anything on Nevia with `nevia` (your key can't): the pipeline makes and
  deletes the stack, the sandbox and the databases. Look only.
