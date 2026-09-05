#!/usr/bin/env python3
"""
Unstructured Data Generator — Rich event variety for DR simulation

File mix (created on first run):
  logs/       app_XX.log      — high-churn application logs
  data/       sensor_XX.dat   — sensor data files (binary-ish, periodic overwrite)
  blobs/      asset_XX.bin    — large binary blobs (simulates media/artifact files)
  config/     svc_XX.conf     — small text config files (frequent edits)
  reports/    report_XX.csv   — CSV report files (append-only growth)

Events per loop (every 2 minutes):
  append          — add log lines to log files
  in_place_edit   — overwrite a region of a data/blob file
  truncate        — reset a log file to empty (simulates log rotation)
  create          — add a brand-new file (organic file set growth)
  delete          — permanently remove a file (simulates cleanup)
  rename          — rename a file (simulates archiving)
  config_update   — rewrite an entire config file with new values
  report_append   — add rows to a CSV report file

All events are recorded to /monitor/unstructured_growth.csv (picked up by telemetry).
Chaos bitflip remains available via CHAOS_BITFLIP_RATE env variable.
"""

import os
import random
import string
import time
from datetime import datetime, timezone, timedelta

# ── Configuration ────────────────────────────────────────────────────────────

TARGET_DIR  = '/data/unstructured'
MONITOR_DIR = '/monitor'
CSV_PATH    = os.path.join(MONITOR_DIR, 'unstructured_growth.csv')

MAX_TOTAL_BYTES = 2 * 1024**3   # 2 GB hard cap
SAFETY_MARGIN   = 150 * 1024 * 1024  # 150 MB headroom

JAKARTA_TZ = timezone(timedelta(hours=7))

# Sub-directory structure
DIRS = {
    'logs':    ('app',    '.log',  int(os.environ.get('LOG_COUNT',    '12'))),
    'data':    ('sensor', '.dat',  int(os.environ.get('DAT_COUNT',    '6'))),
    'blobs':   ('asset',  '.bin',  int(os.environ.get('BLOB_COUNT',   '4'))),
    'config':  ('svc',    '.conf', int(os.environ.get('CONF_COUNT',   '5'))),
    'reports': ('report', '.csv',  int(os.environ.get('REP_COUNT',    '4'))),
}

# Initial file sizes
INIT_SIZES = {
    '.log':  int(os.environ.get('LOG_SIZE_BYTES',  str(30 * 1024 * 1024))),   # 30 MB
    '.dat':  int(os.environ.get('DAT_SIZE_BYTES',  str(50 * 1024 * 1024))),   # 50 MB
    '.bin':  int(os.environ.get('BLOB_SIZE_BYTES', str(80 * 1024 * 1024))),   # 80 MB
    '.conf': int(os.environ.get('CONF_SIZE_BYTES', str(64 * 1024))),          # 64 KB
    '.csv':  int(os.environ.get('REP_SIZE_BYTES',  str(5 * 1024 * 1024))),    # 5 MB
}

# Per-loop activity knobs
APPEND_BYTES          = int(os.environ.get('APPEND_BYTES',       str(80 * 1024)))   # 80 KB
INPLACE_EDIT_BYTES    = int(os.environ.get('INPLACE_EDIT_BYTES', str(32 * 1024)))   # 32 KB
LOGS_PER_LOOP         = int(os.environ.get('LOGS_PER_LOOP',      '4'))
DAT_EDITS_PER_LOOP    = int(os.environ.get('DAT_EDITS_PER_LOOP', '2'))
BLOB_EDITS_PER_LOOP   = int(os.environ.get('BLOB_EDITS_PER_LOOP','1'))
CONF_UPDATES_PER_LOOP = int(os.environ.get('CONF_UPDATES_PER_LOOP','1'))
REP_APPENDS_PER_LOOP  = int(os.environ.get('REP_APPENDS_PER_LOOP','2'))

