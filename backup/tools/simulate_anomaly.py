#!/usr/bin/env python3
"""
simulate_anomaly.py — Inject a sudden anomaly event into the live data systems.

Each anomaly type is designed to be clearly visible in the telemetry CSVs:
  - pg_events.csv      (sudden row-count drops / spikes)
  - mongo_events.csv   (sudden collection changes)
  - unstructured_events.csv (file count drop / large byte spike)
  - unstructured_growth.csv (explicit anomaly event rows)

Usage:
    python3 utilities/backup/tools/simulate_anomaly.py --type <TYPE> [OPTIONS]

Available anomaly types:
    pg_bulk_delete      — DELETE a large fraction of PostgreSQL rows (simulates accidental wipe)
    pg_corrupt_orders   — UPDATE integrity_hash to corrupt values (simulates logic error)
    pg_spike_insert     — Rapidly INSERT a large batch (simulates runaway process)
    mongo_bulk_delete   — deleteMany across orders collection
    mongo_corrupt_sigs  — Reverse hash_signature on random orders (simulates logic error)
    mongo_spike_insert  — Bulk insert documents (simulates runaway job)
    unstr_mass_delete   — Delete a large fraction of unstructured files
    unstr_overwrite     — Overwrite all blob files with random data (simulates corruption)
    unstr_flood_create  — Create many small files rapidly (simulates runaway log spam)
    all                 — Fire all anomaly types sequentially (full chaos scenario)
"""

import argparse
import os
import random
import string
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

JAKARTA_TZ = timezone(timedelta(hours=7))

def now_iso() -> str:
    return datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()

def rand_str(n: int = 8) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

# ---------------------------------------------------------------------------
# Log & telemetry helpers
# ---------------------------------------------------------------------------

LOG_FILE = '/home/primary/utilities/backup/backup.log'
UNSTR_CSV = '/home/primary/data/monitor/unstructured_growth.csv'
UNSTR_DIR = '/home/primary/data/unstructured'

def _log(scope: str, tag: str, msg: str) -> None:
    tag_color = {'<good>': '\x1b[32m', '<info>': '\x1b[36m',
                 '<warn>': '\x1b[33m', '<error>': '\x1b[31m'}
    no_color = os.environ.get('NO_COLOR') is not None or not sys.stdout.isatty()
    if no_color:
        print(f'{scope:<12} {tag:<8} {msg}')
    else:
        c = tag_color.get(tag, '')
        print(f'{scope:<12} {c}{tag}\x1b[0m {msg}')
    try:
        ts = now_iso()
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f'[{ts}] {scope:<12} {tag:<8} {msg}\n')
    except Exception:
        pass

