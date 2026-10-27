#!/usr/bin/env node
// Verification of the {{MONGO_DB}} MongoDB -> PostgreSQL migration (from migrate/verify.mjs,
// copied into the work directory at each run: fix it here if it is wrong, and say so).
//
//   node verify.mjs <branch | main> [--project DIR] [--samples N] [--per-path N]
//
// <branch> is a branch of the database (e.g. try3), main the main database: the URI comes from
// `nvpg uri`, never from the command line, so no password reaches a log. Reads the project's
// mapping_*.yaml files, so it follows the mapping as it is edited, and the source's own checks
// from checks.mjs next to it, when there is one.
// Prints the FAIL lines and a summary only, and exits non-zero on any FAIL. Every check, PASS
// included, goes to verify-<target>.txt next to it, and the results to verify-<target>.json
// (read by the report).

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import YAML from 'yaml';
import pg from 'pg';
import { MongoClient } from 'mongodb';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name, def) => {
  const i = args.indexOf('--' + name);
  return i >= 0 && args[i + 1] ? args[i + 1] : def;
};
const TARGET = args.find((a, i) => !a.startsWith('--') && !(i > 0 && args[i - 1].startsWith('--')));
const PROJECT = opt('project', '{{PROJECT}}');
const SCHEMA = '{{SCHEMA}}';
const SAMPLES = Number(opt('samples', '50'));
const PER_PATH = Number(opt('per-path', '3'));

if (!TARGET || !/^(main|[a-z0-9-]+)$/.test(TARGET)) {
  console.error('usage: node verify.mjs <branch | main> [--project DIR] [--samples N] [--per-path N]');
  process.exit(2);
}
const pgUri = execFileSync('nvpg', TARGET === 'main' ? ['uri'] : ['uri', TARGET], { encoding: 'utf8' }).trim();

const transcript = [];
function say(line, show = true) {
  transcript.push(line);
  if (show) console.log(line);
}

// ---------------------------------------------------------------- reporting
const results = [];
const collectionRows = [];
let failures = 0;
function record(group, name, ok, detail) {
  results.push({ group, name, ok, detail });
  if (!ok) failures++;
  const line = `${ok ? 'PASS' : 'FAIL'} [${group}] ${name}${detail ? ' — ' + detail : ''}`;
  say(line, !ok);
}

function flush() {
  fs.writeFileSync(path.join(HERE, `verify-${TARGET}.txt`), transcript.join('\n') + '\n');
  const groups = [...new Set(results.map((r) => r.group))].map((g) => {
    const rs = results.filter((r) => r.group === g);
    const fail = rs.filter((r) => !r.ok).length;
    return { group: g, pass: rs.length - fail, fail };
  });
  fs.writeFileSync(path.join(HERE, `verify-${TARGET}.json`), JSON.stringify({
    target: TARGET, at: new Date().toISOString(), pass: results.length - failures, fail: failures,
    groups, collections: collectionRows, excluded: readExcluded(), results,
  }, null, 2) + '\n');
}

// ---------------------------------------------------------------- config
function readSourceConfig() {
  const file = path.join(PROJECT, 'config', path.basename(PROJECT) + '.toml');
  const text = fs.readFileSync(file, 'utf8');
  let section = '';
  let uri = null;
  let namespace = null;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    const sec = line.match(/^\[([^\]]+)\]$/);
    if (sec) { section = sec[1]; continue; }
    if (section !== 'source') continue;
    const m = line.match(/^(\w+)\s*=\s*"(.*)"$/);
    if (!m) continue;
    if (m[1] === 'uri') uri = m[2];
    if (m[1] === 'namespace') namespace = m[2];
  }
  if (!uri || !namespace) throw new Error('source uri/namespace not found in config');
  return { uri, namespace };
}

