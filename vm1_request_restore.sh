#!/usr/bin/env bash
# =============================================================================
# vm1_request_restore.sh
# =============================================================================
# VM1-side helper to send a restore request to VM2 and wait for completion.
#
# Usage:
#   ./vm1_request_restore.sh [OPTIONS]
#
# Options:
#   --version   <chain-v>     Chain to restore (required). e.g. chain-v1
#   --chain     <N>           Number of cycles to restore (required). e.g. 3
#   --source    <mode>        local | cloud | immutable  (default: local)
#   --no-push                 Don't rsync back to VM1 (stage only on VM2)
#   --vm1-dest  <path>        Where VM2 rsync-pushes restored cycles on VM1
#                             (default: /home/primary/data/backup-incoming)
#
# Transport modes:
#   --local-req-dir <path>    Write request directly to a local path
#                             (use when this script runs ON VM2, or VM2 fs is mounted)
#   --vm2-host <user@host>    Write request via SSH + SCP to a remote VM2
#                             (use when this script runs ON VM1 and VM2 is remote)
#
# Examples:
#   # Running on VM2 (or with VM2 fs mounted):
#   ./vm1_request_restore.sh --version chain-v1 --chain 3
#
#   # Running on VM1, pushing request via SSH:
#   ./vm1_request_restore.sh --version chain-v1 --chain 3 \
#       --vm2-host recovery@192.168.1.20 \
#       --vm1-dest /home/primary/data/backup-incoming
# =============================================================================
set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
VERSION=""
CHAIN_N=""
SOURCE="local"
SEND_TO_VM1=true
VM1_DEST="primary@192.168.10.128:/home/primary/data/backup-incoming"

# VM2 restore-requests directory (local path or reached via SSH).
LOCAL_REQ_DIR="/home/recovery/local-backup/restore-requests"
VM2_HOST=""          # e.g. recovery@192.168.1.20  (leave empty if running locally)

POLL_INTERVAL=2      # seconds between .done/.error checks
TIMEOUT=300          # max seconds to wait for completion

# ── colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${CYAN}[$(date -u +%T)Z]${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC}  $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC}  $*"; }
die()  { echo -e "${RED}  ✗${NC}  $*" >&2; exit 1; }

# ── argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)       VERSION="$2";       shift 2;;
    --chain)         CHAIN_N="$2";       shift 2;;
    --source)        SOURCE="$2";        shift 2;;
    --no-push)       SEND_TO_VM1=false;  shift;;
    --vm1-dest)      VM1_DEST="$2";      shift 2;;
    --local-req-dir) LOCAL_REQ_DIR="$2"; shift 2;;
    --vm2-host)      VM2_HOST="$2";      shift 2;;
    --timeout)       TIMEOUT="$2";       shift 2;;
    -h|--help)
      sed -n '/^# Usage:/,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0;;
    *) die "Unknown option: $1";;
  esac
done

# ── validation ────────────────────────────────────────────────────────────────
[[ -z "$VERSION" ]] && die "--version is required (e.g. chain-v1)"
[[ -z "$CHAIN_N" ]] && die "--chain is required (number of cycles)"
[[ "$CHAIN_N" =~ ^[0-9]+$ ]] || die "--chain must be a positive integer"
[[ "$CHAIN_N" -ge 1 ]] || die "--chain must be >= 1"
[[ "$SOURCE" =~ ^(local|cloud|immutable)$ ]] || die "--source must be local, cloud, or immutable"

# ── helper: write a file (local or via SSH) ───────────────────────────────────
remote_write() {
  local content="$1"
  local remote_path="$2"
  if [[ -n "$VM2_HOST" ]]; then
    echo "$content" | ssh "$VM2_HOST" "cat > '$remote_path'"
  else
    echo "$content" > "$remote_path"
  fi
}

remote_mv() {
  local src="$1"; local dst="$2"
  if [[ -n "$VM2_HOST" ]]; then
    ssh "$VM2_HOST" "mv '$src' '$dst'"
  else
    mv "$src" "$dst"
  fi
}

remote_exists() {
  local path="$1"
  if [[ -n "$VM2_HOST" ]]; then
    ssh "$VM2_HOST" "test -f '$path'" 2>/dev/null
  else
    test -f "$path"
  fi
}

remote_cat() {
  local path="$1"
  if [[ -n "$VM2_HOST" ]]; then
    ssh "$VM2_HOST" "cat '$path'" 2>/dev/null || true
  else
    cat "$path" 2>/dev/null || true
  fi
}

