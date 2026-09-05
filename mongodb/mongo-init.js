// Collections + sample data
db.createCollection("products");
db.createCollection("orders");
db.createCollection("users");

db.products.insertMany([
  { name: "Laptop Gaming RTX", price: 15000000, category: "elektronik", stock: 20 },
  { name: "SSD 1TB NVMe Gen4", price: 1200000, category: "storage", stock: 50 },
  { name: "Mouse Wireless RGB", price: 250000, category: "peripheral", stock: 100 },
  { name: "Headset Gaming", price: 800000, category: "audio", stock: 30 }
]);

db.users.insertMany([
  { name: "Budi Santoso", email: "budi@email.com", city: "Jakarta", loyalty_points: 150 },
  { name: "Siti Aminah", email: "siti@email.com", city: "Surabaya", loyalty_points: 230 },
  { name: "Ahmad Fauzi", email: "ahmad@email.com", city: "Bandung", loyalty_points: 89 }
]);

print("MongoDB initialized! Collections ready.");
