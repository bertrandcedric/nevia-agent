#!/usr/bin/env node
// Before an import: every value of the exported CSV against the type and nullability of its
// column in the mappings, so that every value PostgreSQL would refuse shows at once (mongo2pg
// import stops at the first one). Generic: any mongo2pg project (from migrate/precheck.mjs,
// copied into the work directory at each run; Claude's scan.py of run 261027181323, made a tool).
//   node precheck.mjs [--project DIR] [--examples N]
// Reads the mappings (pg_mapping.ddl) and data/<namespace>/<collection>/<table>.csv.gz. Prints,
// per column with problems, how many values and a few of them; exits non-zero if any. Run it
// after each regeneration (to-pg, export), before nvpg branch.

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import YAML from 'yaml';

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const PROJECT = opt('project', '{{PROJECT}}');
const EXAMPLES = Number(opt('examples', '3'));

// The tables of the mappings: name -> { column -> { type, nullable } }.
const tables = new Map();
const base = path.join(PROJECT, 'source', 'collections');
for (const dir of fs.readdirSync(base).sort()) {
  if (!fs.statSync(path.join(base, dir)).isDirectory()) continue;
  for (const f of fs.readdirSync(path.join(base, dir)).filter((x) => /^mapping_.*\.yaml$/.test(x))) {
    const ddl = ((YAML.parse(fs.readFileSync(path.join(base, dir, f), 'utf8')) || {}).pg_mapping || {}).ddl || {};
    const cols = new Map((ddl.columns || []).map((c) => [String(c.name).toLowerCase(),
      { type: String(c.sql_type || '').toUpperCase().trim(), nullable: c.nullable !== false }]));
    if (ddl.name) tables.set(String(ddl.name).toLowerCase(), cols);
  }
}

// CSV as COPY reads it: an empty unquoted field is NULL, "" an empty string.
function* csvRows(text) {
  let row = [], field = '', quoted = false, inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') inQuotes = false;
      else field += ch;
    } else if (ch === '"') { inQuotes = true; quoted = true; }
    else if (ch === ',') { row.push(quoted || field !== '' ? field : null); field = ''; quoted = false; }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(quoted || field !== '' ? field : null); yield row; row = []; field = ''; quoted = false;
    } else field += ch;
  }
  if (field !== '' || quoted || row.length) { row.push(quoted || field !== '' ? field : null); yield row; }
}

const ISO_TS = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}(:?\d{2})?)?$/;
const INT = /^[+-]?\d+$/;
const NUM = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$|^(NaN|[+-]?Infinity)$/;
const BOOL = new Set(['t', 'f', 'true', 'false', 'y', 'n', 'yes', 'no', 'on', 'off', '1', '0']);
const RANGE = { SMALLINT: 2n ** 15n, INTEGER: 2n ** 31n, INT: 2n ** 31n, INT4: 2n ** 31n, BIGINT: 2n ** 63n, INT8: 2n ** 63n };
// fits <type> <value>: null if PostgreSQL takes it, else why not.
function fits(type, v) {
  const t = type.replace(/\s+(NOT NULL|NULL|DEFAULT .*|PRIMARY KEY.*)$/, '');
  if (t.endsWith('[]')) return /^\{.*\}$/s.test(v) ? null : 'not an array literal';
  if (/^(TIMESTAMP|DATE)/.test(t)) return ISO_TS.test(v) && !Number.isNaN(Date.parse(v.replace(' ', 'T').replace(/([+-]\d{2})$/, '$1:00'))) ? null : 'not an ISO date';
  if (/^(SMALLINT|INTEGER|INT|INT4|BIGINT|INT8|SERIAL|BIGSERIAL)\b/.test(t)) {
    if (!INT.test(v)) return 'not an integer';
    const max = RANGE[t.split(/[ (]/)[0]];
    return max && (BigInt(v) >= max || BigInt(v) < -max) ? 'out of range' : null;
  }
  if (/^(NUMERIC|DECIMAL|DOUBLE|REAL|FLOAT)/.test(t)) return NUM.test(v.trim()) ? null : 'not a number';
  if (/^BOOL/.test(t)) return BOOL.has(v.trim().toLowerCase()) ? null : 'not a boolean';
  if (/^JSONB?$/.test(t)) { try { JSON.parse(v); return null; } catch { return 'not JSON'; } }
  if (t === 'UUID') return /^[0-9a-f]{8}-?([0-9a-f]{4}-?){3}[0-9a-f]{12}$/i.test(v) ? null : 'not a UUID';
  const n = t.match(/^(VARCHAR|CHARACTER VARYING|CHAR|CHARACTER)\s*\((\d+)\)/);
  if (n && [...v].length > Number(n[2])) return `longer than ${n[2]}`;
  return null;
}

const data = path.join(PROJECT, 'data');
const files = [];
const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) {
  const p = path.join(d, e.name);
  if (e.isDirectory()) walk(p); else if (/\.csv(\.gz)?$/.test(e.name)) files.push(p);
} };
if (fs.existsSync(data)) walk(data);

let problems = 0;
for (const file of files.sort()) {
  const table = path.basename(file).replace(/\.csv(\.gz)?$/, '').toLowerCase();
  const cols = tables.get(table);
  const rel = path.relative(data, file);
  if (!cols) { console.log(`${rel}: no mapping has a table ${table} (a CSV left from a deleted mapping?)`); problems++; continue; }
  const buf = fs.readFileSync(file);
  const text = (file.endsWith('.gz') ? zlib.gunzipSync(buf) : buf).toString('utf8');
  let header = null, rows = 0;
  const bad = new Map(); // column -> { why -> { n, examples } }
  const note = (c, why, v) => {
    const byWhy = bad.get(c) || bad.set(c, new Map()).get(c);
    const e = byWhy.get(why) || byWhy.set(why, { n: 0, examples: [] }).get(why);
    e.n++;
    if (e.examples.length < EXAMPLES && !e.examples.includes(v)) e.examples.push(v);
  };
  for (const row of csvRows(text)) {
    if (!header) { header = row.map((h) => String(h).toLowerCase()); continue; }
    rows++;
    header.forEach((c, i) => {
      const col = cols.get(c);
      if (!col) return;
      const v = row[i] ?? null;
      if (v === null) { if (!col.nullable && !/SERIAL/.test(col.type)) note(c, 'NULL in a NOT NULL column', 'NULL'); return; }
      const why = fits(col.type, v);
      if (why) note(c, why, v.length > 40 ? v.slice(0, 40) + '…' : v);
    });
  }
  const missing = header ? [...cols.keys()].filter((c) => !header.includes(c) && !cols.get(c).nullable && !/SERIAL|DEFAULT/.test(cols.get(c).type)) : [];
  for (const c of missing) note(c, 'NOT NULL column absent from the CSV', '-');
  if (!bad.size) { console.log(`${rel}: ${rows} rows, ok`); continue; }
  console.log(`${rel}: ${rows} rows`);
  for (const [c, byWhy] of bad) for (const [why, e] of byWhy) {
    problems += e.n;
    console.log(`  ${table}.${c} ${cols.get(c).type}: ${e.n} ${why}, e.g. ${e.examples.map((x) => JSON.stringify(x)).join(', ')}`);
  }
}
console.log(problems ? `precheck: ${problems} value(s) PostgreSQL would refuse: fix the mappings, regenerate, run it again` : `precheck: ${files.length} CSV, every value fits its column`);
process.exit(problems ? 1 : 0);
