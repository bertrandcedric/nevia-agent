// The Rocket.Chat checks of verify.mjs (copied into the work directory at each run, {{SCHEMA}}
// filled in): for each one, the groups of an aggregation in MongoDB ({_id, n}) against the rows
// (k, n) of a query in Postgres. The tables and columns are those of the mapping this lab has
// passed with: if a run maps them differently, Claude adapts the SQL here and says so.
export default [
  {
    name: 'messages per room (rid)',
    collection: 'rocketchat_message',
    pipeline: [{ $group: { _id: '$rid', n: { $sum: 1 } } }],
    sql: 'SELECT rid AS k, count(*) AS n FROM "{{SCHEMA}}"."rocketchat_message" GROUP BY rid',
  },
  {
    name: 'thread replies per thread (tmid)',
    collection: 'rocketchat_message',
    pipeline: [{ $match: { tmid: { $ne: null } } }, { $group: { _id: '$tmid', n: { $sum: 1 } } }],
    sql: 'SELECT tmid AS k, count(*) AS n FROM "{{SCHEMA}}"."rocketchat_message" WHERE tmid IS NOT NULL GROUP BY tmid',
  },
  {
    // u: the child table of rocketchat_subscription.u, the subscribed user.
    name: 'subscriptions per user',
    collection: 'rocketchat_subscription',
    pipeline: [{ $group: { _id: '$u._id', n: { $sum: 1 } } }],
    sql: 'SELECT "_id" AS k, count(*) AS n FROM "{{SCHEMA}}"."u" GROUP BY "_id"',
  },
  {
    name: 'users by role',
    collection: 'users',
    pipeline: [{ $unwind: '$roles' }, { $group: { _id: '$roles', n: { $sum: 1 } } }],
    sql: 'SELECT r AS k, count(*) AS n FROM "{{SCHEMA}}"."users", unnest(roles) AS r GROUP BY r',
  },
];