// The empty collections analyse left out of the project ([source] exclude = [...], on one line
// or, as mongo2pg may write it again, on several).
function readExcluded() {
  try {
    const text = fs.readFileSync(path.join(PROJECT, 'config', path.basename(PROJECT) + '.toml'), 'utf8');
    const m = text.match(/^exclude\s*=\s*\[([^\]]*)\]/m);
    return m ? [...m[1].matchAll(/"((?:[^"\\]|\\.)*)"|'([^']*)'/g)].map((x) => x[1] ?? x[2]) : [];
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------- mappings
function loadMappings() {
  const base = path.join(PROJECT, 'source', 'collections');
  const collections = [];
  for (const dir of fs.readdirSync(base).sort()) {
    const dirPath = path.join(base, dir);
    if (!fs.statSync(dirPath).isDirectory()) continue;
    const tables = [];
    for (const f of fs.readdirSync(dirPath).sort()) {
      if (!f.startsWith('mapping_') || !f.endsWith('.yaml')) continue;
      const y = YAML.parse(fs.readFileSync(path.join(dirPath, f), 'utf8'));
      const pm = y.pg_mapping || {};
      tables.push({
        file: f,
        table: pm.table_name,
        schema: pm.schema_name,
        mongoPath: y.mongo_path,
        mode: (y.traversal || {}).mode,
        parent: (y.traversal || {}).parent_table,
        fk: (y.traversal || {}).fk_column,
        keyColumn: (y.traversal || {}).key_column,
        columns: pm.columns || [],
        ddl: pm.ddl || { columns: [], foreign_keys: [] },
      });
    }
    const root = tables.find((t) => t.mode === 'root');
    if (!root) continue;
    collections.push({ collection: dir, root, tables });
  }
  return collections;
}

const segmentsOf = (mongoPath) =>
  (mongoPath || '.').replace(/^\./, '').split('.').filter((s) => s.length > 0);

// ---------------------------------------------------------------- values
const MISSING = '\u0000MISSING';

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v) && !(v instanceof Date)
    && !(v instanceof Buffer) && typeof v._bsontype !== 'string';
}

function normalizeNodes(v) {
  if (v === undefined || v === null) return [];
  if (Array.isArray(v)) return v;
  return [v];
}

// Resolve the sub-documents mongo2pg maps to a child table, for one root doc.
// Mirrors childCountPipeline().
function resolveNodes(rootDoc, segments, mode) {
  let cur = [rootDoc];
  for (const seg of segments) {
    const next = [];
    for (const v of cur) {
      for (const node of normalizeNodes(v)) {
        if (!isPlainObject(node)) continue;
        const got = node[seg];
        if (got !== undefined && got !== null) next.push(got);
      }
    }
    cur = next;
  }
  const nodes = [];
  for (const v of cur) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) {
      for (const e of v) nodes.push({ node: e, key: isPlainObject(e) ? e.key : undefined });
    } else if (isPlainObject(v)) {
      if (mode === 'map_object') {
        for (const k of Object.keys(v)) nodes.push({ node: v[k], key: k });
      } else {
        nodes.push({ node: v, key: undefined });
      }
    } else {
      nodes.push({ node: v, key: undefined });
    }
  }
  return nodes;
}

// Coverage (as unmapped.mjs): the source paths the mappings cover, as leaves (a column),
// containers (a child table and its ancestors, walked into) and JSONB columns (all below them).
const norm = (p) => (p || '.').replace(/^\./, '');
function coverage(tables) {
  const leaves = new Set();
  const containers = new Set();
  const jsonb = new Set();
  for (const t of tables) {
    const b = norm(t.mongoPath);
    if (t.mode !== 'root') {
      containers.add(b);
      const parts = b.split('.');
      for (let i = 1; i < parts.length; i++) containers.add(parts.slice(0, i).join('.'));
    }
    for (const col of t.columns) {
      const p = b ? b + '.' + col.source_field : col.source_field;
      leaves.add(p);
      if (String(col.data_type).toLowerCase().startsWith('json')) jsonb.add(p);
      if (t.keyColumn && col.source_field === t.keyColumn) leaves.add(b);
    }
  }
  return { leaves, containers, jsonb };
}
function covered(p, cov) {
  if (cov.leaves.has(p) || cov.containers.has(p)) return true;
  for (const j of cov.jsonb) if (p === j || p.startsWith(j + '.')) return true;
  return false;
}
// A value that holds nothing: dropping it loses no data.
const isEmpty = (v) => v === null || v === undefined || (Array.isArray(v) && v.length === 0)
  || (isPlainObject(v) && Object.keys(v).length === 0);
