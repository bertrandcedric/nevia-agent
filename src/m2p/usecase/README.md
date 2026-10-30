# The hello data

nevia-helloworld's customers, as agent/uc3 seeds them: one MongoDB collection, `hello.customers`, one
document per customer with its addresses, contacts, orders and their items, tags and preferences.

- `seed.py` generates it on the source computer from `[m2p.seed]` in `agent.toml`
  (`python src/m2p/usecase/seed.py --help`).
- `DESCRIPTION` (in `__init__.py`) describes it in the agent's guide.
- `__init__.py` has the `[m2p.seed]` settings and what `setup` needs to seed the source.

## The two shapes

A customer document (`C-S00036` with seed.py's defaults, its other contacts, address, orders and
items cut), and the PostgreSQL tables mongo2pg makes of it (schema `hello`, database `hello`):

```js
{
  _id: "C-S00036",
  type: "company",
  name: "Acme Immobilier",
  email: "contact@acme-immobilier.example",
  phone: "+33 6 69 80 27 14",
  status: "prospect",
  tags: ["loyalty", "new"],
  addresses: [
    {
      kind: "billing",
      street: "26 boulevard Haussmann",
      city: "Marseille",
      postalCode: "13001",
      country: "FR"
    },
    ...
  ],
  contacts: [
    {
      firstName: "Hugo",
      lastName: "Martin",
      role: "Achats",
      email: "h.martin@acme-immobilier.example",
      phone: "+33 1 40 00 39 55"
    },
    ...
  ],
  orders: [
    {
      orderId: "O-36-1",
      date: ISODate("2024-05-18T09:19:23.332Z"),
      status: "delivered",
      items: [
        { sku: "CHA-100", label: "Chaise de bureau", quantity: 27, unitPrice: 89.9 },
        ...
      ]
    },
    ...
  ],
  preferences: { language: "de", newsletter: true, channels: ["phone", "sms"] },
  createdAt: ISODate("2024-10-31T10:28:12.252Z"),
  updatedAt: ISODate("2024-12-26T10:28:12.252Z")
}
```

| MongoDB | PostgreSQL |
|---|---|
| the document | `customers`: `id` (the `_id`), `type`, `name`, `email`, `phone`, `status`, `tags TEXT[]`, `createdat`, `updatedat` |
| `addresses[]` | `addresses`: `customers_id`, `kind`, `street`, `city`, `postalcode`, `country` |
| `contacts[]` | `contacts`: `customers_id`, `firstname`, `lastname`, `role`, `email`, `phone` |
| `orders[]` | `orders`: `id`, `customers_id`, `orderid`, `date`, `status` |
| `orders[].items[]` | `items`: `orders_id` (the `orders.id`), `sku`, `label`, `quantity`, `unitprice` |
| `preferences` | `preferences`: `customers_id`, `language`, `newsletter`, `channels TEXT[]` |

