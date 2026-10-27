#!/usr/bin/env node
// The shape of the source, from every document (mongo2pg infers from a sample): for each
// collection and field path (a list's elements as path[]), how many documents hold it and the
// BSON types of its values, with the _id of a few documents per minority type. Generic: any
// MongoDB database of a mongo2pg project (from migrate/profile.mjs, copied into the work
// directory at each run). One run answers what one query per field would.
//   node profile.mjs [--collection C] [--all] [--examples N] [--max-docs N] [--project DIR]
// Prints the paths that mix types (all paths with --all), the rarest type first; the whole
// profile goes to profile.json next to it. Reads the source URI in the project's config itself:
// it never shows.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { MongoClient } from 'mongodb';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (n, d) => { const i = args.indexOf('--' + n); return i >= 0 && args[i + 1] ? args[i + 1] : d; };
const PROJECT = opt('project', '{{PROJECT}}');
const ONLY = opt('collection', null);
const ALL = args.includes('--all');
const EXAMPLES = Number(opt('examples', '3'));
const MAX_DOCS = Number(opt('max-docs', '0')); // 0: every document

function readSourceConfig() {
  const text = fs.readFileSync(path.join(PROJECT, 'config', path.basename(PROJECT) + '.toml'), 'utf8');
  let section = '', uri = null, namespace = null, exclude = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    const sec = line.match(/^\[([^\]]+)\]$/);
    if (sec) { section = sec[1]; continue; }
    if (section !== 'source') continue;
    const m = line.match(/^(\w+)\s*=\s*"(.*)"$/);
    if (m && m[1] === 'uri') uri = m[2];
    if (m && m[1] === 'namespace') namespace = m[2];
  }
  const ex = text.match(/^exclude\s*=\s*\[([^\]]*)\]/m);
  if (ex) exclude = [...ex[1].matchAll(/"((?:[^"\\]|\\.)*)"|'([^']*)'/g)].map((x) => x[1] ?? x[2]);
  if (!uri || !namespace) throw new Error('source uri/namespace not found in the config');
  return { uri, namespace, exclude };
}

// The BSON type of a value, as mongosh's $type names it.
function bsonType(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  if (v instanceof Date) return 'date';
  switch (typeof v) {
    case 'string': return 'string';
    case 'boolean': return 'bool';
    case 'number': return 'number';
    case 'bigint': return 'long';
    case 'object': {
      const t = v._bsontype;
      if (t === 'ObjectId' || t === 'ObjectID') return 'objectId';
      if (t === 'Decimal128') return 'decimal';
      if (t === 'Long') return 'long';
      if (t === 'Int32') return 'int';
      if (t === 'Double') return 'double';
      if (t === 'Binary') return 'binData';
      if (t === 'Timestamp') return 'timestamp';
      if (t) return t.toLowerCase();
      return 'object';
    }
    default: return typeof v;
  }
}

// walk: every value of a document, by path; a list counts once for its path and once per element
// for path[].
function walk(value, p, visit) {
  const t = bsonType(value);
  visit(p, t, value);
  if (t === 'array') for (const e of value) walk(e, p + '[]', visit);
  else if (t === 'object') for (const [k, v] of Object.entries(value)) walk(v, p ? `${p}.${k}` : k, visit);
}

const { uri, namespace, exclude } = readSourceConfig();
const client = new MongoClient(uri);
const profile = {};
try {
  const db = client.db(namespace);
  const names = (await db.listCollections({}, { nameOnly: true }).toArray()).map((c) => c.name)
    .filter((n) => !exclude.includes(n) && !n.startsWith('system.') && (!ONLY || n === ONLY)).sort();
  for (const name of names) {
    const paths = {};
    let docs = 0;
    // promoteValues off: an int, a double and a long keep their BSON type (not all JS numbers).
    const cursor = db.collection(name).find({}, { promoteValues: false, ...(MAX_DOCS ? { limit: MAX_DOCS } : {}) });
    for await (const doc of cursor) {
      docs++;
      const seen = new Set();
      for (const [k, v] of Object.entries(doc)) {
        walk(v, k, (p, t) => {
          const e = (paths[p] ??= { docs: 0, types: {} });
          if (!seen.has(p)) { seen.add(p); e.docs++; }
          const ty = (e.types[t] ??= { values: 0, examples: [] });
          ty.values++;
          if (ty.examples.length < EXAMPLES && !ty.examples.includes(String(doc._id))) ty.examples.push(String(doc._id));
        });
      }
    }
    profile[name] = { documents: docs, paths };
  }
} finally {
  await client.close();
}
fs.writeFileSync(path.join(HERE, 'profile.json'), JSON.stringify(profile, null, 1));

// The report: per collection, the paths that mix types (or all), each type with its share.
for (const [name, { documents, paths }] of Object.entries(profile)) {
  const rows = Object.entries(paths)
    .map(([p, e]) => [p, e, Object.entries(e.types).sort((a, b) => a[1].values - b[1].values)])
    .filter(([, , types]) => ALL || types.filter(([t]) => t !== 'null').length > 1 || types.some(([t]) => t === 'null'));
  console.log(`${name}: ${documents} documents, ${Object.keys(paths).length} paths, ${rows.length} ${ALL ? 'shown' : 'mixing types (or with nulls)'}`);
  for (const [p, e, types] of rows) {
    const total = types.reduce((s, [, ty]) => s + ty.values, 0);
    const parts = types.map(([t, ty]) => `${t} ${ty.values}${types.length > 1 && ty.values < total / 2 ? ` (e.g. ${ty.examples.join(', ')})` : ''}`);
    console.log(`  ${p}  [in ${e.docs} docs]  ${parts.join(' | ')}`);
  }
}
console.log('profile.json: every path, every type, examples');
