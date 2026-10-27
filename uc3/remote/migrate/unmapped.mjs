#!/usr/bin/env node
// The source fields that no mapping covers, per collection, with their types and a short sample
// (from migrate/unmapped.mjs, copied into the work directory at each run). Run it once after the
// known issues are fixed, and once more before the report.
//   node unmapped.mjs [--project DIR]

import fs from 'node:fs';
import path from 'node:path';
import YAML from 'yaml';
import { MongoClient } from 'mongodb';

const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const PROJECT = opt('project', '{{PROJECT}}');

function readSourceConfig() {
  const text = fs.readFileSync(path.join(PROJECT, 'config', path.basename(PROJECT) + '.toml'), 'utf8');
  let section = '', uri = null, namespace = null;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    const sec = line.match(/^\[([^\]]+)\]$/);
    if (sec) { section = sec[1]; continue; }
    if (section !== 'source') continue;
    const m = line.match(/^(\w+)\s*=\s*"(.*)"$/);
    if (m && m[1] === 'uri') uri = m[2];
    if (m && m[1] === 'namespace') namespace = m[2];
  }
  return { uri, namespace };
}

function loadMappings() {
  const base = path.join(PROJECT, 'source', 'collections');
  const out = [];
  for (const dir of fs.readdirSync(base).sort()) {
    const dirPath = path.join(base, dir);
    if (!fs.statSync(dirPath).isDirectory()) continue;
    const tables = [];
    for (const f of fs.readdirSync(dirPath).sort()) {
      if (!f.startsWith('mapping_') || !f.endsWith('.yaml')) continue;
      const y = YAML.parse(fs.readFileSync(path.join(dirPath, f), 'utf8'));
      const pm = y.pg_mapping || {};
      tables.push({ table: pm.table_name, mongoPath: y.mongo_path, mode: (y.traversal || {}).mode,
        keyColumn: (y.traversal || {}).key_column, columns: pm.columns || [] });
    }
    if (!tables.some((t) => t.mode === 'root')) continue;
    out.push({ collection: dir, tables });
  }
  return out;
}

const norm = (p) => (p || '.').replace(/^\./, '');

function coverage(tables) {
  const leaves = new Set();
  const containers = new Set();
  const jsonb = new Set();
  for (const t of tables) {
    const base = norm(t.mongoPath);
    if (t.mode !== 'root') {
      containers.add(base);
      // every ancestor is traversed
      const parts = base.split('.');
      for (let i = 1; i < parts.length; i++) containers.add(parts.slice(0, i).join('.'));
    }
    for (const c of t.columns) {
      const p = base ? base + '.' + c.source_field : c.source_field;
      leaves.add(p);
      if (String(c.data_type).toLowerCase().startsWith('json')) jsonb.add(p);
      if (t.keyColumn && c.source_field === t.keyColumn) leaves.add(base); // key comes from the map
    }
  }
  return { leaves, containers, jsonb };
}

function covered(p, cov) {
  if (cov.leaves.has(p) || cov.containers.has(p)) return true;
  for (const j of cov.jsonb) if (p === j || p.startsWith(j + '.')) return true;
  return false;
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v) && !(v instanceof Date)
    && !Buffer.isBuffer(v) && typeof v._bsontype !== 'string';
}

function bsonType(v) {
  if (v === null) return 'null';
  if (v === undefined) return 'missing';
  if (Array.isArray(v)) return v.length === 0 ? 'empty-array' : 'array';
  if (v instanceof Date) return 'date';
  if (Buffer.isBuffer(v)) return 'binary';
  if (typeof v === 'object') return v._bsontype ? String(v._bsontype).toLowerCase() : 'object';
  return typeof v;
}

function walk(doc, prefix, cov, found, counts) {
  for (const k of Object.keys(doc)) {
    const p = prefix ? prefix + '.' + k : k;
    const v = doc[k];
    if (!covered(p, cov)) {
      found.set(p, (found.get(p) || 0) + 1);
      const rec = counts.get(p) || { types: new Map(), sample: undefined };
      const t = bsonType(v);
      rec.types.set(t, (rec.types.get(t) || 0) + 1);
      if (rec.sample === undefined && v !== null && t !== 'empty-array') {
        try { rec.sample = JSON.stringify(v).slice(0, 120); } catch { rec.sample = String(v); }
      }
      counts.set(p, rec);
      continue;
    }
    if (cov.containers.has(p)) {
      for (const node of (Array.isArray(v) ? v : [v])) {
        if (isPlainObject(node)) walk(node, p, cov, found, counts);
      }
    }
  }
}

const cfg = readSourceConfig();
const mongo = new MongoClient(cfg.uri);

async function main() {
  await mongo.connect();
  const db = mongo.db(cfg.namespace);
  for (const c of loadMappings()) {
    const cov = coverage(c.tables);
    const found = new Map();
    const kinds = new Map();
    const cursor = db.collection(c.collection).find({});
    let n = 0;
    for await (const doc of cursor) { n++; walk(doc, '', cov, found, kinds); }
    if (found.size) {
      console.log(`\n${c.collection} (${n} docs):`);
      for (const [p, cnt] of [...found.entries()].sort()) {
        const rec = kinds.get(p);
        const types = [...rec.types.entries()].map(([t, n]) => `${t}:${n}`).join(' ');
        console.log(`  ${p}  {${types}}  in ${cnt} node(s)` + (rec.sample !== undefined ? `  e.g. ${rec.sample}` : ''));
      }
    }
  }
}

main().then(async () => { await mongo.close(); }).catch(async (e) => {
  console.error(e); await mongo.close().catch(() => {}); process.exit(1);
});