Field names lose their capitals (`unitPrice` becomes `unitprice`). An optional field (a phone, an
order's date) is left out of the document when it has no value, and is `NULL` in PostgreSQL; an empty
list (`contacts: []`) has no child row.

With seed.py's defaults (5000 customers, seed 42) and no strange kinds, both sides hold 5000 customers, 5000 addresses,
3051 contacts, 20015 orders, 69999 items and 2983 preferences. Anomalies (`strange_kinds`) change these.

## Connecting

MongoDB listens only inside its pod on `m2p-mongo`:

```sh
nevia exec m2p-mongo -- podman exec m2p-mongo mongosh --quiet hello --eval 'db.customers.countDocuments()'
```

PostgreSQL: `m2p-pg`, or an `m2p-tryN` branch. The URI holds the password, so keep it out of logs
and shell history:

```sh
psql "$(nevia pg connect m2p-pg --uri)"
```

then `\c hello` and `SET search_path = hello;` before the queries below.

## Queries side by side

**How many customers**

```js
db.customers.countDocuments()
```
```sql
SELECT count(*) FROM customers;
```

**One customer, all of it**

```js
db.customers.findOne({ _id: "C-S00036" })
```
```sql
SELECT * FROM customers WHERE id = 'C-S00036';
SELECT * FROM addresses WHERE customers_id = 'C-S00036';
SELECT * FROM orders o LEFT JOIN items i ON i.orders_id = o.id WHERE o.customers_id = 'C-S00036';
```

**Customers by type and status**

```js
db.customers.aggregate([
  { $group: { _id: { type: "$type", status: "$status" }, n: { $sum: 1 } } },
  { $sort: { n: -1 } }
])
```
```sql
SELECT type, status, count(*) AS n FROM customers GROUP BY type, status ORDER BY n DESC;
```

**How many orders**

```js
db.customers.aggregate([
  { $group: { _id: null, orders: { $sum: { $size: { $ifNull: ["$orders", []] } } } } }
])
```
```sql
SELECT count(*) FROM orders;
```

**Quantity and revenue per product**

```js
db.customers.aggregate([
  { $unwind: "$orders" }, { $unwind: "$orders.items" },
  { $group: { _id: "$orders.items.sku",
              quantity: { $sum: "$orders.items.quantity" },
              revenue: { $sum: { $multiply: ["$orders.items.quantity", "$orders.items.unitPrice"] } } } },
  { $sort: { revenue: -1 } }
])
```
```sql
SELECT sku, sum(quantity) AS quantity, round(sum(quantity * unitprice)::numeric, 2) AS revenue
FROM items GROUP BY sku ORDER BY revenue DESC;
```

**A customer's orders and their totals**

```js
db.customers.aggregate([
  { $match: { _id: "C-S00036" } }, { $unwind: "$orders" },
  { $project: { orderId: "$orders.orderId", date: "$orders.date",
                total: { $sum: { $map: { input: "$orders.items", as: "i",
                                         in: { $multiply: ["$$i.quantity", "$$i.unitPrice"] } } } } } },
  { $sort: { date: 1 } }
])
```
```sql
SELECT o.orderid, o.date, sum(i.quantity * i.unitprice) AS total
FROM orders o JOIN items i ON i.orders_id = o.id
WHERE o.customers_id = 'C-S00036'
GROUP BY o.id, o.orderid, o.date ORDER BY o.date;
```

**Billing addresses per city**

```js
db.customers.aggregate([
  { $unwind: "$addresses" }, { $match: { "addresses.kind": "billing" } },
  { $group: { _id: "$addresses.city", n: { $sum: 1 } } }, { $sort: { n: -1 } }
])
```
```sql
SELECT city, count(*) AS n FROM addresses WHERE kind = 'billing' GROUP BY city ORDER BY n DESC;
```

**Customers per tag** (a list of strings: an array column, not a child table)

```js
db.customers.aggregate([
  { $unwind: "$tags" }, { $group: { _id: "$tags", n: { $sum: 1 } } }, { $sort: { n: -1 } }
])
```
```sql
SELECT tag, count(*) AS n FROM customers, unnest(tags) AS tag GROUP BY tag ORDER BY n DESC;
```

**Newsletter subscribers**

```js
db.customers.countDocuments({ "preferences.newsletter": true })
```
```sql
SELECT count(*) FROM preferences WHERE newsletter;
```

**Newsletter flags stored as strings** (the `bool-string` anomaly: `"yes"`, `"no"`, `"true"`, `"1"`)

```js
db.customers.aggregate([
  { $match: { "preferences.newsletter": { $type: "string" } } },
  { $group: { _id: "$preferences.newsletter", n: { $sum: 1 } } }
])
```

In PostgreSQL, the type of `preferences.newsletter` and `normalize.sql`, if the agent wrote one, say
how they came out.

**Order dates stored as strings** (the `date-string` anomaly)

```js
db.customers.aggregate([
  { $unwind: "$orders" }, { $match: { "orders.date": { $type: "string" } } },
  { $project: { orderId: "$orders.orderId", date: "$orders.date" } }
])
```

In PostgreSQL, how these come out depends on what the agent did with the `orders.date` column. Its type
says:

```sql
SELECT data_type FROM information_schema.columns
WHERE table_schema = 'hello' AND table_name = 'orders' AND column_name = 'date';
```
