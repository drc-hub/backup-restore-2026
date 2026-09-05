DO $$
DECLARE
    num_new_customers   INT;
    num_new_products    INT;
    num_new_orders      INT;
    num_updates         INT;
    num_deletes         INT;
    num_price_changes   INT;
    cust_count          INT;
    prod_count          INT;
    order_count         INT;
    i                   INT;
    v_order_id          INT;
    v_prod_id           INT;
    v_cust_id           INT;
    v_status            TEXT;
    payment_methods     TEXT[] := ARRAY['GoPay','OVO','DANA','LinkAja','Credit Card','QRIS','ShopeePay','BCA Virtual Account'];
    order_statuses      TEXT[] := ARRAY['pending','processing','shipped','delivered','cancelled','refunded'];
    cities              TEXT[] := ARRAY['Jakarta','Surabaya','Bandung','Medan','Makassar','Semarang','Palembang','Denpasar'];
    categories          TEXT[] := ARRAY['Elektronik','Fashion','Food','Home','Sports','Beauty','Automotive','Books'];
    chaos_rate          DOUBLE PRECISION := 0;
    event_type          INT;
BEGIN
    -- Read optional chaos rate
    BEGIN
        chaos_rate := COALESCE(NULLIF(current_setting('drsim.chaos_rate', true), ''), '0')::DOUBLE PRECISION;
    EXCEPTION WHEN OTHERS THEN
        chaos_rate := 0;
    END;

    -- ── INSERT: new products ─────────────────────────────────────────────
    num_new_products := (RANDOM() * 8)::INT + 3;   -- 3..10
    FOR i IN 1..num_new_products LOOP
        INSERT INTO products (name, price, category, stock)
        VALUES (
            'Product_' || TO_CHAR(NOW(), 'YYYYMMDD_HH24MISS') || '_' || i,
            (RANDOM() * 4950000 + 50000)::NUMERIC(10,2),
            categories[(1 + FLOOR(RANDOM() * array_length(categories,1)))::INT],
            (RANDOM() * 200)::INT + 10
        );
    END LOOP;

    -- ── INSERT: new customers ────────────────────────────────────────────
    num_new_customers := (RANDOM() * 11)::INT + 5;  -- 5..15
    FOR i IN 1..num_new_customers LOOP
        INSERT INTO customers (name, email, address)
        VALUES (
            'Customer_' || TO_CHAR(NOW(), 'YYYYMMDD_HH24MISS') || '_' || i,
            'cust_' || TO_CHAR(NOW(), 'HH24MISS') || '_' || i || '@example.com',
            cities[(1 + FLOOR(RANDOM() * array_length(cities,1)))::INT]
        );
    END LOOP;

    cust_count  := (SELECT COUNT(*) FROM customers);
    prod_count  := (SELECT COUNT(*) FROM products);
    order_count := (SELECT COUNT(*) FROM orders);

    -- ── INSERT: new orders + payments ────────────────────────────────────
    num_new_orders := (RANDOM() * 41)::INT + 30;   -- 30..70
    IF cust_count > 0 AND prod_count > 0 THEN
        FOR i IN 1..num_new_orders LOOP
            INSERT INTO orders (user_id, amount)
            VALUES (
                (FLOOR(RANDOM() * cust_count) + 1)::INT,
                (RANDOM() * 5000000 + 50000)::NUMERIC(10,2)
            ) RETURNING id INTO v_order_id;

            UPDATE orders
            SET integrity_hash = CASE
                WHEN chaos_rate > 0 AND RANDOM() < chaos_rate THEN
                    md5('CORRUPT|' || v_order_id::text || '|' || (SELECT created_at::text FROM orders WHERE id = v_order_id))
                ELSE
                    md5(v_order_id::text || '|' || (SELECT created_at::text FROM orders WHERE id = v_order_id))
            END
            WHERE id = v_order_id;

            IF RANDOM() < 0.85 THEN
                INSERT INTO payments (order_id, amount, payment_method)
                SELECT
                    v_order_id,
                    (SELECT amount FROM orders WHERE id = v_order_id),
                    payment_methods[(1 + FLOOR(RANDOM() * array_length(payment_methods,1)))::INT];
            END IF;
        END LOOP;
    END IF;

    -- ── UPDATE: order status progression ────────────────────────────────
    -- Advance 5–15 random pending/processing orders to the next status
    num_updates := (RANDOM() * 11)::INT + 5;
    IF order_count > 0 THEN
        FOR i IN 1..num_updates LOOP
            SELECT id, status INTO v_order_id, v_status
            FROM orders
            WHERE status IN ('pending','processing','shipped')
            ORDER BY RANDOM()
            LIMIT 1;

            IF FOUND THEN
                v_status := CASE v_status
                    WHEN 'pending'    THEN 'processing'
                    WHEN 'processing' THEN 'shipped'
                    WHEN 'shipped'    THEN 'delivered'
                    ELSE 'delivered'
                END;
                UPDATE orders SET status = v_status WHERE id = v_order_id;
            END IF;
        END LOOP;
    END IF;

    -- ── UPDATE: product price adjustments ───────────────────────────────
    num_price_changes := (RANDOM() * 6)::INT + 2;  -- 2..7 products get a price change
    IF prod_count > 0 THEN
        FOR i IN 1..num_price_changes LOOP
            SELECT id INTO v_prod_id FROM products ORDER BY RANDOM() LIMIT 1;
            UPDATE products
            SET price = (price * (0.85 + RANDOM() * 0.30))::NUMERIC(10,2)   -- ±15 % swing
            WHERE id = v_prod_id;
        END LOOP;
    END IF;

    -- ── UPDATE: restock low-inventory products ───────────────────────────
    UPDATE products SET stock = stock + 100 WHERE stock < 20;

    -- ── DELETE: cancel + purge a handful of old pending orders ──────────
    num_deletes := (RANDOM() * 4)::INT;   -- 0..3
    IF num_deletes > 0 AND order_count > 10 THEN
        DELETE FROM payments
        WHERE order_id IN (
            SELECT id FROM orders
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT num_deletes
        );
        DELETE FROM orders
        WHERE id IN (
            SELECT id FROM orders
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT num_deletes
        );
    END IF;

    -- ── DELETE: remove a small number of inactive customers ──────────────
    -- Only remove customers who have no orders (referential integrity safe).
    IF RANDOM() < 0.15 THEN   -- ~15% chance per tick
        DELETE FROM customers
        WHERE id NOT IN (SELECT DISTINCT user_id FROM orders)
        AND ctid IN (
            SELECT ctid FROM customers
            WHERE id NOT IN (SELECT DISTINCT user_id FROM orders)
            ORDER BY created_at
            LIMIT 2
        );
    END IF;

    RAISE NOTICE 'CUSTOM_STATS:%,%,%', num_new_customers, num_new_products, num_new_orders;
END $$;