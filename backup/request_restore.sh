#!/usr/bin/env bash
# =============================================================================
# request_restore.sh — VM1-side restore request helper
#
# Workflow:
#   1. Writes a restore request JSON to VM2's restore-requests/ dir (via SSH)
#   2. Polls VM2 for .done / .error marker
#   3. If successful and send_to_vm1=true, cycles will already be in
#      /home/primary/data/backup-incoming/chain-vN/<cycle_id>/
#   4. Bumps the chain version in the local state DB so the next backup cycle
#      writes to a fresh chain-v(N+1)/ directory
#   5. Prints the exact restore_cycle.py command to run next
#
# Usage:
#   sudo bash utilities/backup/request_restore.sh [OPTIONS]
#
# Options:
#   --version  CHAIN    Which chain to restore (default: chain-v1)
#   --chain    N        How many cycles to restore oldest-first (default: all)
#   --source   SRC      Where VM2 reads from: local|general|immutable (default: local)
#   --no-send           Do NOT have VM2 push back to VM1 (inspect mode)
#   --no-bump           Skip chain version bump after restore
#   --dry-run           Print the request JSON but do not send it
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# ★ CONFIGURE — edit these if your paths/IPs change
# ---------------------------------------------------------------------------
VM2_USER="recovery"
VM2_HOST="192.168.10.129"
VM2_SSH_KEY="/home/primary/.ssh/recovery_rsync_ed25519"
VM2_REQUEST_DIR="/home/recovery/local-backup/restore-requests"

CLOUD_GENERAL_BUCKET_URL=""
CLOUD_IMMUTABLE_BUCKET_URL=""


VM1_BACKUP_INCOMING="/home/primary/data/backup-incoming"
VM1_IP="192.168.10.128"
VM1_USER="primary"
VM1_REMOTE_DEST="${VM1_USER}@${VM1_IP}:${VM1_BACKUP_INCOMING}"

VM1_STATE_DB="/home/primary/data/backup-metadata/pfc_index.db"
VM1_RESTORE_SCRIPT="/home/primary/utilities/backup/tools/restore_cycle.py"
BACKUP_ENV="/home/primary/utilities/backup/backup.env"

POLL_INTERVAL=3       # seconds between status checks
POLL_TIMEOUT=600      # max seconds to wait for VM2 (10 min)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
OPT_VERSION="chain-v1"
OPT_CHAIN="999"        # 999 = all available cycles
OPT_SOURCE="local"
OPT_SEND_TO_VM1="true"
OPT_BUMP="1"
OPT_DRY_RUN="0"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)  OPT_VERSION="$2";   shift 2 ;;
        --chain)    OPT_CHAIN="$2";     shift 2 ;;
        --source)   OPT_SOURCE="$2";    shift 2 ;;
        --no-send)  OPT_SEND_TO_VM1="false"; shift ;;
        --no-bump)  OPT_BUMP="0";       shift ;;
        --dry-run)  OPT_DRY_RUN="1";   shift ;;
        -h|--help)
            sed -n '/^# Usage:/,/^# =/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "<error> Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
LOG_FILE="/home/primary/utilities/backup/backup.log"

_ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

_log_file() {
    echo "[$(_ts)] $*" >> "$LOG_FILE"
}

_c() {
    local text="$1"
    local color="$2"
    if [[ -t 1 && "${LOG_COLOR:-1}" != "0" && -z "${NO_COLOR:-}" ]]; then
        echo -e "\e[${color}m${text}\e[0m"
    else
        echo "$text"
    fi
}

_good() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %-8s %s" "$scope" "$(_c "<good>" "32")" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<good>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_info() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %-8s %s" "$scope" "$(_c "<info>" "36")" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<info>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_warn() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %-8s %s" "$scope" "$(_c "<warn>" "33")" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<warn>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_bad() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %-8s %s" "$scope" "$(_c "<error>" "31")" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<error>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_log() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %-8s %s" "$scope" "<log>" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<log>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_ssh() {
    ssh -i "${VM2_SSH_KEY}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile=/home/primary/.ssh/known_hosts \
        "${VM2_USER}@${VM2_HOST}" "$@"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_log "VM1 → VM2 Restore Request"
_log "  version : ${OPT_VERSION}"
_log "  chain   : ${OPT_CHAIN}"
_log "  source  : ${OPT_SOURCE}"
_log "  push→vm1: ${OPT_SEND_TO_VM1}"
_log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Step 0 – Verify SSH to VM2 is working
_info "Checking SSH to VM2 (${VM2_USER}@${VM2_HOST}) …"
if ! _ssh "echo SSH_OK" &>/dev/null; then
    _bad "Cannot reach VM2 via SSH. Check key and network."
    exit 1
fi
_good "SSH OK"

# Step 1 – Build request JSON
REQUEST_ID="restore-$(date +%s)-$(openssl rand -hex 3)"

BUCKET_URL=""
if [[ "${OPT_SOURCE}" == "general" ]]; then
    BUCKET_URL="${CLOUD_GENERAL_BUCKET_URL}"
elif [[ "${OPT_SOURCE}" == "immutable" ]]; then
    BUCKET_URL="${CLOUD_IMMUTABLE_BUCKET_URL}"
fi

REQUEST_JSON=$(cat <<EOF
{
  "version":       "${OPT_VERSION}",
  "chain":         ${OPT_CHAIN},
  "source":        "${OPT_SOURCE}",
  "bucket_url":    "${BUCKET_URL}",
  "send_to_vm1":   ${OPT_SEND_TO_VM1},
  "vm1_dest_root": "${VM1_REMOTE_DEST}",
  "restore-date-time": "$(date '+%Y-%m-%dT%H:%M:%S%z')"
}
EOF
)