// uncoveredIn <doc>: the paths of the document that hold data and that no mapping covers.
function uncoveredIn(doc, prefix, cov, found) {
  for (const k of Object.keys(doc)) {
    const p = prefix ? prefix + '.' + k : k;
    const v = doc[k];
    if (!covered(p, cov)) {
      if (!isEmpty(v)) found.set(p, (found.get(p) || 0) + 1);
      continue;
    }
    if (cov.containers.has(p)) {
      for (const node of (Array.isArray(v) ? v : [v])) if (isPlainObject(node)) uncoveredIn(node, p, cov, found);
    }
  }
}

// Aggregation that counts the elements mongo2pg maps to a child table.
function childCountPipeline(segments, mode) {
  const p = [{ $project: { v: '$$ROOT' } }];
  for (const seg of segments) {
    p.push({
      $project: {
        v: {
          $cond: [
            { $isArray: '$v' },
            '$v',
            { $cond: [{ $in: [{ $type: '$v' }, ['missing', 'null']] }, [], ['$v']] },
          ],
        },
      },
    });
    p.push({ $unwind: '$v' });
    p.push({
      $project: {
        v: {
          $cond: [
            { $eq: [{ $type: '$v' }, 'object'] },
            { $getField: { field: { $literal: seg }, input: '$v' } },
            '$$REMOVE',
          ],
        },
      },
    });
  }
  const branches = [
    { case: { $in: [{ $type: '$v' }, ['missing', 'null']] }, then: 0 },
    { case: { $isArray: '$v' }, then: { $size: '$v' } },
  ];
  if (mode === 'map_object') {
    branches.push({
      case: { $eq: [{ $type: '$v' }, 'object'] },
      then: { $size: { $objectToArray: '$v' } },
    });
    branches.push({ case: true, then: 0 });
  }
  p.push({ $project: { n: { $switch: { branches, default: 1 } } } });
  p.push({ $group: { _id: null, total: { $sum: '$n' } } });
  return p;
}

function baseType(t) {
  const s = String(t || '').toLowerCase().trim();
  if (s.endsWith('[]')) return { kind: 'array', elem: baseType(s.slice(0, -2)).kind };
  if (s.startsWith('varchar') || s.startsWith('character varying') || s === 'text' || s === 'uuid')
    return { kind: 'text' };
  if (s.startsWith('timestamp') || s === 'date') return { kind: 'ts' };
  if (s === 'boolean' || s === 'bool') return { kind: 'bool' };
  if (s === 'jsonb' || s === 'json') return { kind: 'json' };
  if (s === 'bytea') return { kind: 'bytes' };
  if (/^(small|big)?int/.test(s) || s === 'numeric' || s === 'real' || s === 'double precision'
      || s.startsWith('numeric')) return { kind: 'num' };
  return { kind: 'text' };
}

function toMillis(v) {
  if (v instanceof Date) return v.getTime();
  if (typeof v === 'number') return v;
  if (typeof v === 'string') {
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }
  if (v && typeof v.getTime === 'function') return v.getTime();
  if (v && v._bsontype === 'Timestamp' && typeof v.toNumber === 'function') return v.toNumber() * 1000;
  return null;
}

// Canonical, comparable string for a value of a given SQL type.
function canon(type, v) {
  if (v === undefined) return MISSING;
  if (v === null) return MISSING;
  const t = baseType(type);
  if (t.kind === 'array') {
    const arr = Array.isArray(v) ? v : [v];
    return JSON.stringify(arr.map((e) => canon(t.elem, e)));
  }
  switch (t.kind) {
    case 'ts': {
      const ms = toMillis(v);
      return ms === null ? 'BADDATE:' + String(v) : 'ts:' + ms;
    }
    case 'bool':
      if (typeof v === 'boolean') return v ? 'true' : 'false';
      if (v === 'true' || v === 't' || v === 1) return 'true';
      if (v === 'false' || v === 'f' || v === 0) return 'false';
      return 'bool:' + String(v);
    case 'num': {
      const n = Number(typeof v === 'object' && v !== null && typeof v.valueOf === 'function'
        ? v.valueOf() : v);
      return Number.isNaN(n) ? 'NaN:' + String(v) : 'n:' + n;
    }
    case 'json':
      return 'j:' + canonJson(v);
    case 'bytes': {
      const b = Buffer.isBuffer(v) ? v
        : (v && v.buffer ? Buffer.from(v.buffer) : Buffer.from(String(v)));
      return 'b:' + b.toString('base64');
    }
    default: {
      if (v instanceof Date) return v.toISOString();
      if (typeof v === 'object' && v !== null) {
        if (typeof v.toHexString === 'function') return v.toHexString();
        return String(v);
      }
      return String(v);
    }
  }
}

