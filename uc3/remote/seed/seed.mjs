// The customers of the database hello, as nevia-helloworld stores them (support/Documents): one
// document per customer, with its addresses, contacts, orders and their items, tags and
// preferences. The same SEED gives the same customers, ids C-S00001 and up; the collection is
// replaced, so a seed gives exactly SEED_CUSTOMERS of them.
// What makes them a test for mongo2pg: optional fields (phone, preferences, an order's date and
// status), lists of every length, empty ones included, and numbers with and without decimals.
// And SEED_STRANGE percent of them are strange: one to three anomalies each (STRANGE below), as
// a database that several app versions and imports wrote to has them, e.g. a number as a string,
// an object where a list should be, an ObjectId for an id. They are drawn from a random stream of
// their own: the same SEED gives the same normal data whatever SEED_STRANGE.
// Env: MONGO_URI, MONGO_DB, SEED_CUSTOMERS (5000), SEED (42), SEED_STRANGE (5, percent),
// SEED_STRANGE_KINDS (all, or a comma-separated list of STRANGE's kinds).
import { MongoClient, ObjectId, Decimal128, Long } from 'mongodb';

const count = Number(process.env.SEED_CUSTOMERS || 5000);
const seed = Number(process.env.SEED || 42) >>> 0;
const strangePct = Number(process.env.SEED_STRANGE ?? 5);
if (!(count >= 0) || !(strangePct >= 0 && strangePct <= 100)) throw new Error('SEED_CUSTOMERS must be 0 or more, SEED_STRANGE 0 to 100');

