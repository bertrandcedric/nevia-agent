-- The post-import fixes of mongo2pg issues met before that no mapping can avoid (generic: any project;
-- objectid-rendered-as-uuid, binary-exported-as-decimal-array), for the schema {{SCHEMA}}. From
-- migrate/fix-data.sql, copied into the work directory at each run. Run it after each import,
-- on the same database:
--   nvpg psql try<N> -f {{WORK}}/fix-data.sql
-- Each fix touches only the values of the exact wrong shape, and says how many it changed: 0 means
-- the issue did not show in this export (fixed in mongo2pg, or no such column).

BEGIN;

SET CONSTRAINTS ALL DEFERRED;

-- objectid-rendered-as-uuid: export rewrites every 24-hex id value of a key column into UUID
-- text (MongoDB 6ae05d95069a4ba3bc18df4d, CSV 00000000-6ae0-5d95-069a-4ba3bc18df4d), whatever
-- the column's type. This restores the source form in every text column named id or *_id. No
-- source id has a hyphen, so the match is unambiguous; parent and child columns change in one
-- transaction, the foreign keys (DEFERRABLE INITIALLY DEFERRED) are checked again at COMMIT.
DO $$
DECLARE
    r        record;
    n        bigint;
    total    bigint := 0;
    touched  int := 0;
BEGIN
    FOR r IN
        SELECT c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = '{{SCHEMA}}'
          AND t.table_type = 'BASE TABLE'
          AND c.data_type IN ('text', 'character varying')
          AND (c.column_name = 'id' OR c.column_name LIKE '%\_id')
        ORDER BY c.table_name, c.column_name
    LOOP
        EXECUTE format(
            'UPDATE %I.%I SET %I = substr(replace(%I, ''-'', ''''), 9) '
            'WHERE %I ~ ''^00000000-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$''',
            '{{SCHEMA}}', r.table_name, r.column_name, r.column_name, r.column_name);
        GET DIAGNOSTICS n = ROW_COUNT;
        IF n > 0 THEN
            touched := touched + 1;
            total := total + n;
            RAISE NOTICE '{{SCHEMA}}.%.%: % value(s)', r.table_name, r.column_name, n;
        END IF;
    END LOOP;
    RAISE NOTICE 'objectid-rendered-as-uuid: % id value(s) restored in % column(s)', total, touched;
END $$;

-- binary-exported-as-decimal-array: a BSON Binary mapped to BYTEA is exported as the text of a
-- decimal byte list, so PostgreSQL stores the ASCII of "[137,80,78,71,...]" instead of the bytes.
-- This rebuilds the bytes of every BYTEA column whose value has exactly that shape. The shape is
-- read with encode(.., 'escape'), which never fails: real binary data is not valid UTF-8, and
-- shows there as \ooo escapes that the pattern does not match.
DO $$
DECLARE
    r     record;
    n     bigint;
    total bigint := 0;
BEGIN
    FOR r IN
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = '{{SCHEMA}}' AND data_type = 'bytea'
        ORDER BY table_name, column_name
    LOOP
        EXECUTE format(
            'UPDATE %1$I.%2$I SET %3$I = ('
            '  SELECT decode(string_agg(lpad(to_hex(v::int), 2, ''0''), '''' ORDER BY ord), ''hex'')'
            '  FROM unnest(string_to_array(btrim(encode(%3$I, ''escape''), ''[]''), '',''))'
            '       WITH ORDINALITY AS t(v, ord))'
            ' WHERE encode(%3$I, ''escape'') ~ ''^\[[0-9]+(,[0-9]+)*\]$''',
            '{{SCHEMA}}', r.table_name, r.column_name);
        GET DIAGNOSTICS n = ROW_COUNT;
        IF n > 0 THEN
            total := total + n;
            RAISE NOTICE '{{SCHEMA}}.%.%: % binary value(s)', r.table_name, r.column_name, n;
        END IF;
    END LOOP;
    RAISE NOTICE 'binary-exported-as-decimal-array: % binary value(s) rebuilt', total;
END $$;

COMMIT;