function canonJson(v) {
  const norm = (x) => {
    if (x === null || x === undefined) return null;
    if (x instanceof Date) return x.toISOString();
    if (Array.isArray(x)) return x.map(norm);
    if (typeof x === 'object') {
      if (typeof x.toHexString === 'function') return x.toHexString();
      if (typeof x._bsontype === 'string' && typeof x.valueOf === 'function') return x.valueOf();
      const out = {};
      for (const k of Object.keys(x).sort()) out[k] = norm(x[k]);
      return out;
    }
    if (typeof x === 'string') {
      // A JSONB column may hold either the parsed value or its JSON text.
      return x;
    }
    return x;
  };
  return JSON.stringify(norm(v));
}

// JSONB round-trip tolerance: a string that parses to the same JSON matches.
function jsonEquivalent(a, b) {
  if (a === b) return true;
  const strip = (s) => (s.startsWith('j:') ? s.slice(2) : s);
  const pa = strip(a);
  const pb = strip(b);
  if (pa === pb) return true;
  const tryParse = (s) => { try { return JSON.stringify(JSON.parse(s)); } catch { return null; } };
  const qa = tryParse(pa);
  const qb = tryParse(pb);
  if (qa !== null && qa === pb) return true;
  if (qb !== null && qb === pa) return true;
  if (qa !== null && qb !== null && qa === qb) return true;
  // "x" vs x for scalar strings
  const unq = (s) => (s.length > 1 && s[0] === '"' && s.endsWith('"') ? s.slice(1, -1) : s);
  return unq(pa) === unq(pb);
}

function valuesMatch(type, mongoVal, pgVal) {
  const a = canon(type, mongoVal);
  const b = canon(type, pgVal);
  if (a === b) return true;
  if (baseType(type).kind === 'json') return jsonEquivalent(a, b);
  return false;
}

// Value of one mapped column for a child node.
function nodeColumnValue(col, nodeEntry, keyColumn) {
  const { node, key } = nodeEntry;
  if (isPlainObject(node)) {
    if (node[col.source_field] !== undefined) return node[col.source_field];
    if (keyColumn && col.source_field === keyColumn && key !== undefined) return key;
    return undefined;
  }
  // scalar node
  if (keyColumn && col.source_field === keyColumn) return key;
  return node;
}