echo ""
_info "Request ID: ${REQUEST_ID}"
_info "Request JSON:"
echo "${REQUEST_JSON}" | sed 's/^/  /'
echo ""

if [[ "${OPT_DRY_RUN}" == "1" ]]; then
    _info "DRY RUN — request not sent."
    exit 0
fi

# Step 2 – Send request to VM2 atomically (write .tmp, then rename)
TMP_NAME="${REQUEST_ID}.json.tmp"
FINAL_NAME="${REQUEST_ID}.json"

_info "Sending request to VM2 …"
echo "${REQUEST_JSON}" | _ssh \
    "cat > '${VM2_REQUEST_DIR}/${TMP_NAME}' && \
     mv '${VM2_REQUEST_DIR}/${TMP_NAME}' '${VM2_REQUEST_DIR}/${FINAL_NAME}'"

_good "Request dropped: ${VM2_REQUEST_DIR}/${FINAL_NAME}"

# Step 3 – Poll for .done / .error
_info "Polling VM2 for completion (timeout=${POLL_TIMEOUT}s, interval=${POLL_INTERVAL}s) …"
elapsed=0
status="pending"

while (( elapsed < POLL_TIMEOUT )); do
    # Read status directory listing once per poll
    markers=$(_ssh "ls '${VM2_REQUEST_DIR}/' 2>/dev/null" || echo "")

    if echo "${markers}" | grep -q "${REQUEST_ID}.done"; then
        status="done"
        break
    elif echo "${markers}" | grep -q "${REQUEST_ID}.error"; then
        status="error"
        break
    elif echo "${markers}" | grep -q "${REQUEST_ID}.inprogress"; then
        _info "  VM2 is processing… (${elapsed}s elapsed)"
    else
        _info "  Waiting for VM2 to pick up request… (${elapsed}s elapsed)"
    fi

    sleep "${POLL_INTERVAL}"
    elapsed=$(( elapsed + POLL_INTERVAL ))
done

echo ""

if [[ "${status}" == "error" ]]; then
    error_msg=$(_ssh "cat '${VM2_REQUEST_DIR}/${REQUEST_ID}.error' 2>/dev/null" || echo "(no error file)")
    _bad "VM2 reported an error:"
    echo "  ${error_msg}"
    exit 2
fi

if [[ "${status}" != "done" ]]; then
    _bad "Timed out after ${POLL_TIMEOUT}s waiting for VM2."
    _info "The request is still at: ${VM2_REQUEST_DIR}/${REQUEST_ID}.inprogress"
    _info "To retry later: ssh ${VM2_USER}@${VM2_HOST} mv ${VM2_REQUEST_DIR}/${REQUEST_ID}.inprogress ${VM2_REQUEST_DIR}/${REQUEST_ID}.json"
    exit 3
fi

_good "VM2 restore complete."

# Step 4 – Show what landed in backup-incoming
if [[ "${OPT_SEND_TO_VM1}" == "true" ]]; then
    echo ""
    _info "Cycles received in ${VM1_BACKUP_INCOMING}/${OPT_VERSION}/:"
    if [[ -d "${VM1_BACKUP_INCOMING}/${OPT_VERSION}" ]]; then
        ls -1 "${VM1_BACKUP_INCOMING}/${OPT_VERSION}" 2>/dev/null | \
            awk '{print "  [cycle] " $0}'
        cycle_count=$(ls -1 "${VM1_BACKUP_INCOMING}/${OPT_VERSION}" 2>/dev/null | wc -l)
        _good "${cycle_count} cycle(s) ready in backup-incoming"
    else
        _info "(directory not yet visible — VM2 may still be syncing)"
    fi
fi

# Step 5 – Bump chain version in state DB
if [[ "${OPT_BUMP}" == "1" ]]; then
    echo ""
    _info "Bumping chain version in state DB …"

    # Ensure state DB parent dir exists (may have been wiped by reset_system)
    mkdir -p "$(dirname "${VM1_STATE_DB}")"

    new_version=$(python3 - <<PYEOF
import sqlite3, os

state_db = "${VM1_STATE_DB}"
os.makedirs(os.path.dirname(state_db), exist_ok=True)

conn = sqlite3.connect(state_db)
conn.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT)")
conn.commit()
row = conn.execute("SELECT value FROM metadata WHERE key='chain_version'").fetchone()
old = (row[0] or '').strip() if row else ''
new_n = 2
if old.startswith('chain-v'):
    try:
        new_n = int(old[len('chain-v'):]) + 1
    except ValueError:
        pass
new_ver = f'chain-v{new_n}'
conn.execute("REPLACE INTO metadata(key,value) VALUES(?,?)", ('chain_version', new_ver))
conn.commit()
conn.close()
print(new_ver)
PYEOF
    )
    _good "Chain version bumped → ${new_version}"
    _info "Next backup cycle will write to backup-cycles/${new_version}/"
fi

# Step 6 – Print ready-to-run restore command
echo ""
_log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
_good "Restore data is ready. Run this to replay into live DBs:"
echo ""
echo "  sudo python3 ${VM1_RESTORE_SCRIPT} \\"
echo "    --out-root    ${VM1_BACKUP_INCOMING} \\"
echo "    --cycles-root ${VM1_BACKUP_INCOMING} \\"
echo "    --version     ${OPT_VERSION} \\"
echo "    --chain       ${OPT_CHAIN} \\"
echo "    --pg-restore \\"
echo "    --mongo-restore \\"
echo "    --promote && \\"
echo "  sudo rm -rf ${VM1_BACKUP_INCOMING}/*"
echo ""
_log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