def _log_unstr_event(action: str, target: str, bytes_changed: int,
                     status: str = 'ok', details: str = '') -> None:
    from pathlib import Path
    import csv
    header = ['timestamp','target','action','status','target_file',
              'bytes_changed','total_bytes','total_files','details']
    try:
        total_bytes = total_files = 0
        for root, _, files in os.walk(UNSTR_DIR):
            total_files += len(files)
            for f in files:
                try: total_bytes += os.path.getsize(os.path.join(root, f))
                except OSError: pass
        safe_details = (details or '').replace('\n',' ').replace(',',';')[:200]
        row = [now_iso(), 'unstructured', action, status, target,
               bytes_changed, total_bytes, total_files, safe_details]
        path = Path(UNSTR_CSV)
        write_header = not path.exists() or path.stat().st_size == 0
        with open(UNSTR_CSV, 'a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(header)
            w.writerow(row)
    except Exception:
        pass

def _run_pg(sql: str) -> tuple[bool, str]:
    cmd = [
        'docker', 'exec', '-i', 'postgres_live',
        'psql', '-U', 'postgresql', '-d', 'transactiondb',
        '-t', '-A', '-c', sql,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        return True, r.stdout.strip()
    except Exception as e:
        return False, str(e)

def _run_mongo(js: str) -> tuple[bool, str]:
    cmd = [
        'docker', 'exec', '-i', 'mongodb_live',
        'mongosh',
        'mongodb://mongodb:password@127.0.0.1:27017/test?authSource=admin',
        '--quiet', '--eval', js,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
        return True, r.stdout.strip()
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------------------------
# Anomaly implementations
# ---------------------------------------------------------------------------

def pg_bulk_delete(pct: float = 0.40) -> None:
    """Delete ~pct of orders and all associated payments."""
    _log('anomaly', '<warn>', f'pg_bulk_delete: targeting ~{int(pct*100)}% of orders')
    ok, out = _run_pg("SELECT COUNT(*) FROM orders;")
    if not ok:
        _log('anomaly', '<error>', f'pg count failed: {out}'); return
    total = int(out) if out.isdigit() else 0
    n = max(1, int(total * pct))
    sql = f"""
    WITH to_del AS (SELECT id FROM orders ORDER BY RANDOM() LIMIT {n})
    DELETE FROM payments WHERE order_id IN (SELECT id FROM to_del);
    WITH to_del AS (SELECT id FROM orders ORDER BY RANDOM() LIMIT {n})
    DELETE FROM orders WHERE id IN (SELECT id FROM to_del);
    """
    ok, out = _run_pg(sql)
    tag = '<good>' if ok else '<error>'
    _log('anomaly', tag, f'pg_bulk_delete: deleted ~{n} orders ok={ok} {out[:100] if not ok else ""}')

def pg_corrupt_orders(n: int = 50) -> None:
    """Flip integrity_hash on N random orders to simulate logic corruption."""
    _log('anomaly', '<warn>', f'pg_corrupt_orders: corrupting {n} order hashes')
    sql = f"""
    UPDATE orders
    SET integrity_hash = md5('ANOMALY|' || id::text || '|' || NOW()::text)
    WHERE id IN (SELECT id FROM orders ORDER BY RANDOM() LIMIT {n});
    """
    ok, out = _run_pg(sql)
    tag = '<good>' if ok else '<error>'
    _log('anomaly', tag, f'pg_corrupt_orders: ok={ok} {out[:100] if not ok else ""}')

def pg_spike_insert(n: int = 500) -> None:
    """Rapidly insert N extra orders (runaway process simulation)."""
    _log('anomaly', '<warn>', f'pg_spike_insert: inserting {n} orders rapidly')
    sql = f"""
    INSERT INTO orders (user_id, amount)
    SELECT
        (FLOOR(RANDOM() * (SELECT COUNT(*) FROM customers)) + 1)::INT,
        (RANDOM() * 5000000 + 50000)::NUMERIC(10,2)
    FROM generate_series(1, {n});
    """
    ok, out = _run_pg(sql)
    tag = '<good>' if ok else '<error>'
    _log('anomaly', tag, f'pg_spike_insert: ok={ok} {out[:100] if not ok else ""}')

def mongo_bulk_delete(pct: float = 0.40) -> None:
    """Delete ~pct of orders collection."""
    _log('anomaly', '<warn>', f'mongo_bulk_delete: targeting ~{int(pct*100)}% of orders')
    ok, out = _run_mongo('print(db.orders.countDocuments());')
    if not ok:
        _log('anomaly', '<error>', f'mongo count failed: {out}'); return
    try:
        total = int(out.strip())
    except Exception:
        total = 100
    n = max(1, int(total * pct))
    js = f"""
    const ids = db.orders.find({{}},'{{_id:1}}').limit({n}).toArray().map(o=>o._id);
    db.orders.deleteMany({{_id:{{$in:ids}}}});
    print('deleted=' + ids.length);
    """
    ok, out = _run_mongo(js)
    tag = '<good>' if ok else '<error>'
    _log('anomaly', tag, f'mongo_bulk_delete: ok={ok} {out[:100]}')

def mongo_corrupt_sigs(n: int = 50) -> None:
    """Reverse hash_signature on N random orders."""
    _log('anomaly', '<warn>', f'mongo_corrupt_sigs: corrupting {n} order signatures')
    js = f"""
    const docs = db.orders.find({{}}).limit({n}).toArray();
    let count = 0;
    for (const d of docs) {{
      const bad = (d.hash_signature || 'CORRUPT').split('').reverse().join('') + '_ANOM';
      db.orders.updateOne({{_id: d._id}}, {{$set: {{hash_signature: bad}}}});
      count++;
    }}
    print('corrupted=' + count);
    """
    ok, out = _run_mongo(js)
    tag = '<good>' if ok else '<error>'
    _log('anomaly', tag, f'mongo_corrupt_sigs: ok={ok} {out[:100]}')

def mongo_spike_insert(n: int = 500) -> None:
    """Bulk insert N documents into orders (runaway job simulation)."""
    _log('anomaly', '<warn>', f'mongo_spike_insert: inserting {n} orders')
    js = f"""
    const docs = [];
    for (let i = 0; i < {n}; i++) {{
      docs.push({{
        user_id: null,
        product_id: null,
        quantity: Math.floor(Math.random()*10)+1,
        total_price: Math.floor(Math.random()*5000000)+50000,
        status: 'anomaly_inject',
        created_at: new Date(),
        hash_signature: 'INJECTED_' + i
      }});
    }}
    db.orders.insertMany(docs);
    print('inserted={n}');
    """
    ok, out = _run_mongo(js)
    tag = '<good>' if ok else '<error>'
    _log('anomaly', tag, f'mongo_spike_insert: ok={ok} {out[:100]}')

def unstr_mass_delete(pct: float = 0.50) -> None:
    """Delete ~pct of all unstructured files (simulates accidental rm -rf)."""
    _log('anomaly', '<warn>', f'unstr_mass_delete: deleting ~{int(pct*100)}% of unstructured files')
    all_files = []
    for root, _, files in os.walk(UNSTR_DIR):
        for f in files:
            all_files.append(os.path.join(root, f))
    n = max(1, int(len(all_files) * pct))
    targets = random.sample(all_files, min(n, len(all_files)))
    deleted = bytes_freed = 0
    for path in targets:
        try:
            sz = os.path.getsize(path)
            os.remove(path)
            deleted += 1
            bytes_freed += sz
        except Exception:
            pass
    _log_unstr_event('anomaly_mass_delete', UNSTR_DIR, bytes_freed,
                     details=f'deleted={deleted} files pct={int(pct*100)}')
    _log('anomaly', '<good>', f'unstr_mass_delete: deleted={deleted} files freed={bytes_freed//1024}KB')

def unstr_overwrite(target_ext: str = '.bin') -> None:
    """Overwrite all blob/dat files with random bytes (simulates storage corruption)."""
    _log('anomaly', '<warn>', f'unstr_overwrite: overwriting all {target_ext} files with random data')
    targets = []
    for root, _, files in os.walk(UNSTR_DIR):
        for f in files:
            if f.endswith(target_ext):
                targets.append(os.path.join(root, f))
    corrupted = total_bytes = 0
    for path in targets:
        try:
            sz = os.path.getsize(path)
            with open(path, 'r+b') as f:
                chunk = min(sz, 1024 * 1024)
                f.seek(0)
                f.write(os.urandom(chunk))
            total_bytes += chunk
            corrupted += 1
        except Exception:
            pass
    _log_unstr_event('anomaly_overwrite', UNSTR_DIR, total_bytes,
                     details=f'corrupted={corrupted} files ext={target_ext}')
    _log('anomaly', '<good>', f'unstr_overwrite: corrupted={corrupted} files {target_ext}')

def unstr_flood_create(n: int = 200) -> None:
    """Create N small files rapidly (runaway log spam / disk flood)."""
    _log('anomaly', '<warn>', f'unstr_flood_create: creating {n} small flood files')
    flood_dir = os.path.join(UNSTR_DIR, 'logs')
    os.makedirs(flood_dir, exist_ok=True)
    created = total_bytes = 0
    for i in range(n):
        fname = f'flood_{rand_str(8)}_{i}.log'
        path  = os.path.join(flood_dir, fname)
        try:
            content = f'FLOOD {now_iso()} {rand_str(64)}\n' * random.randint(10, 50)
            with open(path, 'w') as f:
                f.write(content)
            total_bytes += len(content.encode())
            created += 1
        except Exception:
            pass
    _log_unstr_event('anomaly_flood_create', flood_dir, total_bytes,
                     details=f'created={created} files')
    _log('anomaly', '<good>', f'unstr_flood_create: created={created} files ({total_bytes//1024}KB)')


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

ANOMALY_MAP = {
    'pg_bulk_delete':    pg_bulk_delete,
    'pg_corrupt_orders': pg_corrupt_orders,
    'pg_spike_insert':   pg_spike_insert,
    'mongo_bulk_delete': mongo_bulk_delete,
    'mongo_corrupt_sigs':mongo_corrupt_sigs,
    'mongo_spike_insert':mongo_spike_insert,
    'unstr_mass_delete': unstr_mass_delete,
    'unstr_overwrite':   unstr_overwrite,
    'unstr_flood_create':unstr_flood_create,
}

def main() -> int:
    ap = argparse.ArgumentParser(
        description='Inject a sudden anomaly event into the live data systems.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='\n'.join(f'  {k}' for k in ['all'] + list(ANOMALY_MAP))
    )
    ap.add_argument('--type', required=True,
                    choices=list(ANOMALY_MAP) + ['all'],
                    help='Anomaly type to inject')
    ap.add_argument('--pct',  type=float, default=0.40,
                    help='Fraction of rows/files to affect for bulk_delete/overwrite (default: 0.40)')
    ap.add_argument('--n',    type=int,   default=None,
                    help='Override count for spike_insert / corrupt / flood_create')
    args = ap.parse_args()

    _log('anomaly', '<info>', f'=== ANOMALY INJECTION START  type={args.type}  {now_iso()} ===')

    types_to_run = list(ANOMALY_MAP) if args.type == 'all' else [args.type]
    for t in types_to_run:
        fn = ANOMALY_MAP[t]
        import inspect
        sig = inspect.signature(fn)
        kwargs = {}
        if 'pct' in sig.parameters and args.pct is not None:
            kwargs['pct'] = args.pct
        if 'n' in sig.parameters and args.n is not None:
            kwargs['n'] = args.n
        try:
            fn(**kwargs)
        except Exception as e:
            _log('anomaly', '<error>', f'{t} raised: {e}')
        if args.type == 'all':
            time.sleep(1)   # brief gap so each event is separately timestamped

    _log('anomaly', '<info>', f'=== ANOMALY INJECTION END    type={args.type}  {now_iso()} ===')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
