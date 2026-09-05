// mongo-generator.js — Rich event variety: inserts, updates, deletes, status progressions
// Called every 60 s by the data-generator-mongo container.

const chaosRate = (typeof CHAOS_RATE !== 'undefined' && CHAOS_RATE !== null)
  ? (Number(CHAOS_RATE) || 0) : 0;

const cities     = ['Jakarta','Surabaya','Bandung','Medan','Makassar','Semarang','Palembang','Denpasar'];
const categories = ['Electronics','Fashion','Food','Home','Books','Beauty','Automotive','Sports'];
const statuses   = ['pending','processing','shipped','delivered','cancelled','refunded'];

// ── INSERT: new products ─────────────────────────────────────────────────────
const numNewProducts = Math.floor(Math.random() * 8) + 3;
let productsCreated = 0;
for (let i = 0; i < numNewProducts; i++) {
  db.products.insertOne({
    name:       'Product_' + new Date().toISOString() + '_' + i,
    price:      Math.floor(Math.random() * 495) * 10000 + 50000,
    stock:      Math.floor(Math.random() * 200) + 10,
    category:   categories[Math.floor(Math.random() * categories.length)],
    created_at: new Date()
  });
  productsCreated++;
}

// ── INSERT: new users ────────────────────────────────────────────────────────
const numNewUsers = Math.floor(Math.random() * 11) + 5;
let usersCreated = 0;
for (let i = 0; i < numNewUsers; i++) {
  db.users.insertOne({
    name:       'User_' + new Date().toISOString() + '_' + i,
    email:      'user_' + Date.now() + '_' + i + '@example.com',
    city:       cities[Math.floor(Math.random() * cities.length)],
    is_active:  true,
    created_at: new Date()
  });
  usersCreated++;
}

// ── INSERT: new orders ───────────────────────────────────────────────────────
const products = db.products.find().toArray();
const users    = db.users.find().toArray();

const numNewOrders = Math.floor(Math.random() * 41) + 30;
let ordersCreated = 0;

if (products.length > 0 && users.length > 0) {
  for (let i = 0; i < numNewOrders; i++) {
    const prod = products[Math.floor(Math.random() * products.length)];
    const user = users[Math.floor(Math.random() * users.length)];
    const qty  = Math.floor(Math.random() * 5) + 1;

    const stockResult = db.products.updateOne(
      { _id: prod._id, stock: { $gte: qty } },
      { $inc: { stock: -qty } }
    );
    if (stockResult.matchedCount === 0) continue;

    const sig = (user._id.toString() + prod._id.toString()).substring(0, 12);
    db.orders.insertOne({
      user_id:        user._id,
      product_id:     prod._id,
      quantity:       qty,
      total_price:    prod.price * qty,
      status:         'pending',
      created_at:     new Date(),
      hash_signature: (chaosRate > 0 && Math.random() < chaosRate)
                        ? sig.split('').reverse().join('')
                        : sig
    });
    ordersCreated++;
  }
}

// ── UPDATE: order status progression ────────────────────────────────────────
const numStatusUpdates = Math.floor(Math.random() * 12) + 5;
const progressable = db.orders.find({ status: { $in: ['pending','processing','shipped'] } }).toArray();
for (let i = 0; i < Math.min(numStatusUpdates, progressable.length); i++) {
  const ord  = progressable[Math.floor(Math.random() * progressable.length)];
  const next = { pending: 'processing', processing: 'shipped', shipped: 'delivered' }[ord.status];
  if (next) db.orders.updateOne({ _id: ord._id }, { $set: { status: next, updated_at: new Date() } });
}

// ── UPDATE: product price fluctuation ───────────────────────────────────────
const numPriceChanges = Math.floor(Math.random() * 6) + 2;
for (let i = 0; i < numPriceChanges; i++) {
  const prod = products[Math.floor(Math.random() * products.length)];
  const factor = 0.85 + Math.random() * 0.30;   // ±15 % swing
  db.products.updateOne(
    { _id: prod._id },
    { $set: { price: Math.round(prod.price * factor), updated_at: new Date() } }
  );
}

// ── UPDATE: restock low-inventory products ───────────────────────────────────
db.products.updateMany({ stock: { $lt: 20 } }, { $inc: { stock: 100 }, $set: { updated_at: new Date() } });

// ── UPDATE: deactivate churned users (no orders in last 30 days, random sample) ──
if (Math.random() < 0.2) {
  const thirtyDaysAgo = new Date(Date.now() - 30 * 24 * 3600 * 1000);
  const recentBuyers  = db.orders
    .find({ created_at: { $gte: thirtyDaysAgo } }, { projection: { user_id: 1 } })
    .toArray()
    .map(o => o.user_id.toString());
  const candidate = db.users.findOne({
    is_active: true,
    created_at: { $lt: thirtyDaysAgo },
    _id: { $nin: recentBuyers.map(id => { try { return new ObjectId(id); } catch(e) { return id; } }) }
  });
  if (candidate) {
    db.users.updateOne({ _id: candidate._id }, { $set: { is_active: false, deactivated_at: new Date() } });
  }
}

// ── DELETE: purge a few old cancelled/refunded orders ───────────────────────
const numDeletes = Math.floor(Math.random() * 4);   // 0..3
if (numDeletes > 0) {
  const toDelete = db.orders
    .find({ status: { $in: ['cancelled','refunded'] } })
    .sort({ created_at: 1 })
    .limit(numDeletes)
    .toArray();
  if (toDelete.length > 0) {
    db.orders.deleteMany({ _id: { $in: toDelete.map(o => o._id) } });
  }
}

// ── DELETE: remove a couple of fully inactive users (no orders at all) ───────
if (Math.random() < 0.10) {
  const allBuyers = db.orders.distinct('user_id');
  db.users.deleteOne({
    is_active: false,
    _id: { $nin: allBuyers }
  });
}

print(`${productsCreated},${usersCreated},${ordersCreated}`);