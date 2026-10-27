# Known mongo2pg issues

The mongo2pg issues found by earlier runs of this lab, with how to see each one in the project and how
to fix it. Copied into the work directory at each run: Claude fixes those that show **before the
first attempt**, in one pass, then regenerates once (to-pg, export). The lab is there to find the
issues no one knows yet; these are known and have their fixes.

For each one, check first that it still shows (mongo2pg may have fixed it, the data may not have it):
fix only what you see. A known issue that still shows goes into `mongo2pg-issues.json` like any
other, with `"known": true` and `"first_seen": "known"`, so that the list of all runs tells which
ones mongo2pg still has. One that no longer shows is not recorded: say it in the report (section 4).

Found with mongo2pg 0.8.0 (`mongo2pg-v0.8.0`), Rocket.Chat 8.7 source, run 261027081842 (and
`varchar-sized-from-sample` in the review of run 261027095359: both runs had missed it;
`nested-shortcode-hoisted-to-parent` in run 261027110820).

| Id | Kind | Command | Fix | Seen in |
| --- | --- | --- | --- | --- |
| [gen-random-uuid-schema-qualified](#gen-random-uuid-schema-qualified) | postgres | to-pg | `known-mappings.mjs` | any project |
| [objectid-id-typed-as-uuid](#objectid-id-typed-as-uuid) | inference | infer | `known-mappings.mjs` | any project |
| [reserved-word-table-name](#reserved-word-table-name) | postgres | to-pg | mapping | any project |
| [avatars-sql-filename-dot-vs-underscore](#avatars-sql-filename-dot-vs-underscore) | bug | export | file names | collections with a dot |
| [array-of-objects-as-array-scalar](#array-of-objects-as-array-scalar) | inference | infer | mapping | any project |
| [objectid-rendered-as-uuid](#objectid-rendered-as-uuid) | bug | export | `known-fixes.sql` | any project |
| [binary-exported-as-decimal-array](#binary-exported-as-decimal-array) | bug | export | `known-fixes.sql` | BYTEA columns |
| [videoconf-settings-subtree-misrooted](#videoconf-settings-subtree-misrooted) | inference | infer | `known-mappings.mjs --jsonb` | rocketchat_statistics |
| [colliding-child-table-dropped](#colliding-child-table-dropped) | bug | infer | mapping | sibling sub-objects of the same shape |
| [unmapped-fields-dropped-silently](#unmapped-fields-dropped-silently) | inference | infer | mapping | any project |
| [ttl-collections-not-flagged](#ttl-collections-not-flagged) | feature | infer | none (the lab freezes the source) | collections with a TTL index |
| [varchar-sized-from-sample](#varchar-sized-from-sample) | inference | infer | `known-mappings.mjs` | any project |
| [nested-shortcode-hoisted-to-parent](#nested-shortcode-hoisted-to-parent) | bug | export | `known-mappings.mjs --jsonb` | nested arrays of objects (Rocket.Chat's `md`) |

The mechanical mapping fixes are in `known-mappings.mjs` (`node known-mappings.mjs`, then to-pg
and export); the post-import fixes are in `known-fixes.sql`: run it after each import, on the same database
(`nvpg psql try<N> -f known-fixes.sql`, then `nvpg psql -f known-fixes.sql` on the main one). It
prints how many values each fix changed.

## gen-random-uuid-schema-qualified

- **Kind, command:** postgres, `to-pg`
- **Observed:** the DDL has `id UUID DEFAULT public.gen_random_uuid() PRIMARY KEY`. import stops on
  the first such file: `function public.gen_random_uuid() does not exist (SQLSTATE 42883)`.
- **Expected:** `gen_random_uuid()` without a schema: it is built in (pg_catalog) since PostgreSQL 13.
- **See it:** `grep -rn 'public.gen_random_uuid' <project>/source/collections <project>/schema`
  (in run 261027081842: `rocketchat_cron`, `rocketchat_avatars.chunks`).
- **Fix:** with [objectid-id-typed-as-uuid](#objectid-id-typed-as-uuid), which is always its cause
  here: the column is `TEXT`, with no default (`node known-mappings.mjs` does both).
- **Suggestion for mongo2pg:** emit `gen_random_uuid()` unqualified (or `pg_catalog.`); if an
  extension is needed, `CREATE EXTENSION IF NOT EXISTS pgcrypto` without assuming `public`.

## objectid-id-typed-as-uuid

- **Kind, command:** inference, `infer`
- **Observed:** for a collection whose `_id` is a BSON ObjectId, infer maps `_id` to
  `UUID DEFAULT public.gen_random_uuid()`. An ObjectId (24 hex digits) is not a UUID, and a
  generated default would replace the source key with a random one.
- **Expected:** `TEXT` holding the 24-hex ObjectId, as infer does for the other collections, and no
  default on a column fed from the document.
- **See it:** the same grep as above; `srcmongo --eval 'db.getCollection("<c>").findOne()._id'`
  shows an `ObjectId(...)`.
- **Fix:** `node known-mappings.mjs` (in the work directory): in every mapping, `data_type`
  `uuid default public.gen_random_uuid()` to `text` and `sql_type` to `TEXT`. Check first that the
  `_id` it changes is an ObjectId or a string: a real UUID would keep `UUID`, without the default.
- **Suggestion for mongo2pg:** map ObjectId to `TEXT`/`CHAR(24)` (or `BYTEA`), never to `UUID`; never
  a generated default on a column that carries the source key.

## reserved-word-table-name

- **Kind, command:** postgres, `to-pg`
- **Observed:** `CREATE TABLE values (` unquoted: `VALUES` is reserved in PostgreSQL 18, a syntax
  error (to-pg already quotes reserved column names such as `"from"`, not table names).
- **Expected:** a name PostgreSQL accepts, quoted or prefixed with its parent table.
- **See it:** the table names of the DDL against the reserved words:
  `grep -h 'CREATE TABLE' <project>/schema/tables/*/*.sql`, compared with
  `psql -c "select word from pg_get_keywords() where catcode = 'R'"` (through `nvpg psql`). In run
  261027081842: `rocketchat_settings` → table `values`.
- **Fix:** in the mapping: `pg_mapping.table_name` and `pg_mapping.ddl.name` to
  `<parent>_<name>` (`settings_values`), the file renamed `mapping_<new name>.yaml`, the child
  tables that name it as `traversal.parent_table` changed too, its stale CSV deleted; then to-pg
  and export.
- **Suggestion for mongo2pg:** check generated table names against the reserved words, or quote
  every identifier.

## avatars-sql-filename-dot-vs-underscore

- **Kind, command:** bug, `export`
- **Observed:** for a collection with a dot in its name, to-pg writes `rocketchat_avatars.chunks.sql`
  but export looks for `rocketchat_avatars_chunks.sql`, warns `SQL schema not found for collection
  ...` and **skips the collection**: its rows are silently missing.
- **Expected:** the same file name rule in both commands; a missing schema file an error.
- **See it:** `ls <project>/schema/tables/*/ | grep '\..*\.sql$'`, or that warning in the output of
  export (in run 261027081842: `rocketchat_avatars.chunks`, `rocketchat_avatars.files`).
- **Fix:** **after every to-pg**, rename each such file with underscores
  (`mv .../rocketchat_avatars.chunks.sql .../rocketchat_avatars_chunks.sql`, without `-f`: an `mv`
  with flags needs an approval), then export.
- **Suggestion for mongo2pg:** one shared sanitising of the collection name (dots to underscores) in
  to-pg and export; turn the warning into an error.

## array-of-objects-as-array-scalar

- **Kind, command:** inference, `infer`
- **Observed:** an array of objects (`[{key, value}, ...]`) gets traversal mode `array_scalar` with
  a `key_column`: export writes each whole element as JSON into the value column and leaves the key
  empty, and import fails: `null value in column "key" ... violates not-null constraint (23502)`.
- **Expected:** mode `object`, each element's fields as the columns already describe them.
- **See it:** `grep -rln 'mode: array_scalar' <project>/source/collections`, then for each one,
  whether the elements are objects:
  `srcmongo --eval 'print(JSON.stringify(db.<c>.findOne({"<field>.0": {$type: "object"}}, {"<field>": 1})))'`
  (in run 261027081842: `rocketchat_server_events.data`, table `events_data`).
- **Fix:** in that mapping, `traversal.mode: array_scalar` → `object`; then to-pg and export.
- **Suggestion for mongo2pg:** choose `array_scalar` only when the elements are scalars (a key
  column already says they are objects).

## objectid-rendered-as-uuid

- **Kind, command:** bug, `export`
- **Observed:** export rewrites every 24-hex id value of a key column (primary key and traversal
  foreign key) into UUID text: `6ae05d95069a4ba3bc18df4d` becomes
  `00000000-6ae0-5d95-069a-4ba3bc18df4d`, whatever the column type (a `VARCHAR(24)` then
  overflows), while the same value in a data column or in JSONB keeps its form. 45 636 values in 56
  columns in run 261027081842.
- **Expected:** the id as MongoDB holds it.
- **See it:** `zcat <a CSV of the export> | head -3` shows `00000000-....` ids.
- **Fix:** no mapping avoids it: `known-fixes.sql` after the import restores them (every text column
  named `id` or `*_id`, only values of that exact shape). Keep key columns `TEXT`.
- **Suggestion for mongo2pg:** never reformat an id; a UUID rendering, if wanted, opt-in and
  applied to every occurrence of the value.

## binary-exported-as-decimal-array

- **Kind, command:** bug, `export`
- **Observed:** a BSON Binary mapped to `BYTEA` is written to the CSV as the text
  `[137,80,78,71,...]`: PostgreSQL stores the ASCII of that list, the data is destroyed (the
  15 127-byte avatar PNG of run 261027081842).
- **Expected:** BYTEA input format, `\x89504e47...`.
- **See it:** `grep -rln -i 'bytea' <project>/source/collections`; after an import,
  `nvpg psql try<N> -c "select left(encode(<col>, 'escape'), 20) from <schema>.<table> limit 1"`
  shows `[137,80,...`.
- **Fix:** `known-fixes.sql` after the import rebuilds the bytes of every BYTEA value of that shape.
- **Suggestion for mongo2pg:** write BYTEA as `\x` hex in the CSV.

## videoconf-settings-subtree-misrooted

- **Kind, command:** inference, `infer`
- **Observed:** 12 child tables rooted at `.videoConf.settings.*` (accounts, e2ee, email, fileUpload,
  general, message, push, search, threads, email.smtp, general.nps, general.update), a path that
  exists in no document; the real subtree is the root field `.settings`, which no mapping covered
  (with `liveStreamAndBroadcasting` and `webRTC`). The tables load 0 rows, the data is dropped.
- **Expected:** tables at the paths the fields occupy; none for a path that matches no document.
- **See it:** `grep -H 'mongo_path' <project>/source/collections/rocketchat_statistics/mapping_*.yaml`,
  against `srcmongo --eval 'const s = db.rocketchat_statistics.findOne(); print(Object.keys(s.videoConf.settings), Object.keys(s.settings))'`.
  More generally: a child table whose `mongo_path` matches no document.
- **Careful:** `.videoConf.settings` itself **does exist** (`{provider, dms, channels, groups, teams}`,
  table `videoconf_settings`): only the 12 tables *below* it are wrong. Run 261027110820 deleted
  `videoconf_settings` too, and lost `videoConf.settings.provider` without any check failing.
- **Fix:** both subtrees as JSONB, in one command:
  `node known-mappings.mjs --jsonb rocketchat_statistics:.videoConf.settings --jsonb rocketchat_statistics:.settings`.
  The first deletes `videoconf_settings` and the 12 tables below it (with their CSVs) and gives
  `videoconf` a JSONB column `settings` that keeps all of it; the second gives
  `rocketchat_statistics` a JSONB column `settings` for the real subtree. Then to-pg and export.
- **Suggestion for mongo2pg:** child paths from the paths actually walked; report a table whose path
  matches no document.

## colliding-child-table-dropped

- **Kind, command:** bug, `infer`
- **Observed:** `workHours[].start` and `workHours[].finish` have the same shape
  `{time, cron: {...}, utc: {...}}`; infer makes `start_cron` and `start_utc` but nothing for
  `finish.cron` and `finish.utc`: their values have no mapping (the infer warnings name them).
- **Expected:** `finish_cron` and `finish_utc` beside `start_cron` and `start_utc`.
- **See it:** `unmapped.mjs` lists `workHours.finish.cron` and `.utc` under
  `rocketchat_livechat_business_hours`. More generally: two sibling sub-objects of the same shape,
  the second one's children unmapped.
- **Fix:** columns `cron` and `utc` (JSONB) on the existing `finish` table (`pg_mapping.columns`
  and `pg_mapping.ddl.columns`); then to-pg and export.
- **Suggestion for mongo2pg:** prefix the child table with its parent (as for `start_cron`) instead
  of dropping the second one.

## unmapped-fields-dropped-silently

- **Kind, command:** inference, `infer`
- **Observed:** fields present in the documents that no mapping covers are dropped by export without
  a warning. With data in run 261027081842: `rocketchat_message.starred`, `.channels`,
  `.mentions[]._id`, `.urls[].meta`, `.attachments[].attachments`, `rocketchat_room.u` (the room's
  owner), `rocketchat_room.lastMessage.starred`, `.channels`, `.mentions[]._id`, `.urls[].meta`,
  `rocketchat_statistics.apps`, `.services.ldap.backgroundSync`, `.services.ldap.ee`,
  `.services.oauth.custom`, `rocketchat_notification_queue.sending`, `.items[].data.headers`,
  `rocketchat_cron.data`, `users.settings`. 50 more held only null or `[]`.
- **Expected:** every field seen either mapped, or reported as deliberately skipped.
- **See it:** `node unmapped.mjs` (in the work directory): every uncovered field, its types, a sample.
- **Fix:** one column per field with data, on the table of its parent: JSONB for an object or an
  array, `TEXT` for an id, `TIMESTAMPTZ` for a date (`pg_mapping.columns` and
  `pg_mapping.ddl.columns`); then to-pg and export. Leave the always-null and always-empty ones,
  and list them in the issue. A subtree is simplest as JSONB:
  `node known-mappings.mjs --jsonb <collection>:<path>`. By hand, mind the mappings whose list is
  written `columns: []`: it becomes `columns:` with the item below it, or to-pg fails (`did not
  find expected key`).
- **Suggestion for mongo2pg:** list per collection the fields seen but not mapped, and why.

## ttl-collections-not-flagged

- **Kind, command:** feature, `infer`
- **Observed:** `instances` (TTL 60 s on `_updatedAt`) and `rocketchat_notification_queue` (TTL
  7 200 s on `ts`) lost documents between analyse and the run, while Rocket.Chat was stopped: the
  counts of CONTEXT.md were stale, and nothing in mongo2pg says a collection expires.
- **Expected:** a note that a collection has a TTL index, and the time each count was taken.
- **See it:** `srcmongo --eval 'db.getCollectionNames().forEach(c => db.getCollection(c).getIndexes().filter(i => i.expireAfterSeconds !== undefined).forEach(i => print(c, JSON.stringify(i.key), i.expireAfterSeconds)))'`
- **Fix:** none in the project. The lab now turns MongoDB's TTL expiry off from analyse to the end
  of the run (the Rocket.Chat source's `before-run`), and analyses again when the source's counts
  changed since: the counts of CONTEXT.md are those of the run. Record it again only if a count
  still moves during the run.
- **Suggestion for mongo2pg:** read the indexes during infer, flag `expireAfterSeconds`, stamp the
  counts with their time.

## varchar-sized-from-sample

- **Kind, command:** inference, `infer`
- **Observed:** string fields get a `VARCHAR(n)` sized from the values infer saw, rounded up to a
  bucket: 142 columns in run 261027095359 (103 `VARCHAR(20)`, 17 `VARCHAR(5)`, 16 `VARCHAR(1)`, 5
  `VARCHAR(4)`, 1 `VARCHAR(3)`), among them `rocketchat_message.title`, `author_name`, `color`,
  `tmid` and user ids. The data of the run fits, so the load and the comparisons pass; the next
  longer value (a message title of 21 characters) makes an insert fail.
- **Expected:** `TEXT`, unless the source or the application has a real limit (MongoDB has none).
- **See it:** `grep -rci 'varchar(' <project>/source/collections` (a `data_type: varchar(n)` in
  `pg_mapping.columns` and a `sql_type: VARCHAR(n)` in `pg_mapping.ddl.columns` per column);
  `verify.mjs` fails its `ddl` check while one is left.
- **Fix:** every one to `TEXT` with `node known-mappings.mjs` (in the work directory), then to-pg
  and export. Not with `sed -i` over `*/mapping_*.yaml` nor `find -exec`: Claude Code refuses both
  (run 261027105943). Record it with the number of columns it changed, not each one; section 4
  of the report: one row for all.
- **Suggestion for mongo2pg:** `TEXT` for strings by default; a `VARCHAR(n)` only on request, and
  then from a limit of the data model, never from the longest value of a sample.

## nested-shortcode-hoisted-to-parent

- **Kind, command:** bug, `export`
- **Observed:** in an array of objects nested in another one, export writes a field of a child
  element on the row of its parent element. Rocket.Chat's parsed messages (`md`): the `shortCode`
  of an EMOJI inside a BOLD element (`md[].value[].value[].shortCode`) lands on the row of the BOLD
  element in table `value` (and `md_value` for `rocketchat_room.lastMessage.md`), whose own element
  has none: 7 rows in run 261027110820 (e.g. `eyes` on message `9KThx9QCwvbFX4ABN`).
- **Expected:** a row's columns from its own element only; `shortcode` NULL on the BOLD row.
- **See it:** only in the documents that have such nesting, so a random sample may miss it (try1
  of run 261027110820 passed, main failed):
  `srcmongo --eval 'print(db.rocketchat_message.countDocuments({"md.value.value.shortCode": {$exists: true}}))'`;
  `verify.mjs` also samples the documents of each child table's path, which finds it.
- **Fix:** the whole `md` tree as JSONB, for the messages and for the rooms' last message:
  `node known-mappings.mjs --jsonb rocketchat_message:.md --jsonb rocketchat_room:.lastMessage.md`.
  It deletes the tables `md`, `value`, `lastmessage_md`, `md_value` (and their CSVs) and adds a
  JSONB column `md` to `rocketchat_message` and to `lastmessage`; then to-pg and export. `md` is a
  parse tree of the message, read as a whole: as JSON it is exact, as tables it was 4 tables and
  this bug. (Run 261027110820 cleared the wrong `shortcode` values with SQL after the import
  instead: an heuristic on `md`'s format, not kept.)
- **Suggestion for mongo2pg:** resolve a column's source field at the element's own level only,
  never from its descendants.
