#!/usr/bin/env bash
# =============================================================================
# run_backup.sh — RPO-controlled backup loop for the Primary VM
#
# Runs backup_orchestrator.py every RPO_MINUTES, which:
#   1. Takes a snapshot of PG WAL + Mongo oplog + PFC unstructured data
#   2. Stores the hashed cycle under  backup-cycles/chain-vN/<cycle_id>/
#   3. Transfers the cycle to the recovery VM via rsync (TRANSFER_ENABLE=1)
#
# Usage:
#   sudo bash utilities/backup/run_backup.sh
#
# Or as non-root with a sudoers NOPASSWD entry:
#   bash utilities/backup/run_backup.sh
#
# To stop:
#   Ctrl+C  (the trap will print a clean exit message)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# ★ CONFIGURE HERE — tweak these to change the RPO
# ---------------------------------------------------------------------------
RPO_MINUTES=5              # How often to run a backup cycle (Recovery Point Objective)
MAX_CONSECUTIVE_FAILS=3     # Abort the loop after this many back-to-back failures

# ---------------------------------------------------------------------------
# Paths (relative to this script's directory, so moveable)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ENV="${SCRIPT_DIR}/backup.env"
ORCHESTRATOR="${SCRIPT_DIR}/backup_orchestrator.py"
MONITOR="${SCRIPT_DIR}/tools/resource_telemetry.py"

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
if [[ ! -f "${BACKUP_ENV}" ]]; then
    echo "<error> backup.env not found at: ${BACKUP_ENV}" >&2
    exit 1
fi
if [[ ! -f "${ORCHESTRATOR}" ]]; then
    echo "<error> backup_orchestrator.py not found at: ${ORCHESTRATOR}" >&2
    exit 1
fi

# Source environment (sets TRANSFER_ENABLE, RECOVERY_RSYNC_TARGETS, SSH key, etc.)
# shellcheck source=backup.env
source "${BACKUP_ENV}"

# ---------------------------------------------------------------------------
# Must run as root (backup tools need access to docker data dirs).
# If not root, re-exec via sudo -E (preserves env from backup.env).
# ---------------------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
    echo "<info> Not root — re-executing via: sudo -E bash $0 $*"
    exec sudo -E bash "$0" "$@"
fi

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
    local term_msg="$(printf "%-10s %s %s" "$scope" "$(_c "<good>  " "32")" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<good>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_info() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %s %s" "$scope" "$(_c "<info>  " "36")" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<info>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_warn() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %s %s" "$scope" "$(_c "<warn>  " "33")" "$*")"
    local file_msg="$(printf "%-10s %-8s %s" "$scope" "<warn>" "$*")"
    echo "$term_msg"
    _log_file "$file_msg"
}

_bad() {
    local scope="${LOG_SCOPE:-main}"
    local term_msg="$(printf "%-10s %s %s" "$scope" "$(_c "<error> " "31")" "$*")"
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

_info "RPO=${RPO_MINUTES}min  transfer=${TRANSFER_ENABLE:-0}  target=${RECOVERY_RSYNC_TARGETS:-<none>}"
_info "Orchestrator: ${ORCHESTRATOR}"
_info "Env:          ${BACKUP_ENV}"
_info "Chain init:   ${CHAIN_VERSION_INIT:-chain-v1}"
echo ""

# ---------------------------------------------------------------------------
# Graceful shutdown on Ctrl+C / SIGTERM
# ---------------------------------------------------------------------------
_cleanup() {
    echo ""
    _info "Received stop signal — backup loop exiting cleanly."
    exit 0
}
trap _cleanup INT TERM

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
consecutive_fails=0
cycle_num=0

while true; do
    cycle_num=$(( cycle_num + 1 ))
    _info "──────────────────────────────────────────────────────"
    _info "Cycle #${cycle_num} starting"

    # Run the orchestrator.
    # -E preserves all sourced env vars through sudo.
    # Redirect stderr to stdout so the log is a single stream.
    if python3 "${ORCHESTRATOR}" 2>&1; then
        consecutive_fails=0
        _good "Cycle #${cycle_num} completed OK"
    else
        consecutive_fails=$(( consecutive_fails + 1 ))
        _bad "Cycle #${cycle_num} FAILED (consecutive_fails=${consecutive_fails}/${MAX_CONSECUTIVE_FAILS})"

        if (( consecutive_fails >= MAX_CONSECUTIVE_FAILS )); then
            _bad "Too many consecutive failures — aborting backup loop."
            exit 2
        fi
    fi

    # Sleep until next cycle.
    sleep_sec=$(( RPO_MINUTES * 60 ))
    _info "Sleeping ${RPO_MINUTES}m until next cycle …"
    echo ""
    sleep "${sleep_sec}"
done