// mulberry32: small, deterministic.
const stream = (state) => () => {
  state = (state + 0x6d2b79f5) >>> 0;
  let t = state;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
let rand = stream(seed);
const int = (min, max) => min + Math.floor(rand() * (max - min + 1));
const pick = (list) => list[int(0, list.length - 1)];
const some = (list, max) => [...new Set(Array.from({ length: int(0, max) }, () => pick(list)))];
const maybe = (p, value) => (rand() < p ? value() : undefined);
const date = (from, to) => new Date(from + rand() * (to - from));

const FIRST = ['Claire', 'Hugo', 'Léa', 'Lucas', 'Chloé', 'Nathan', 'Inès', 'Théo', 'Zoé', 'Jules', 'Anaïs', 'Noé'];
const LAST = ['Martin', 'Bernard', 'Dubois', 'Thomas', 'Robert', 'Richard', 'Petit', 'Durand', 'Leroy', 'Moreau'];
const COMPANY = ['Acme', 'Globex', 'Initech', 'Umbrella', 'Hooli', 'Stark', 'Wayne', 'Soylent', 'Tyrell', 'Cyberdyne'];
const SECTOR = ['Distribution', 'Logistique', 'Industries', 'Services', 'Conseil', 'Immobilier'];
const CITY = [['Paris', '75004'], ['Lyon', '69002'], ['Marseille', '13001'], ['Lille', '59000'], ['Nantes', '44000'],
  ['Bordeaux', '33000'], ['Toulouse', '31000'], ['Strasbourg', '67000']];
const STREET = ['rue de Rivoli', 'place Bellecour', 'avenue Jean Jaurès', 'boulevard Haussmann', 'quai des Chartrons'];
const ROLE = ['Achats', 'Comptabilité', 'Direction', 'Logistique', 'Support'];
const TAG = ['retail', 'vip', 'online', 'wholesale', 'b2b', 'loyalty', 'new'];
const PRODUCT = [['CHA-100', 'Chaise de bureau', 89.9], ['BUR-200', 'Bureau 160 cm', 249], ['LAM-010', 'Lampe LED', 24.5],
  ['LIV-501', 'Coffret livres', 59], ['CAR-002', 'Carte cadeau', 25], ['ECR-270', 'Écran 27"', 319.99], ['CLA-090', 'Clavier', 49.95]];
const ORDER_STATUS = ['pending', 'paid', 'shipped', 'delivered', 'cancelled'];
const DAY = 86400000;
const START = Date.parse('2023-01-01T00:00:00Z');
const END = Date.parse('2025-06-30T00:00:00Z');

function customer(n) {
  const company = rand() < 0.4;
  const first = pick(FIRST);
  const last = pick(LAST);
  const name = company ? `${pick(COMPANY)} ${pick(SECTOR)}` : `${first} ${last}`;
  const domain = company ? `${name.toLowerCase().replace(/[^a-z]+/g, '-')}.example` : 'mail.example';
  const email = company ? `contact@${domain}` : `${first}.${last}.${n}@${domain}`.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
  const address = (kind) => {
    const [city, postalCode] = pick(CITY);
    return { kind, street: `${int(1, 120)} ${pick(STREET)}`, city, postalCode, country: 'FR' };
  };
  let orderNo = 0;
  const createdAt = date(START, END);
  return {
    _id: `C-S${String(n).padStart(5, '0')}`,
    type: company ? 'company' : 'individual',
    name,
    email,
    phone: maybe(0.7, () => `+33 6 ${String(int(10, 99))} ${String(int(10, 99))} ${String(int(10, 99))} ${String(int(10, 99))}`),
    status: pick(['prospect', 'active', 'active', 'inactive']),
    tags: some(TAG, 3),
    addresses: ['billing', 'shipping'].slice(0, int(0, 2)).map(address),
    contacts: Array.from({ length: company ? int(0, 3) : 0 }, () => {
      const f = pick(FIRST), l = pick(LAST);
      return { firstName: f, lastName: l, role: maybe(0.8, () => pick(ROLE)),
        email: maybe(0.8, () => `${f[0]}.${l}@${domain}`.toLowerCase()), phone: maybe(0.4, () => `+33 1 40 00 ${int(10, 99)} ${int(10, 99)}`) };
    }),
    orders: Array.from({ length: int(0, 8) }, () => ({
      orderId: `O-${n}-${++orderNo}`,
      date: maybe(0.9, () => date(START, END)),
      status: maybe(0.9, () => pick(ORDER_STATUS)),
      items: Array.from({ length: int(1, 6) }, () => {
        const [sku, label, unitPrice] = pick(PRODUCT);
        return { sku, label, quantity: int(1, 30), unitPrice };
      }),
    })),
    preferences: maybe(0.6, () => ({ language: pick(['fr', 'en', 'de']), newsletter: rand() < 0.5, channels: some(['email', 'sms', 'phone'], 2) })),
    createdAt,
    updatedAt: new Date(createdAt.getTime() + int(0, 90) * DAY),
  };
}

// The anomalies: kind -> what it does to a customer (after clean: it may put a null, an ObjectId...).
// Each one, when it needs an element (an order, an item...) and the customer has none, adds one,
// and takes the field as an earlier one of the same customer may have left it.
const anOrder = (c) => (c.orders.length ? pick(c.orders)
  : (c.orders.push({ orderId: `O-${c._id}-x`, items: [{ sku: 'CHA-100', label: 'Chaise de bureau', quantity: 1, unitPrice: 89.9 }] }), c.orders[0]));
const anItem = (c) => pick(anOrder(c).items);
const anAddress = (c) => {
  if (c.addresses && !Array.isArray(c.addresses)) return c.addresses;
  if (!c.addresses.length) c.addresses.push({ kind: 'billing', street: '1 rue de la Paix', city: 'Paris', postalCode: '75002', country: 'FR' });
  return pick(c.addresses);
};
const tags = (c) => (Array.isArray(c.tags) ? c.tags : [c.tags]);
const STRANGE = {
  'phone-number': (c) => { c.phone = 33600000000 + int(0, 99999999); },
  'quantity-string': (c) => { const i = anItem(c); i.quantity = String(typeof i.quantity === 'number' ? i.quantity : 1); },
  'quantity-long': (c) => { anItem(c).quantity = Long.fromString(String(2 ** 40 + int(0, 1000))); },
  'quantity-negative': (c) => { anItem(c).quantity = -int(1, 5); },
  'price-string': (c) => { const i = anItem(c); i.unitPrice = (typeof i.unitPrice === 'number' ? i.unitPrice : 9.9).toFixed(2).replace('.', ','); },
  'price-decimal128': (c) => { const i = anItem(c); i.unitPrice = Decimal128.fromString((typeof i.unitPrice === 'number' ? i.unitPrice : 9.9).toFixed(2)); },
  'date-string': (c) => { const o = anOrder(c); const d = o.date instanceof Date ? o.date : new Date(START); o.date = pick([d.toISOString().slice(0, 10), d.toLocaleDateString('fr-FR')]); },
  'date-epoch': (c) => { c.createdAt = c.createdAt instanceof Date ? c.createdAt.getTime() : START; },
  'null-fields': (c) => { c.phone = null; c.preferences = null; },
  'empty-strings': (c) => { c.name = ''; c.email = ''; },
  'missing-required': (c) => { pick([() => delete c.email, () => delete c.type, () => delete c.name])(); },
  'enum-case': (c) => { c.status = String(c.status).toUpperCase(); if (c.type) c.type = c.type[0].toUpperCase() + c.type.slice(1); },
  'unknown-fields': (c) => { c._class = 'com.example.legacy.Customer'; c.legacyId = int(1, 99999); c.importedAt = new Date(END); },
  'long-text': (c) => { c.notes = 'Lorem ipsum dolor sit amet, '.repeat(int(200, 800)); },
  'unicode': (c) => { c.name = `${c.name ?? ''} 🏢 ​ שלום 漢字`; },
  'object-not-array': (c) => { c.addresses = anAddress(c); },
  'scalar-not-array': (c) => { c.tags = tags(c).filter((t) => typeof t === 'string').join(',') || 'vip'; },
  'mixed-array': (c) => { c.tags = [...tags(c), int(1, 9), null, { label: 'vip' }]; },
  'nested-deeper': (c) => { c.preferences = { language: 'fr', newsletter: true, channels: [{ type: 'email', optIn: true, since: new Date(START) }] }; },
  'empty-object': (c) => { c.preferences = {}; },
  'bool-string': (c) => { c.preferences = { ...(c.preferences || { language: 'fr', channels: [] }), newsletter: pick(['yes', 'no', 'true', '1']) }; },
  'id-objectid': (c) => { c._id = new ObjectId(); },
  'duplicate-order-id': (c) => { const o = anOrder(c); c.orders.push({ ...o, items: o.items.map((i) => ({ ...i })) }); },
  'odd-field-names': (c) => { const a = anAddress(c); a['Code postal'] = a.postalCode; delete a.postalCode; a['pays.iso'] = a.country; },
};
const wanted = (process.env.SEED_STRANGE_KINDS || 'all') === 'all' ? Object.keys(STRANGE)
  : process.env.SEED_STRANGE_KINDS.split(',').map((k) => k.trim()).filter(Boolean);
const unknown = wanted.filter((k) => !STRANGE[k]);
if (unknown.length || !wanted.length) throw new Error(`SEED_STRANGE_KINDS: unknown ${unknown.join(', ') || '(none given)'}; kinds: ${Object.keys(STRANGE).join(', ')}`);

// Absent fields left out, as the app does (absent and null are the same to it).
const clean = (v) => Array.isArray(v) ? v.map(clean)
  : v && typeof v === 'object' && !(v instanceof Date)
    ? Object.fromEntries(Object.entries(v).filter(([, x]) => x !== undefined).map(([k, x]) => [k, clean(x)])) : v;
const normal = rand;
const strange = stream(seed ^ 0x5eed5);
const seen = {};
let strangeDocs = 0;
function next(n) {
  rand = normal;
  const c = clean(customer(n));
  rand = strange;
  if (rand() * 100 < strangePct) {
    strangeDocs++;
    for (const k of new Set(Array.from({ length: int(1, 3) }, () => pick(wanted)))) { STRANGE[k](c); seen[k] = (seen[k] || 0) + 1; }
  }
  return c;
}

const client = new MongoClient(process.env.MONGO_URI);
try {
  const customers = client.db(process.env.MONGO_DB || 'hello').collection('customers');
  await customers.drop().catch(() => {});
  for (let n = 1; n <= count; n += 1000) {
    await customers.insertMany(Array.from({ length: Math.min(1000, count - n + 1) }, (_, i) => next(n + i)), { ordered: true });
  }
  console.log(`[seed] ${await customers.countDocuments()} customers in ${customers.dbName}.customers, ${strangeDocs} strange (${strangePct}%)`);
  for (const [k, v] of Object.entries(seen).sort()) console.log(`[seed]   ${k}: ${v}`);
} finally {
  await client.close();
}
