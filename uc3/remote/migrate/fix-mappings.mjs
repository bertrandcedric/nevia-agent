#!/usr/bin/env node
// The mapping fixes of mongo2pg issues met before (generic: any project), in the mapping_*.yaml
// of the project (from migrate/fix-mappings.mjs, copied into the work directory at each run). A sed over all the
// files would do some of it, but Claude Code refuses a glob in a write command.
//   node fix-mappings.mjs [--jsonb <collection>:<mongo path>]... [--project DIR] [--dry-run]
// Always, in every mapping:
// - varchar-sized-from-sample: data_type varchar(n) -> text, sql_type VARCHAR(n) -> TEXT
// - objectid-id-typed-as-uuid, gen-random-uuid-schema-qualified: data_type
//   uuid default public.gen_random_uuid() -> text, sql_type UUID DEFAULT public.gen_random_uuid() -> TEXT
// From profile.json (every document, not infer's sample; profile.mjs is run first when there is
// none), in every mapping:
// - not-null-from-sample: NOT NULL on a column that is not a key, whose field some document (or
//   element) lacks or holds null -> nullable (infer saw it in every document of its sample)
// - mixed-types: a column whose scalar values mix types (a date and a string, a bool and a
//   string...) -> TEXT, a list column whose scalar elements do -> TEXT[]. The tables and columns
//   stay those mongo2pg proposed: no JSONB is added. Values mixing objects or lists are left.
// - mixed-elements: the child tables of a list that mixes objects and scalars, reported only.
// With --jsonb (not for uc3, which keeps the structure of the analysis: for another project) <collection>:.<field> (once per subtree): the subtree as one JSONB column of the
// table of its parent path. The mappings at that path and below are deleted, with their CSVs,
// and the column (named after the path's last field) is added to the parent's mapping, if it
// has none of that name. A parent path that no mapping has is an error.
// --no-profile: the fixes that need no profile only.
// Prints what each fix changed: 0 means the issue did not show. Then regenerate (to-pg, export).

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import YAML from 'yaml';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const PROJECT = opt('project', '{{PROJECT}}');
const DRY = args.includes('--dry-run');
const JSONB = args.flatMap((a, i) => (a === '--jsonb' && args[i + 1] ? [args[i + 1]] : []));

// The profile of the source: profile.json, made by profile.mjs here if there is none yet.
let PROFILE = null;
if (!args.includes('--no-profile')) {
  const file = path.join(HERE, 'profile.json');
  if (!fs.existsSync(file)) {
    console.log('profile.json: none yet, node profile.mjs first');
    execFileSync(process.execPath, [path.join(HERE, 'profile.mjs'), '--project', PROJECT], { stdio: ['ignore', 'ignore', 'inherit'] });
  }
  PROFILE = JSON.parse(fs.readFileSync(file, 'utf8'));
}
// The type families of the values at a dotted path (lists' [] dropped), or of their elements.
const FAMILY = { number: 'numeric', int: 'numeric', long: 'numeric', double: 'numeric', decimal: 'numeric',
  timestamp: 'date', null: null };
function families(collection, dotted, elements) {
  const out = new Set();
  for (const [k, e] of Object.entries(((PROFILE || {})[collection] || {}).paths || {})) {
    if (k.endsWith('[]') !== elements || k.replace(/\[\]/g, '') !== dotted) continue;
    for (const t of Object.keys(e.types)) { const f = t in FAMILY ? FAMILY[t] : t; if (f) out.add(f); }
  }
  return out;
}
const SCALAR = (f) => f !== 'object' && f !== 'array';
// always <collection> <table path> <dotted>: true if every node of the table holds a non-null
// value at the field (the root: every document; a child table: every object at its path).
function always(collection, tablePath, dotted) {
  const prof = (PROFILE || {})[collection] || {};
  const sum = (pred, type) => Object.entries(prof.paths || {}).filter(([k]) => pred(k))
    .reduce((s, [, e]) => s + Object.entries(e.types).filter(([t]) => type(t)).reduce((a, [, ty]) => a + ty.values, 0), 0);
  const strip = (k) => k.replace(/\[\]/g, '');
  const nodes = tablePath === '' ? prof.documents
    : sum((k) => strip(k) === tablePath && (k.endsWith('[]') || !Object.keys(prof.paths).includes(k + '[]')), (t) => t === 'object');
  const values = sum((k) => !k.endsWith('[]') && strip(k) === dotted, (t) => t !== 'null');
  return values >= nodes;
}

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