const q = (id) => '"' + String(id).replace(/"/g, '""') + '"';

// ---------------------------------------------------------------- main
const { uri: mongoUri, namespace: dbName } = readSourceConfig();
const collections = loadMappings();
const tableByName = new Map();
for (const c of collections) for (const t of c.tables) tableByName.set(t.table, t);

// pg >= 8.16 maps sslmode=require to verify-full; Nevia serves a self-signed
// certificate, so drop sslmode and keep libpq's "require" semantics (encrypt,
// do not verify the chain).
function pgConnectionString(uri) {
  const u = new URL(uri);
  u.searchParams.delete('sslmode');
  return u.toString();
}

const mongo = new MongoClient(mongoUri);
const client = new pg.Client({
  connectionString: pgConnectionString(pgUri),
  ssl: { rejectUnauthorized: false },
});

async function pgRows(sql, params) {
  const r = await client.query(sql, params);
  return r.rows;
}
async function pgScalar(sql, params) {
  const rows = await pgRows(sql, params);
  return rows.length ? Object.values(rows[0])[0] : null;
}

function schemaOf(t) { return t.schema || SCHEMA; }
function fq(t) { return q(schemaOf(t)) + '.' + q(t.table); }

async function main() {
  await mongo.connect();
  await client.connect();
  const db = mongo.db(dbName);

  // 1. row counts per collection -----------------------------------------
  for (const c of collections) {
    const mcount = await db.collection(c.collection).countDocuments();
    let pcount;
    try {
      pcount = Number(await pgScalar(`SELECT count(*) FROM ${fq(c.root)}`));
    } catch (e) {
      record('counts', `${c.collection} -> ${c.root.table}`, false, 'query failed: ' + e.message);
      continue;
    }
    record('counts', `${c.collection} -> ${c.root.table}`, mcount === pcount,
      `mongo ${mcount} / pg ${pcount}`);
    collectionRows.push({ collection: c.collection, root: c.root.table, mongo: mcount, pg: pcount,
      child_tables: c.tables.filter((t) => t.mode !== 'root').length });
  }

  // 2. child table counts --------------------------------------------------
  for (const c of collections) {
    for (const t of c.tables) {
      if (t.mode === 'root') continue;
      const segs = segmentsOf(t.mongoPath);
      let expected = 0;
      try {
        const agg = await db.collection(c.collection)
          .aggregate(childCountPipeline(segs, t.mode), { allowDiskUse: true }).toArray();
        expected = agg.length ? Number(agg[0].total) : 0;
      } catch (e) {
        record('child tables', `${c.collection}.${t.table}`, false, 'aggregation failed: ' + e.message);
        continue;
      }
      let actual;
      try {
        actual = Number(await pgScalar(`SELECT count(*) FROM ${fq(t)}`));
      } catch (e) {
        record('child tables', `${c.collection}.${t.table}`, false, 'query failed: ' + e.message);
        continue;
      }
      record('child tables', `${c.collection}.${t.table} (${t.mongoPath})`, expected === actual,
        `mongo ${expected} / pg ${actual}`);
    }
  }

  // 3. sampled documents ---------------------------------------------------
  // SAMPLES documents at random, and PER_PATH more among those that have the path of each child
  // table: a bug of a nested structure that few documents have is not left to chance.
  for (const c of collections) {
    const coll = db.collection(c.collection);
    const sample = await coll.aggregate([{ $sample: { size: SAMPLES } }], { allowDiskUse: true }).toArray();
    const seen = new Set(sample.map((d) => String(d._id)));
    for (const t of c.tables) {
      const segs = segmentsOf(t.mongoPath);
      if (t.mode === 'root' || segs.length === 0) continue;
      try {
        const more = await coll.aggregate([{ $match: { [segs.join('.')]: { $exists: true } } },
          { $sample: { size: PER_PATH } }], { allowDiskUse: true }).toArray();
        for (const d of more) if (!seen.has(String(d._id))) { seen.add(String(d._id)); sample.push(d); }
      } catch { /* a path MongoDB cannot match (e.g. a $ in a key): the random sample only */ }
    }
    const rootIdCol = (c.root.columns.find((x) => x.source_field === '_id') || {}).target_field;
    const pkCols = c.root.ddl.columns.filter((x) => x.primary_key).map((x) => x.name);
    const idCol = rootIdCol || (pkCols.length === 1 ? pkCols[0] : null);

    let bad = 0;
    const details = [];
    for (const doc of sample) {
      if (!idCol) break;
      // root row
      const cols = c.root.columns;
      const sel = cols.map((x) => q(x.target_field)).join(', ');
      let rows;
      try {
        rows = await pgRows(
          `SELECT ${sel} FROM ${fq(c.root)} WHERE ${q(idCol)} = $1`, [String(doc._id)]);
      } catch (e) {
        bad++; details.push(`${doc._id}: ${e.message}`); break;
      }
      if (rows.length !== 1) {
        bad++; details.push(`${doc._id}: ${rows.length} rows in ${c.root.table}`); continue;
      }
      for (const col of cols) {
        const mv = doc[col.source_field];
        const pv = rows[0][col.target_field];
        if (!valuesMatch(col.data_type, mv, pv)) {
          bad++;
          details.push(`${doc._id}.${col.source_field}: mongo=${JSON.stringify(canon(col.data_type, mv)).slice(0, 80)} pg=${JSON.stringify(canon(col.data_type, pv)).slice(0, 80)}`);
        }
      }
      // child rows for this document
      for (const t of c.tables) {
        if (t.mode === 'root') continue;
        const nodes = resolveNodes(doc, segmentsOf(t.mongoPath), t.mode);
        const expected = nodes.map((n) =>
          t.columns.map((col) => canon(col.data_type, nodeColumnValue(col, n, t.keyColumn))).join('\u0001'));
        // build the join chain up to the root table
        const chain = [];
        let curT = t;
        while (curT && curT.mode !== 'root') {
          chain.push(curT);
          curT = tableByName.get(curT.parent);
        }
        if (!curT) { bad++; details.push(`${t.table}: no root in chain`); continue; }
        let sql = `SELECT ${t.columns.map((x, i) => 't0.' + q(x.target_field) + ' AS c' + i).join(', ') || 't0.id'} FROM ${fq(t)} t0`;
        for (let i = 0; i < chain.length; i++) {
          const next = i + 1 < chain.length ? chain[i + 1] : curT;
          // join on the parent's real primary key: a mapped `_id` column is the
          // source document's id, not the surrogate key the fk points at.
          const parentPk = (next.ddl.columns.find((x) => x.primary_key) || { name: 'id' }).name;
          sql += ` JOIN ${fq(next)} t${i + 1} ON t${i}.${q(chain[i].fk)} = t${i + 1}.${q(parentPk)}`;
        }
        sql += ` WHERE t${chain.length}.${q(idCol)} = $1`;
        let crows;
        try { crows = await pgRows(sql, [String(doc._id)]); } catch (e) {
          bad++; details.push(`${t.table}: ${e.message}`); continue;
        }
        const actual = crows.map((r) =>
          t.columns.map((x, i) => canon(x.data_type, r['c' + i])).join('\u0001'));
        const ea = expected.slice().sort();
        const aa = actual.slice().sort();
        let same = ea.length === aa.length;
        if (same) {
          for (let i = 0; i < ea.length; i++) {
            if (ea[i] === aa[i]) continue;
            if (t.columns.some((cc) => baseType(cc.data_type).kind === 'json')
              && jsonEquivalent(ea[i], aa[i])) continue;
            same = false; break;
          }
        }
        if (!same) {
          bad++;
          details.push(`${doc._id} ${t.table}: mongo ${ea.length} rows / pg ${aa.length} rows${ea.length === aa.length ? ' (values differ: ' + JSON.stringify(ea.find((x, i) => x !== aa[i]) || '').slice(0, 70) + ' vs ' + JSON.stringify(aa.find((x, i) => x !== ea[i]) || '').slice(0, 70) + ')' : ''}`);
        }
      }
    }
    record('samples', `${c.collection} (${sample.length} docs)`, bad === 0,
      bad === 0 ? 'all mapped fields match' : `${bad} mismatch(es): ` + details.slice(0, 4).join(' | '));
  }

  // 3a. coverage: every field that holds data is mapped (known issue unmapped-fields-dropped-silently,
  // and a table deleted with data in it). Every document is read: a rare field is a field too.
  for (const c of collections) {
    const cov = coverage(c.tables);
    const found = new Map();
    for await (const doc of db.collection(c.collection).find({})) uncoveredIn(doc, '', cov, found);
    const list = [...found.entries()].sort().map(([p, n]) => `${p} (${n})`);
    record('coverage', c.collection, list.length === 0,
      list.length === 0 ? 'every field with data is mapped'
        : `${list.length} field(s) with data and no mapping: ` + list.slice(0, 6).join(', ') + (list.length > 6 ? ', ...' : ''));
  }

  // 3b. no VARCHAR(n) sized from the sample (known issue varchar-sized-from-sample) ------
  // The data loads today, so no comparison sees it; the next longer value would not.
  const sized = [];
  for (const c of collections) {
    for (const t of c.tables) {
      const cols = [...t.columns.map((x) => [x.target_field, x.data_type]),
        ...(t.ddl.columns || []).map((x) => [x.name, x.sql_type])];
      for (const [name, type] of cols) {
        if (/^\s*(varchar|character varying)\s*\(\d+\)/i.test(String(type || ''))) sized.push(`${t.table}.${name} ${type}`);
      }
    }
  }
  const sizedCols = [...new Set(sized.map((s) => s.replace(/ .*/, '')))];
  record('ddl', 'no VARCHAR(n) sized from the sample', sizedCols.length === 0,
    sizedCols.length === 0 ? 'none' : `${sizedCols.length} column(s): ` + sizedCols.slice(0, 5).join(', '));

  // 4. primary keys and foreign keys ---------------------------------------
  for (const c of collections) {
    for (const t of c.tables) {
      const pk = t.ddl.columns.filter((x) => x.primary_key).map((x) => x.name);
      if (pk.length) {
        const list = pk.map(q).join(', ');
        try {
          const r = await pgRows(
            `SELECT count(*) AS n, count(DISTINCT (${list})) AS d, count(*) FILTER (WHERE ${pk.map((p) => q(p) + ' IS NULL').join(' OR ')}) AS nulls FROM ${fq(t)}`);
          const ok = Number(r[0].n) === Number(r[0].d) && Number(r[0].nulls) === 0;
          record('keys', `pk ${t.table} (${pk.join(',')})`, ok,
            `${r[0].n} rows / ${r[0].d} distinct / ${r[0].nulls} null`);
        } catch (e) {
          record('keys', `pk ${t.table}`, false, e.message);
        }
      }
      for (const fk of (t.ddl.foreign_keys || [])) {
        const parent = tableByName.get(fk.to_table);
        if (!parent) { record('keys', `fk ${t.table}.${fk.from_col}`, false, 'unknown parent ' + fk.to_table); continue; }
        try {
          const n = Number(await pgScalar(
            `SELECT count(*) FROM ${fq(t)} ch LEFT JOIN ${fq(parent)} pa ON ch.${q(fk.from_col)} = pa.${q(fk.to_col)} WHERE ch.${q(fk.from_col)} IS NOT NULL AND pa.${q(fk.to_col)} IS NULL`));
          record('keys', `fk ${t.table}.${fk.from_col} -> ${fk.to_table}.${fk.to_col}`, n === 0,
            `${n} orphan(s)`);
        } catch (e) {
          record('keys', `fk ${t.table}.${fk.from_col}`, false, e.message);
        }
      }
    }
  }

  // 5. the source's own checks (checks.mjs, if any) --------------------------
  // Each one is { name, collection, pipeline, sql }: the aggregation's groups ({_id, n}) in
  // MongoDB against the rows (k, n) of the SQL in Postgres, compared group by group.
  const checksFile = path.join(HERE, 'checks.mjs');
  const checks = fs.existsSync(checksFile) ? (await import(pathToFileURL(checksFile).href)).default : [];
  for (const ch of checks) {
    try {
      const m = new Map((await db.collection(ch.collection).aggregate(ch.pipeline).toArray())
        .map((x) => [String(x._id), Number(x.n)]));
      const p = new Map((await pgRows(ch.sql)).map((x) => [String(x.k), Number(x.n)]));
      const diffs = [];
      for (const [k, v] of m) if (p.get(k) !== v) diffs.push(`${k}: mongo ${v} / pg ${p.get(k) ?? 0}`);
      for (const [k, v] of p) if (!m.has(k)) diffs.push(`${k}: mongo 0 / pg ${v}`);
      record('source checks', ch.name, diffs.length === 0,
        diffs.length === 0 ? `${m.size} group(s) equal` : `${diffs.length} differ: ` + diffs.slice(0, 3).join(' | '));
    } catch (e) {
      record('source checks', ch.name, false, e.message);
    }
  }

  // summary ----------------------------------------------------------------
  const groups = [...new Set(results.map((r) => r.group))];
  say('');
  say('--- summary ---');
  for (const g of groups) {
    const rs = results.filter((r) => r.group === g);
    const f = rs.filter((r) => !r.ok).length;
    say(`${g}: ${rs.length - f} PASS, ${f} FAIL`);
  }
  const pass = results.length - failures;
  say(`TOTAL: ${pass} PASS, ${failures} FAIL`);
  say(failures === 0 ? 'VERIFICATION PASS' : 'VERIFICATION FAIL');
  say(`every check: verify-${TARGET}.txt, the results: verify-${TARGET}.json`);
}

main()
  .then(async () => {
    flush();
    await mongo.close().catch(() => {});
    await client.end().catch(() => {});
    process.exit(failures === 0 ? 0 : 1);
  })
  .catch(async (e) => {
    record('fatal', 'verify.mjs', false, e && e.message ? e.message : String(e));
    flush();
    await mongo.close().catch(() => {});
    await client.end().catch(() => {});
    process.exit(1);
  });