# ── print plan ────────────────────────────────────────────────────────────────
echo -e "${BOLD}"
echo "  ┌─────────────────────────────────────────────┐"
echo "  │         VM2 Restore Request                 │"
echo "  └─────────────────────────────────────────────┘"
echo -e "${NC}"
log "version   : $VERSION"
log "chain     : $CHAIN_N  (oldest $CHAIN_N cycle(s))"
log "source    : $SOURCE"
log "send_vm1  : $SEND_TO_VM1"
[[ "$SEND_TO_VM1" == "true" ]] && log "vm1_dest  : $VM1_DEST"
[[ -n "$VM2_HOST" ]] && log "vm2_host  : $VM2_HOST" || log "mode      : local (direct filesystem write)"

# ── generate request ID ───────────────────────────────────────────────────────
REQUEST_ID="restore-${VERSION}-$(date +%s%3N)"
REQ_TMP="$LOCAL_REQ_DIR/$REQUEST_ID.json.tmp"
REQ_FILE="$LOCAL_REQ_DIR/$REQUEST_ID.json"
DONE_FILE="$LOCAL_REQ_DIR/$REQUEST_ID.done"
ERR_FILE="$LOCAL_REQ_DIR/$REQUEST_ID.error"
INPROG_FILE="$LOCAL_REQ_DIR/$REQUEST_ID.inprogress"

# ── build JSON payload ────────────────────────────────────────────────────────
SEND_BOOL="true"
[[ "$SEND_TO_VM1" == "false" ]] && SEND_BOOL="false"

JSON=$(cat << ENDJSON
{
  "version":       "$VERSION",
  "chain":         $CHAIN_N,
  "source":        "$SOURCE",
  "send_to_vm1":   $SEND_BOOL,
  "vm1_dest_root": "$VM1_DEST"
}
ENDJSON
)

# ── write request (atomic: write tmp → rename) ────────────────────────────────
log "Writing restore request …"
remote_write "$JSON" "$REQ_TMP"
remote_mv    "$REQ_TMP" "$REQ_FILE"
ok "Request dropped: $REQUEST_ID.json"
echo "$JSON" | sed 's/^/         /'

# ── wait for VM2 to process the request ──────────────────────────────────────
echo ""
log "Waiting for VM2 to process (timeout: ${TIMEOUT}s) …"

START_TIME=$SECONDS
RESULT="timeout"

while true; do
  ELAPSED=$(( SECONDS - START_TIME ))

  if remote_exists "$DONE_FILE"; then
    RESULT="done"
    break
  fi

  if remote_exists "$ERR_FILE"; then
    RESULT="error"
    break
  fi

  if [[ $ELAPSED -ge $TIMEOUT ]]; then
    RESULT="timeout"
    break
  fi

  # Show a spinner so the operator knows we're waiting.
  printf "\r  ·  elapsed: %ds …  " "$ELAPSED"
  sleep "$POLL_INTERVAL"
done
echo ""   # clear spinner line

ELAPSED=$(( SECONDS - START_TIME ))

# ── handle result ─────────────────────────────────────────────────────────────
case "$RESULT" in

  done)
    ok "Restore SUCCEEDED in ${ELAPSED}s"
    echo ""
    if [[ "$SEND_TO_VM1" == "true" ]]; then
      ok "Cycles were rsync-pushed by VM2 to:"
      ok "  $VM1_DEST/$VERSION/<cycle_id>/"
      echo ""
      log "Replay cycles in order (oldest first):"
      log "  ls $VM1_DEST/$VERSION/"
    else
      ok "Cycles staged on VM2 at:"
      ok "  /home/recovery/local-backup/outgoing/primary/$VERSION/"
      warn "send_to_vm1 was false — pull manually from VM2 if needed"
    fi
    ;;

  error)
    ERR_MSG="$(remote_cat "$ERR_FILE")"
    die "Restore FAILED (${ELAPSED}s elapsed). Reason: ${ERR_MSG:-unknown}"
    ;;

  timeout)
    echo ""
    warn "Timed out after ${TIMEOUT}s"
    warn "Request may still be processing on VM2."
    warn "Check on VM2: ls $LOCAL_REQ_DIR/"
    warn "To retry:     mv $INPROG_FILE $REQ_FILE"
    exit 1
    ;;

esac

# ── print next steps ──────────────────────────────────────────────────────────
echo ""
log "Done. Restored $CHAIN_N cycle(s) from $VERSION."
echo ""
