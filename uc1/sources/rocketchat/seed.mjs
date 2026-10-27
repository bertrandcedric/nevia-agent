// Fills Rocket.Chat through its REST API with data as varied as a real workspace: users (some
// with a status), public channels (some with a topic), private groups, direct messages,
// discussions, messages with threads, mentions, markdown, links and attachments, then reactions,
// edits, pins and stars. The plan of rooms and messages is the same for a given SEED.
// Env: RC_URL, RC_ADMIN_USER, RC_ADMIN_PASS, SEED_USERS (40), SEED_CHANNELS (10), SEED_GROUPS (4),
// SEED_DMS (20), SEED_MESSAGES (3000), SEED (42), SEED_CONCURRENCY (6).
import { randomBytes } from 'node:crypto';

const env = (k, d) => process.env[k] ?? d;
const num = (k, d) => Number.parseInt(env(k, String(d)), 10);
const URL_ = env('RC_URL', 'http://127.0.0.1:3000');
const N = {
  users: num('SEED_USERS', 40), channels: num('SEED_CHANNELS', 10), groups: num('SEED_GROUPS', 4),
  dms: num('SEED_DMS', 20), messages: num('SEED_MESSAGES', 3000), concurrency: num('SEED_CONCURRENCY', 6),
};

// mulberry32: the same data for the same seed.
let s = num('SEED', 42) >>> 0;
const rand = () => { s = (s + 0x6d2b79f5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
const pick = (a) => a[Math.floor(rand() * a.length)];
const chance = (p) => rand() < p;
const sample = (a, n) => [...a].sort(() => rand() - 0.5).slice(0, n);

const FIRST = ['Alice', 'Bruno', 'Chloé', 'David', 'Emma', 'Farid', 'Gaëlle', 'Hugo', 'Inès', 'Jules', 'Karim', 'Léa', 'Malik', 'Nina', 'Oscar', 'Paula', 'Quentin', 'Rosa', 'Samir', 'Tara'];
const LAST = ['Martin', 'Bernard', 'Dubois', 'Nguyen', 'Garcia', 'Müller', 'Rossi', 'Kowalski', "O'Brien", 'Silva', 'Haddad', 'Leroy'];
const TOPICS = ['general', 'dev', 'ops', 'support', 'sales', 'design', 'random', 'release', 'oncall', 'data', 'security', 'hr', 'infra', 'qa'];
const WORDS = 'the deploy is green again we should check the logs before the release can you review my PR tomorrow the database migration took longer than expected let us sync after lunch the customer reported a timeout on the API I pushed a fix please retry the build is flaky on CI who owns this service the dashboard shows a spike in latency'.split(' ');
const EMOJI = [':thumbsup:', ':tada:', ':eyes:', ':rocket:', ':heart:', ':joy:', ':white_check_mark:', ':fire:'];
const STATUS = ['online', 'away', 'busy'];

async function api(auth, method, path, body) {
  const headers = { 'content-type': 'application/json' };
  if (auth) Object.assign(headers, { 'X-Auth-Token': auth.token, 'X-User-Id': auth.id });
  for (let attempt = 1; ; attempt++) {
    const res = await fetch(`${URL_}/api/v1/${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
    const json = await res.json().catch(() => ({}));
    if (res.ok && json.success !== false) return json;
    if ((res.status === 429 || res.status >= 500) && attempt < 5) { await new Promise((r) => setTimeout(r, 1000 * attempt)); continue; }
    throw new Error(`${method} ${path}: ${res.status} ${json.error ?? json.message ?? ''}`);
  }
}
const login = async (user, password) => {
  const r = await api(null, 'POST', 'login', { user, password });
  return { id: r.data.userId, token: r.data.authToken, username: r.data.me.username };
};

// pool: run fn on each item, n at a time.
async function pool(items, n, fn) {
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const k = i++; await fn(items[k], k); }
  }));
}

const admin = await login(env('RC_ADMIN_USER', 'admin'), env('RC_ADMIN_PASS', ''));
console.log(`[seed] logged in as ${admin.username} on ${URL_}`);

// Users: created, or given a new password when they already exist (a second run).
const users = [];
await pool(Array.from({ length: N.users }, (_, k) => k + 1), N.concurrency, async (k) => {
  const username = `user${String(k).padStart(2, '0')}`;
  const password = randomBytes(12).toString('hex');
  const name = `${FIRST[k % FIRST.length]} ${LAST[(k * 7) % LAST.length]}`;
  try {
    await api(admin, 'POST', 'users.create', {
      username, name, password, email: `${username}@example.com`, verified: true, joinDefaultChannels: k % 3 !== 0,
      roles: k % 13 === 0 ? ['user', 'bot'] : ['user'],
    });
  } catch (e) {
    if (!/already in use|exists/i.test(e.message)) throw e;
    const { user } = await api(admin, 'GET', `users.info?username=${username}`);
    await api(admin, 'POST', 'users.update', { userId: user._id, data: { password } });
  }
  const auth = await login(username, password);
  if (k % 2 === 0) await api(auth, 'POST', 'users.setStatus', { status: STATUS[k % 3], message: pick(['in a meeting', 'on call 🔔', 'lunch', 'deep work']) });
  users[k - 1] = auth;
});
const byName = Object.fromEntries(users.map((u) => [u.username, u]));
console.log(`[seed] ${users.length} users`);

// Rooms: rid -> member usernames (the admin is in the channels and groups it creates).
const rooms = [];
const room = async (kind, name, members) => {
  const path = kind === 'c' ? 'channels' : 'groups';
  let r;
  try {
    r = (await api(admin, 'POST', `${path}.create`, { name, members }))[kind === 'c' ? 'channel' : 'group'];
  } catch (e) {
    if (!/already|exists|duplicate/i.test(e.message)) throw e;
    r = (await api(admin, 'GET', `${path}.info?roomName=${name}`))[kind === 'c' ? 'channel' : 'group'];
    await pool(members, N.concurrency, (m) => api(admin, 'POST', `${path}.invite`, { roomId: r._id, userId: byName[m].id }).catch(() => {}));
  }
  if (chance(0.6)) await api(admin, 'POST', `${path}.setTopic`, { roomId: r._id, topic: `All about ${name}` });
  if (chance(0.4)) await api(admin, 'POST', `${path}.setDescription`, { roomId: r._id, description: `Room for ${name} discussions, created by the seed` });
  rooms.push({ rid: r._id, kind, members: [admin.username, ...members], pinnable: true });
};
const names = users.map((u) => u.username);
for (let k = 0; k < N.channels; k++) await room('c', `${TOPICS[k % TOPICS.length]}${k >= TOPICS.length ? k : ''}`, sample(names, 5 + Math.floor(rand() * 20)));
for (let k = 0; k < N.groups; k++) await room('p', `private-${TOPICS[(k + 3) % TOPICS.length]}-${k}`, sample(names, 3 + Math.floor(rand() * 6)));
for (let k = 0; k < N.dms; k++) {
  const [a, b] = sample(names, 2);
  const r = (await api(byName[a], 'POST', 'im.create', { username: b })).room;
  rooms.push({ rid: r._id, kind: 'd', members: [a, b], pinnable: false });
}
// Discussions: a sub-room of a channel, with a few of its members.
for (const parent of rooms.filter((r) => r.kind === 'c').slice(0, 3)) {
  const users_ = sample(parent.members.filter((m) => m !== admin.username), 3);
  const d = (await api(admin, 'POST', 'rooms.createDiscussion', { prid: parent.rid, t_name: `Incident review ${parent.rid.slice(0, 4)}`, users: users_ })).discussion;
  rooms.push({ rid: d._id, kind: 'p', members: [admin.username, ...users_], pinnable: true });
}
console.log(`[seed] ${rooms.length} rooms (${N.channels} channels, ${N.groups} groups, ${N.dms} DMs, discussions)`);

const authOf = (username) => (username === admin.username ? admin : byName[username]);
const text = (r) => {
  let t = Array.from({ length: 3 + Math.floor(rand() * 18) }, () => pick(WORDS)).join(' ');
  if (chance(0.15)) t = `@${pick(r.members)} ${t}`;
  if (chance(0.05)) t += ' @here';
  if (chance(0.1)) t += ` ${pick(EMOJI)}`;
  if (chance(0.06)) t += '\n```\nSELECT count(*) FROM orders WHERE status = \'late\';\n```';
  if (chance(0.05)) t += ' see https://status.example.com/incidents/' + Math.floor(rand() * 9999);
  if (chance(0.04)) t = `**${t}**`;
  return t;
};

// Messages: replies go to an earlier message of the same room (threads), some carry attachments.
const posted = new Map(rooms.map((r) => [r.rid, []]));
const counts = { messages: 0, threads: 0, attachments: 0, reactions: 0, edits: 0, pins: 0, stars: 0 };
const plan = Array.from({ length: N.messages }, () => {
  const r = rooms[Math.floor(Math.pow(rand(), 1.6) * rooms.length)];
  return { r, author: pick(r.members), msg: text(r), thread: chance(0.15), attach: chance(0.05), seed: rand() };
});
await pool(plan, N.concurrency, async ({ r, author, msg, thread, attach, seed }) => {
  const earlier = posted.get(r.rid);
  const message = { rid: r.rid, msg };
  if (thread && earlier.length) { message.tmid = earlier[Math.floor(seed * earlier.length)]; counts.threads++; }
  if (attach) {
    message.attachments = [{ title: 'Build report', text: `job #${Math.floor(seed * 1e5)} finished`, color: seed > 0.5 ? '#2de0a5' : '#f5455c',
      fields: [{ short: true, title: 'Status', value: seed > 0.5 ? 'passed' : 'failed' }, { short: true, title: 'Duration', value: `${Math.floor(seed * 600)} s` }] }];
    counts.attachments++;
  }
  const m = (await api(authOf(author), 'POST', 'chat.sendMessage', { message })).message;
  counts.messages++;
  if (!message.tmid) earlier.push(m._id);
  const others = r.members.filter((u) => u !== author);
  if (chance(0.2)) for (const u of sample(others, 1 + Math.floor(rand() * 3))) {
    await api(authOf(u), 'POST', 'chat.react', { messageId: m._id, emoji: pick(EMOJI), shouldReact: true }); counts.reactions++;
  }
  if (chance(0.05)) { await api(authOf(author), 'POST', 'chat.update', { roomId: r.rid, msgId: m._id, text: `${msg} (edited)` }); counts.edits++; }
  if (r.pinnable && chance(0.02)) { await api(admin, 'POST', 'chat.pinMessage', { messageId: m._id }); counts.pins++; }
  if (others.length && chance(0.03)) { await api(authOf(pick(others)), 'POST', 'chat.starMessage', { messageId: m._id }); counts.stars++; }
  if (counts.messages % 500 === 0) console.log(`[seed] ${counts.messages}/${N.messages} messages`);
});
console.log(`[seed] done: ${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', ')}`);