// mixed-elements, reported only: the child tables of a list that mixes objects and scalars. The
// table stays as mongo2pg proposed it (no JSONB); export writes no row for the scalars, so
// they are for the attempts to load.
if (PROFILE) {
  const mixed = [];
  for (const coll of fs.readdirSync(base).filter((d) => fs.statSync(path.join(base, d)).isDirectory()).sort()) {
    for (const f of mappingsOf(coll)) {
      const text = fs.readFileSync(f, 'utf8');
      const p = mongoPath(text);
      if (!p || p === '.') continue;
      const fams = families(coll, p.replace(/^\./, ''), true);
      if (fams.has('object') && [...fams].some(SCALAR)) mixed.push(`${tableName(text)} (${coll}:${p}: ${[...fams].sort().join('/')})`);
    }
  }
  console.log(`mixed-elements: ${mixed.length} child table(s) whose list mixes objects and scalars, kept${mixed.length ? ': ' + mixed.join(', ') : ''}`);
}

// --jsonb: each subtree as one JSONB column of its parent's table.
for (const spec of JSONB) {
  const m = spec.match(/^([^:]+):(\.\S+)$/);
  if (!m) { console.error(`--jsonb ${spec}: expected <collection>:<mongo path>, e.g. <collection>:.<field>`); process.exit(2); }
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

// The fixes from the profile, column by column (YAML, as mongo2pg writes it: lists not indented).
const changed = { 'not-null-from-sample': [], 'mixed-types': [] };
if (PROFILE) {
  for (const file of fs.existsSync(base) ? all.filter((f) => fs.existsSync(f)) : []) {
    const coll = path.basename(path.dirname(file));
    const doc = YAML.parseDocument(fs.readFileSync(file, 'utf8'));
    const pm = doc.get('pg_mapping');
    const cols = pm && pm.get('columns');
    const ddlCols = pm && pm.get('ddl') && pm.get('ddl').get('columns');
    if (!cols || !ddlCols || !cols.items) continue;
    const table = pm.get('table_name');
    const prefix = String(doc.get('mongo_path') || '.').replace(/^\./, '');
    const keys = new Set(((pm.get('ddl').get('foreign_keys') || {}).items || []).map((fk) => fk.get('from_col')));
    const ddlOf = (name) => ddlCols.items.find((d) => d.get('name') === name);
    let dirty = false;
    for (const col of cols.items) {
      const ddl = ddlOf(col.get('target_field'));
      if (!ddl) continue;
      const name = `${table}.${col.get('target_field')}`;
      const dotted = prefix ? `${prefix}.${col.get('source_field')}` : String(col.get('source_field'));
      if (!ddl.get('primary_key') && !keys.has(ddl.get('name')) && (ddl.get('nullable') === false || col.get('nullable') === false)
          && !always(coll, prefix, dotted)) {
        ddl.set('nullable', true); col.set('nullable', true); dirty = true;
        changed['not-null-from-sample'].push(name);
      }
      const type = String(col.get('data_type') || '').toLowerCase();
      if (type === 'text' || type.startsWith('json')) continue;
      // Scalars only: the structure stays, the type becomes TEXT (TEXT[] for a list).
      const list = type.endsWith('[]');
      const fams = families(coll, dotted, list);
      if (fams.size < 2 || ![...fams].every(SCALAR)) continue;
      const to = list ? 'text[]' : 'text';
      if (type === to) continue;
      col.set('data_type', to); ddl.set('sql_type', to.toUpperCase()); dirty = true;
      changed['mixed-types'].push(`${name} ${type} -> ${to} (${[...fams].sort().join('/')})`);
    }
    if (dirty) write(file, doc.toString({ indentSeq: false, lineWidth: 0 }));
  }
}
for (const [id, list] of Object.entries(changed)) {
  console.log(`${id}: ${PROFILE ? list.length + ' column(s)' + dry : 'skipped (--no-profile)'}${list.length ? ': ' + list.slice(0, 8).join(', ') + (list.length > 8 ? ', ...' : '') : ''}`);
}
console.log(`${all.length} mapping files${DRY ? ', none written' : ''}; now: to-pg, then export`);
