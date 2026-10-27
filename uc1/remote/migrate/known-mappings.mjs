#!/usr/bin/env node
// The mapping fixes of the known mongo2pg issues, in the mapping_*.yaml of the project (from
// migrate/known-mappings.mjs, copied into the work directory at each run). A sed over all the
// files would do some of it, but Claude Code refuses a glob in a write command.
//   node known-mappings.mjs [--jsonb <collection>:<mongo path>]... [--project DIR] [--dry-run]
// Always, in every mapping:
// - varchar-sized-from-sample: data_type varchar(n) -> text, sql_type VARCHAR(n) -> TEXT
// - objectid-id-typed-as-uuid, gen-random-uuid-schema-qualified: data_type
//   uuid default public.gen_random_uuid() -> text, sql_type UUID DEFAULT public.gen_random_uuid() -> TEXT
// With --jsonb rocketchat_message:.md (once per subtree): the subtree as one JSONB column of the
// table of its parent path. The mappings at that path and below are deleted, with their CSVs,
// and the column (named after the path's last field) is added to the parent's mapping, if it
// has none of that name. A parent path that no mapping has is an error.
// Prints what each fix changed: 0 means the issue did not show. Then regenerate (to-pg, export).

import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const PROJECT = opt('project', '{{PROJECT}}');
const DRY = args.includes('--dry-run');
const JSONB = args.flatMap((a, i) => (a === '--jsonb' && args[i + 1] ? [args[i + 1]] : []));

const FIXES = [
  { id: 'varchar-sized-from-sample',
    rules: [[/^( *data_type: )varchar\(\d+\)\s*$/gim, '$1text'], [/^( *sql_type: )VARCHAR\(\d+\)\s*$/gim, '$1TEXT']] },
  { id: 'objectid-id-typed-as-uuid',
    rules: [[/^( *data_type: )uuid default public\.gen_random_uuid\(\)\s*$/gim, '$1text'],
            [/^( *sql_type: )UUID DEFAULT public\.gen_random_uuid\(\)\s*$/gim, '$1TEXT']] },
];

const base = path.join(PROJECT, 'source', 'collections');
const mappingsOf = (dir) => fs.readdirSync(path.join(base, dir)).filter((f) => /^mapping_.*\.yaml$/.test(f)).sort()
  .map((f) => path.join(base, dir, f));
const field = (text, re) => (text.match(re) || [])[1];
const mongoPath = (text) => field(text, /^mongo_path:\s*(\S+)\s*$/m);
const tableName = (text) => field(text, /^  table_name:\s*(\S+)\s*$/m);
const write = (file, text) => { if (!DRY) fs.writeFileSync(file, text); };
const dry = DRY ? ' (dry run)' : '';

// --jsonb: each subtree as one JSONB column of its parent's table.
for (const spec of JSONB) {
  const m = spec.match(/^([^:]+):(\.\S+)$/);
  if (!m) { console.error(`--jsonb ${spec}: expected <collection>:<mongo path>, e.g. rocketchat_message:.md`); process.exit(2); }
  const [, coll, sub] = m;
  if (!fs.existsSync(path.join(base, coll))) { console.error(`--jsonb ${spec}: no collection ${coll} in the project`); process.exit(2); }
  const parentPath = sub.lastIndexOf('.') > 0 ? sub.slice(0, sub.lastIndexOf('.')) : '.';
  const name = sub.slice(sub.lastIndexOf('.') + 1);
  const files = mappingsOf(coll).map((f) => ({ f, text: fs.readFileSync(f, 'utf8') }));
  const parent = files.find((x) => mongoPath(x.text) === parentPath);
  if (!parent) { console.error(`--jsonb ${spec}: no mapping of ${coll} at the parent path ${parentPath}`); process.exit(1); }
  const gone = files.filter((x) => mongoPath(x.text) === sub || mongoPath(x.text).startsWith(sub + '.'));
  for (const x of gone) {
    if (!DRY) fs.unlinkSync(x.f);
    // Their CSVs, which export would not write again: data/<namespace>/<collection>/<table>.csv.gz.
    const data = path.join(PROJECT, 'data');
    for (const ns of fs.existsSync(data) ? fs.readdirSync(data) : []) {
      for (const c of [coll, coll.replace(/\./g, '_')]) {
        const csv = path.join(data, ns, c, `${tableName(x.text)}.csv.gz`);
        if (fs.existsSync(csv) && !DRY) fs.unlinkSync(csv);
      }
    }
  }
  const target = name.toLowerCase();
  let added = false;
  if (!new RegExp(`^  - source_field: ${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'm').test(parent.text)) {
    const col = `  - source_field: ${name}\n    target_field: ${target}\n    data_type: jsonb\n    nullable: true\n`;
    const ddl = `    - name: ${target}\n      sql_type: JSONB\n      nullable: true\n      primary_key: false\n`;
    // mongo2pg writes an empty list inline (`columns: []`, run 261027123409): it becomes a block
    // list with the column; otherwise the column goes at the end of the list.
    let text = parent.text;
    if (/^  columns: \[\]\s*$/m.test(text)) text = text.replace(/^  columns: \[\]\s*$/m, '  columns:\n' + col.trimEnd());
    else if (/^  ddl:\n/m.test(text)) text = text.replace(/^  ddl:\n/m, col + '  ddl:\n');
    else { console.error(`--jsonb ${spec}: ${path.relative(base, parent.f)} has no "  columns:" list before "  ddl:"`); process.exit(1); }
    if (/^    columns: \[\]\s*$/m.test(text)) text = text.replace(/^    columns: \[\]\s*$/m, '    columns:\n' + ddl.trimEnd());
    else if (/^    foreign_keys:/m.test(text)) text = text.replace(/^    foreign_keys:/m, ddl + '    foreign_keys:');
    else { console.error(`--jsonb ${spec}: ${path.relative(base, parent.f)} has no "    columns:" list before "    foreign_keys:"`); process.exit(1); }
    write(parent.f, text);
    added = true;
  }
  console.log(`jsonb ${coll}:${sub}: ${gone.length} mapping(s) deleted (${gone.map((x) => tableName(x.text)).join(', ') || 'none'}), ` +
    `column ${tableName(parent.text)}.${target} ${added ? 'added' : 'already there'}${dry}`);
}

// The fixes of every mapping.
const all = fs.readdirSync(base).filter((d) => fs.statSync(path.join(base, d)).isDirectory()).sort().flatMap(mappingsOf);
const stats = Object.fromEntries(FIXES.map((f) => [f.id, { columns: 0, files: [] }]));
for (const file of all) {
  const before = fs.readFileSync(file, 'utf8');
  let text = before;
  for (const fix of FIXES) {
    // A column is one data_type line (its sql_type line is the DDL of the same column).
    const n = (text.match(fix.rules[0][0]) || []).length;
    for (const [re, to] of fix.rules) text = text.replace(re, to);
    if (n > 0) {
      stats[fix.id].columns += n;
      stats[fix.id].files.push(path.relative(base, file));
    }
  }
  if (text !== before) write(file, text);
}
for (const fix of FIXES) {
  const s = stats[fix.id];
  const where = s.files.length > 6 ? `${s.files.slice(0, 6).join(', ')} and ${s.files.length - 6} more` : s.files.join(', ');
  console.log(`${fix.id}: ${s.columns} column(s)${dry}${s.files.length ? ' in ' + where : ''}`);
}
console.log(`${all.length} mapping files${DRY ? ', none written' : ''}; now: to-pg, then export`);
