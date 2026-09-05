#!/usr/bin/env bash
set -euo pipefail

# VM1-only incremental restore test for cycle artifacts.
#
# Default behavior:
# - Uses the latest 2 cycles under /home/primary/data/backup-cycles (apply older -> newer)
# - Verifies the newest cycle (manifest + checksums + optional raw verify)
# - Restores into /home/primary/data/restore/<newest_cycle>/ (unstructured/ + pg/ + mongo/)
# - Compares restored unstructured against live unstructured for integrity compliance
#
# Usage examples:
#   bash utilities/backup/tools/test_restore_unstructured_pfc.sh
#   bash utilities/backup/tools/test_restore_unstructured_pfc.sh --verify-raw
#   bash utilities/backup/tools/test_restore_unstructured_pfc.sh --cycles 20260526_002633 20260526_002712
#   bash utilities/backup/tools/test_restore_unstructured_pfc.sh --base /home/primary/data/unstructured

log() {
  echo "[TEST] $*"
}

CYCLES_ROOT="/home/primary/data/backup-cycles"
BASE_SNAPSHOT="/home/primary/data/unstructured"
VERIFY_RAW=0
CHUNK_SIZE="1048576"
OUT_DIR=""
CYCLES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cycles)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        CYCLES+=("$1")
        shift
      done
      ;;
    --base)
      BASE_SNAPSHOT="$2"
      shift 2
      ;;
    --out)
      OUT_DIR="$2"
      shift 2
      ;;
    --chunk-size)
      CHUNK_SIZE="$2"
      shift 2
      ;;
    --verify-raw)
      VERIFY_RAW=1
      shift
      ;;
    -h|--help)
      sed -n '1,120p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -d "$CYCLES_ROOT" ]]; then
  echo "[TEST] Missing cycles root: $CYCLES_ROOT" >&2
  exit 2
fi

if [[ ! -d "$BASE_SNAPSHOT" ]]; then
  echo "[TEST] Missing base dir: $BASE_SNAPSHOT" >&2
  exit 2
fi

if [[ ${#CYCLES[@]} -eq 0 ]]; then
  mapfile -t LAST_TWO < <(ls -1 "$CYCLES_ROOT" | sort | tail -n 2)
  if [[ ${#LAST_TWO[@]} -lt 1 ]]; then
    echo "[TEST] No cycles found under $CYCLES_ROOT" >&2
    exit 2
  fi
  CYCLES=("${LAST_TWO[@]}")
fi

# Convert cycle IDs into absolute paths
CYCLE_DIRS=()
for cid in "${CYCLES[@]}"; do
  d="$CYCLES_ROOT/$cid"
  if [[ ! -d "$d" ]]; then
    echo "[TEST] Not a cycle dir: $d" >&2
    exit 2
  fi
  CYCLE_DIRS+=("$d")
done

NEWEST_CYCLE="${CYCLES[-1]}"
if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="/home/primary/data/restore/$NEWEST_CYCLE"
fi

log "Base snapshot:  $BASE_SNAPSHOT"
log "Cycles:         ${CYCLES[*]}"
log "Output dir:     $OUT_DIR"
log "Chunk size:     $CHUNK_SIZE"

# 1) Verify newest cycle
log "Verifying newest cycle: $NEWEST_CYCLE"
VERIFY_ARGS=()
if [[ "$VERIFY_RAW" -eq 1 ]]; then
  VERIFY_ARGS+=("--verify-raw")
fi
python3 -u /home/primary/utilities/backup/tools/verify_cycle.py "${VERIFY_ARGS[@]}" "$CYCLES_ROOT/$NEWEST_CYCLE"

# 2) Restore
log "Restoring (apply cycles in order)"
RESTORE_ARGS=()
if [[ "$VERIFY_RAW" -eq 1 ]]; then
  RESTORE_ARGS+=("--verify-raw")
fi
python3 -u /home/primary/utilities/backup/tools/restore_cycle.py \
  --base-unstructured "$BASE_SNAPSHOT" \
  --out-root "$OUT_DIR" \
  --chunk-size "$CHUNK_SIZE" \
  "${RESTORE_ARGS[@]}" \
  "${CYCLE_DIRS[@]}"

# 3) Compare restored unstructured vs base dir for integrity compliance
log "Comparing restored unstructured vs base (SHA256 tree compare)"
python3 -u /home/primary/utilities/backup/tools/compare_trees.py \
  --left "$BASE_SNAPSHOT" \
  --right "$OUT_DIR/unstructured"

log "Restore complete"
log "Tip: to validate historical point-in-time, you need a base snapshot at that point; this test validates artifact integrity + idempotent restore." 