# Creation/deletion/rename probabilities per loop
P_CREATE  = float(os.environ.get('P_CREATE',  '0.15'))
P_DELETE  = float(os.environ.get('P_DELETE',  '0.10'))
P_RENAME  = float(os.environ.get('P_RENAME',  '0.10'))
P_TRUNCATE= float(os.environ.get('P_TRUNCATE','0.05'))

# Chaos
CHAOS_BITFLIP_RATE       = float(os.environ.get('CHAOS_BITFLIP_RATE',       '0.0'))
CHAOS_BITFLIP_MIN_OFFSET = int(os.environ.get('CHAOS_BITFLIP_MIN_OFFSET',   '4096'))

LOOP_SLEEP = int(os.environ.get('LOOP_SLEEP', '120'))  # seconds


# ── Helpers ──────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(JAKARTA_TZ).replace(microsecond=0).isoformat()

def rand_str(n: int = 8) -> str:
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def rand_payload(n: int) -> bytes:
    return (''.join(random.choices(string.printable[:72], k=n))).encode('utf-8')[:n]

def dir_stats(path: str) -> tuple[int, int]:
    total_bytes = total_files = 0
    for root, _, files in os.walk(path):
        for f in files:
            total_files += 1
            try:
                total_bytes += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total_bytes, total_files

def _safe_bytes() -> int:
    return dir_stats(TARGET_DIR)[0]

def ensure_csv():
    os.makedirs(MONITOR_DIR, exist_ok=True)
    header = 'timestamp,target,action,status,target_file,bytes_changed,total_bytes,total_files,details\n'
    if os.path.exists(CSV_PATH):
        try:
            with open(CSV_PATH, 'r') as f:
                if f.readline().strip() == header.strip():
                    return
        except Exception:
            pass
        bak = CSV_PATH + '.bak.' + datetime.now(JAKARTA_TZ).strftime('%Y%m%d_%H%M%S')
        try:
            os.rename(CSV_PATH, bak)
        except Exception:
            pass
    with open(CSV_PATH, 'w') as f:
        f.write(header)

def log_event(action: str, target_file: str, bytes_changed: int,
              *, status: str = 'ok', details: str = '') -> None:
    ts = now_iso()
    total_bytes, total_files = dir_stats(TARGET_DIR)
    safe_details = (details or '').replace('\n', ' ').replace(',', ';')[:200]
    row = f'{ts},unstructured,{action},{status},{target_file},{bytes_changed},{total_bytes},{total_files},{safe_details}\n'
    try:
        with open(CSV_PATH, 'a') as f:
            f.write(row)
    except Exception:
        pass


# ── File factory ─────────────────────────────────────────────────────────────

def _write_log_file(path: str, size: int) -> None:
    with open(path, 'w') as f:
        written = 0
        while written < size:
            line = f'{now_iso()} INFO {random.choice(["worker","api","db","cache","scheduler"])} ' \
                   f'req_id={rand_str()} latency_ms={random.randint(1,500)} ' \
                   f'status={random.choice(["200","404","500","201","304"])} ' \
                   f'msg="{random.choice(["ok","timeout","retry","hit","miss"])}"\n'
            f.write(line)
            written += len(line.encode())

