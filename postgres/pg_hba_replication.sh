#!/usr/bin/env sh
set -eu

# Allows pg_basebackup/replication connections from inside the container.
# This runs only on fresh init (docker-entrypoint-initdb.d).

if [ -z "${PGDATA:-}" ]; then
  echo "[PG][INIT] PGDATA not set; cannot update pg_hba.conf" >&2
  exit 0
fi

HBA="$PGDATA/pg_hba.conf"

# Only append once
if grep -q "# drsim:replication" "$HBA" 2>/dev/null; then
  echo "[PG][INIT] pg_hba.conf already contains drsim replication rules"
  exit 0
fi

{
  echo ""
  echo "# drsim:replication"
  echo "# Allow replication connections for pg_basebackup (local container only)"
  echo "host replication all 127.0.0.1/32 md5"
  echo "host replication all ::1/128 md5"
} >> "$HBA"

echo "[PG][INIT] Appended replication rules to pg_hba.conf"