def _write_dat_file(path: str, size: int) -> None:
    block = f'SENSOR-{rand_str(16)}-\n'.encode()
    with open(path, 'wb') as f:
        written = 0
        while written < size:
            chunk = block * (min(4096, size - written) // len(block) + 1)
            to_write = min(size - written, len(chunk))
            f.write(chunk[:to_write])
            written += to_write

def _write_bin_file(path: str, size: int) -> None:
    with open(path, 'wb') as f:
        written = 0
        while written < size:
            chunk_size = min(1024 * 1024, size - written)
            f.write(os.urandom(chunk_size))
            written += chunk_size

def _write_conf_file(path: str) -> None:
    lines = [
        f'# Generated {now_iso()}\n',
        f'service_name = svc_{rand_str(6)}\n',
        f'listen_port  = {random.randint(3000, 9999)}\n',
        f'log_level    = {random.choice(["debug","info","warn","error"])}\n',
        f'max_conn     = {random.randint(10, 500)}\n',
        f'timeout_ms   = {random.randint(100, 5000)}\n',
        f'secret_key   = {rand_str(32)}\n',
        f'region       = {random.choice(["ap-southeast-1","ap-southeast-3","us-east-1"])}\n',
        f'enable_tls   = {random.choice(["true","false"])}\n',
        f'replica_count= {random.randint(1, 5)}\n',
    ]
    with open(path, 'w') as f:
        f.writelines(lines)

def _write_csv_file(path: str, size: int) -> None:
    with open(path, 'w') as f:
        f.write('ts,metric,value,unit,host\n')
        written = 5
        while written < size:
            line = f'{now_iso()},{random.choice(["cpu","mem","disk_io","net_rx","net_tx"])},' \
                   f'{random.uniform(0,100):.2f},{random.choice(["pct","mb","mbps"])},{rand_str(8)}\n'
            f.write(line)
            written += len(line.encode())

def _make_file(path: str, ext: str) -> None:
    sz = INIT_SIZES.get(ext, 64 * 1024)
    if ext == '.log':  _write_log_file(path, sz)
    elif ext == '.dat': _write_dat_file(path, sz)
    elif ext == '.bin': _write_bin_file(path, sz)
    elif ext == '.conf': _write_conf_file(path)
    elif ext == '.csv': _write_csv_file(path, sz)
    else:
        with open(path, 'wb') as f:
            f.write(os.urandom(sz))


# ── Initialization ────────────────────────────────────────────────────────────

def initialize_base_state() -> None:
    print(f'[{now_iso()}] Initializing base file state in {TARGET_DIR}')
    for subdir, (prefix, ext, count) in DIRS.items():
        d = os.path.join(TARGET_DIR, subdir)
        os.makedirs(d, exist_ok=True)
        existing = set(os.listdir(d))
        for i in range(1, count + 1):
            fname = f'{prefix}_{i:02d}{ext}'
            if fname not in existing:
                _make_file(os.path.join(d, fname), ext)
                print(f'[{now_iso()}]   created {subdir}/{fname}')
    log_event('init', TARGET_DIR, 0, details='base_state_initialized')
    print(f'[{now_iso()}] Base state ready')


# ── Event actions ─────────────────────────────────────────────────────────────

def do_append(subdir: str = 'logs') -> None:
    d = os.path.join(TARGET_DIR, subdir)
    files = [f for f in os.listdir(d) if f.endswith('.log')]
    if not files:
        return
    if _safe_bytes() + APPEND_BYTES + SAFETY_MARGIN > MAX_TOTAL_BYTES:
        log_event('append', subdir, 0, status='skip', details='space_cap')
        return
    targets = random.sample(files, min(LOGS_PER_LOOP, len(files)))
    for fname in targets:
        path = os.path.join(d, fname)
        try:
            with open(path, 'a') as f:
                written = 0
                while written < APPEND_BYTES:
                    line = f'{now_iso()} {random.choice(["DEBUG","INFO","WARN","ERROR"])} ' \
                           f'{rand_str()} msg="{rand_str(12)}" trace={rand_str(16)}\n'
                    f.write(line)
                    written += len(line.encode())
            log_event('append', f'{subdir}/{fname}', written)
        except Exception as e:
            log_event('append', f'{subdir}/{fname}', 0, status='fail', details=str(e))


def do_inplace_edit(subdir: str, ext: str) -> None:
    d = os.path.join(TARGET_DIR, subdir)
    files = [f for f in os.listdir(d) if f.endswith(ext)]
    if not files:
        return
    fname = random.choice(files)
    path  = os.path.join(d, fname)
    try:
        size = os.path.getsize(path)
        if size < INPLACE_EDIT_BYTES + 64:
            offset = 0
        else:
            offset = random.randint(size // 4, (3 * size) // 4)
        token   = f'---EDITED_{now_iso()}---'
        payload = (token * (INPLACE_EDIT_BYTES // len(token) + 1)).encode()[:INPLACE_EDIT_BYTES]
        with open(path, 'r+b') as f:
            f.seek(offset)
            f.write(payload)
        log_event('in_place_edit', f'{subdir}/{fname}', len(payload))
    except Exception as e:
        log_event('in_place_edit', f'{subdir}/{fname}', 0, status='fail', details=str(e))


def do_config_update() -> None:
    d = os.path.join(TARGET_DIR, 'config')
    files = [f for f in os.listdir(d) if f.endswith('.conf')]
    if not files:
        return
    for _ in range(min(CONF_UPDATES_PER_LOOP, len(files))):
        fname = random.choice(files)
        path  = os.path.join(d, fname)
        try:
            old_size = os.path.getsize(path)
            _write_conf_file(path)
            new_size = os.path.getsize(path)
            log_event('config_update', f'config/{fname}', abs(new_size - old_size))
        except Exception as e:
            log_event('config_update', f'config/{fname}', 0, status='fail', details=str(e))


def do_report_append() -> None:
    d = os.path.join(TARGET_DIR, 'reports')
    files = [f for f in os.listdir(d) if f.endswith('.csv')]
    if not files:
        return
    targets = random.sample(files, min(REP_APPENDS_PER_LOOP, len(files)))
    for fname in targets:
        path = os.path.join(d, fname)
        try:
            rows = random.randint(50, 200)
            written = 0
            with open(path, 'a') as f:
                for _ in range(rows):
                    line = f'{now_iso()},{random.choice(["cpu","mem","disk_io","net_rx","net_tx"])},' \
                           f'{random.uniform(0,100):.2f},{random.choice(["pct","mb","mbps"])},{rand_str(8)}\n'
                    f.write(line)
                    written += len(line.encode())
            log_event('report_append', f'reports/{fname}', written, details=f'rows={rows}')
        except Exception as e:
            log_event('report_append', f'reports/{fname}', 0, status='fail', details=str(e))


def do_truncate() -> None:
    """Simulate log truncation / log rotation by zeroing a log file."""
    d = os.path.join(TARGET_DIR, 'logs')
    files = [f for f in os.listdir(d) if f.endswith('.log')]
    if not files:
        return
    fname = random.choice(files)
    path  = os.path.join(d, fname)
    try:
        old_size = os.path.getsize(path)
        with open(path, 'w') as f:
            f.write(f'# rotated {now_iso()}\n')
        log_event('truncate', f'logs/{fname}', old_size, details='log_rotation')
    except Exception as e:
        log_event('truncate', f'logs/{fname}', 0, status='fail', details=str(e))


def do_create() -> None:
    """Organically add a new file to a random sub-directory."""
    if _safe_bytes() + SAFETY_MARGIN > MAX_TOTAL_BYTES:
        return
    subdir, (prefix, ext, _) = random.choice(list(DIRS.items()))
    d = os.path.join(TARGET_DIR, subdir)
    fname = f'{prefix}_new_{rand_str(6)}{ext}'
    path  = os.path.join(d, fname)
    try:
        _make_file(path, ext)
        size = os.path.getsize(path)
        log_event('create', f'{subdir}/{fname}', size, details='new_file')
    except Exception as e:
        log_event('create', f'{subdir}/{fname}', 0, status='fail', details=str(e))


def do_delete() -> None:
    """Remove a randomly selected non-base file (created files or old rotations)."""
    candidates = []
    for subdir in DIRS:
        d = os.path.join(TARGET_DIR, subdir)
        for fname in os.listdir(d):
            # Only delete dynamically-created or rotated files, not base files
            if '_new_' in fname or '.old.' in fname:
                candidates.append((subdir, fname))
    if not candidates:
        return
    subdir, fname = random.choice(candidates)
    path = os.path.join(TARGET_DIR, subdir, fname)
    try:
        size = os.path.getsize(path)
        os.remove(path)
        log_event('delete', f'{subdir}/{fname}', size, details='file_removed')
    except Exception as e:
        log_event('delete', f'{subdir}/{fname}', 0, status='fail', details=str(e))


def do_rename() -> None:
    """Archive a log file by renaming it with a timestamp suffix."""
    d = os.path.join(TARGET_DIR, 'logs')
    files = [f for f in os.listdir(d) if f.endswith('.log') and '_new_' not in f]
    if not files:
        return
    fname = random.choice(files)
    src   = os.path.join(d, fname)
    ts    = datetime.now(JAKARTA_TZ).strftime('%Y%m%d_%H%M%S')
    dst   = os.path.join(d, f'{fname}.old.{ts}')
    try:
        size = os.path.getsize(src)
        os.rename(src, dst)
        # recreate empty file so appends still work
        with open(src, 'w') as f:
            f.write(f'# rotated to {os.path.basename(dst)} at {now_iso()}\n')
        # retention: keep max 3 old files per base name
        base_prefix = fname + '.old.'
        old_files   = sorted([p for p in os.listdir(d) if p.startswith(base_prefix)], reverse=True)
        for extra in old_files[3:]:
            try:
                os.remove(os.path.join(d, extra))
            except Exception:
                pass
        log_event('rename', f'logs/{fname}', size, details=f'archived_to={os.path.basename(dst)}')
    except Exception as e:
        log_event('rename', f'logs/{fname}', 0, status='fail', details=str(e))


def do_bitflip() -> None:
    """Optional chaos: silent single-byte corruption."""
    candidates = []
    for root, _, files in os.walk(TARGET_DIR):
        for f in files:
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
                if sz >= CHAOS_BITFLIP_MIN_OFFSET * 2 + 1:
                    candidates.append((p, sz))
            except OSError:
                pass
    if not candidates:
        return
    path, size = random.choice(candidates)
    min_off = min(CHAOS_BITFLIP_MIN_OFFSET, size - 2)
    max_off = max(min_off, size - min_off - 1)
    if max_off <= min_off:
        return
    offset = random.randint(min_off, max_off)
    try:
        with open(path, 'r+b') as f:
            f.seek(offset)
            b = f.read(1)
            if b:
                f.seek(offset)
                f.write(bytes([b[0] ^ 0x01]))
        rel = os.path.relpath(path, TARGET_DIR)
        log_event('bitflip', rel, 1, status='ok', details=f'offset={offset}')
    except Exception as e:
        log_event('bitflip', os.path.relpath(path, TARGET_DIR), 0, status='fail', details=str(e))


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    os.makedirs(TARGET_DIR, exist_ok=True)
    ensure_csv()
    initialize_base_state()

    print(f'[{now_iso()}] Entering main loop (sleep={LOOP_SLEEP}s)')
    loop = 0
    while True:
        loop += 1

        # ── Always-on steady events ──────────────────────────────────────
        do_append('logs')
        for _ in range(DAT_EDITS_PER_LOOP):
            do_inplace_edit('data', '.dat')
        for _ in range(BLOB_EDITS_PER_LOOP):
            do_inplace_edit('blobs', '.bin')
        do_config_update()
        do_report_append()

        # ── Probabilistic events ─────────────────────────────────────────
        if random.random() < P_TRUNCATE:
            do_truncate()
        if random.random() < P_CREATE:
            do_create()
        if random.random() < P_RENAME:
            do_rename()
        if random.random() < P_DELETE:
            do_delete()

        # ── Chaos ────────────────────────────────────────────────────────
        if CHAOS_BITFLIP_RATE > 0 and random.random() < CHAOS_BITFLIP_RATE:
            do_bitflip()

        # ── Space guard ──────────────────────────────────────────────────
        total = _safe_bytes()
        if total + SAFETY_MARGIN >= MAX_TOTAL_BYTES:
            log_event('space_limit', '', 0, status='skip', details='space_cap')

        time.sleep(LOOP_SLEEP)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print(f'[{now_iso()}] Stopped')
