#!/usr/bin/env bash
# =============================================================================
# vm2_service.sh  –  VM2 continuous backup-and-restore service (chain-aware)
# =============================================================================
#
# TWO concurrent workers run inside this process:
#
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  BACKUP WORKER (background subshell)                                │
#   │  • Scans incoming/<chain-v>/<cycle_id>/ every VM2_POLL_SECONDS      │
#   │  • Encrypts (AES-256-GCM + Base64) → encrypted/<chain-v>/           │
#   │  • Copies raw to permanent/<chain-v>/ (disabled)                    │
#   │  • Deletes incoming cycle dir after success                         │
#   │  • Optional uploads to general / immutable cloud buckets            │
#   └─────────────────────────────────────────────────────────────────────┘
#
#   ┌─────────────────────────────────────────────────────────────────────┐
#   │  RESTORE LISTENER (foreground, event-driven)                        │
#   │  • Watches restore-requests/ with inotifywait (instant wakeup)      │
#   │  • Falls back to polling if inotifywait is unavailable              │
#   │  • On new *.json: pauses backup → decrypts → rsync to VM1 → resumes │
#   │  • Request schema: { version, chain, source, send_to_vm1,           │
#   │                      vm1_dest_root }                                │
#   └─────────────────────────────────────────────────────────────────────┘
#
# Required:
#   VM2_AES_KEY_B64  – Base64-encoded 32-byte AES-256 key. NEVER logged.
#
# Cloud upload (OPTIONAL – placeholder until buckets configured):
#   VM2_UPLOAD_GENERAL_CMD    – {src} {cycle_id} {chain_v} placeholders
#   VM2_UPLOAD_IMMUTABLE_CMD  – same placeholders
#   VM2_ENABLE_IMMUTABLE_UPLOAD – "1" to enable (default: "0")
#
# Cloud restore (OPTIONAL – placeholder until rclone configured):
#   VM2_RCLONE_GENERAL_REMOTE   – rclone remote:path for general bucket
#   VM2_RCLONE_IMMUTABLE_REMOTE – rclone remote:path for immutable bucket
#
# Usage:
#   VM2_AES_KEY_B64=<key> ./utilities/vm2_service.sh
# =============================================================================
set -euo pipefail
cd /   # never run with a CWD that might be deleted

# ── logging ──────────────────────────────────────────────────────────────────
# LOG_FILE is resolved after .vm2.env is loaded below.

log_info() {
  local ts="$(date +"%Y-%m-%dT%H:%M:%S%z")"
  echo "[$ts] <INFO> $*" | tee -a "$LOG_FILE"
}
log_good() {
  local ts="$(date +"%Y-%m-%dT%H:%M:%S%z")"
  echo "[$ts] <GOOD> $*" | tee -a "$LOG_FILE"
}
log_warn() {
  local ts="$(date +"%Y-%m-%dT%H:%M:%S%z")"
  echo "[$ts] <WARN> $*" | tee -a "$LOG_FILE" >&2
}
log_error() {
  local ts="$(date +"%Y-%m-%dT%H:%M:%S%z")"
  echo "[$ts] <ERROR> $*" | tee -a "$LOG_FILE" >&2
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    log_error "required env var $name is not set"
    exit 2
  fi
}

# ── configuration ─────────────────────────────────────────────────────────────
# All defaults live in .vm2.env, loaded by start_service.sh via:
#   set -a; source utilities/.vm2.env; set +a
# Values set in the environment always take precedence.

# Load .vm2.env if it exists and vars aren't already exported (dev/direct-run fallback).
_ENV_FILE="$(dirname "${BASH_SOURCE[0]}")/.vm2.env"
if [[ -f "$_ENV_FILE" && -z "${VM2_AES_KEY_B64:-}" ]]; then
  set -a
  # shellcheck source=.vm2.env
  source "$_ENV_FILE"
  set +a
fi
unset _ENV_FILE

# Validate that every required variable is present.
require_env VM2_AES_KEY_B64
require_env GOOGLE_APPLICATION_CREDENTIALS
export GOOGLE_APPLICATION_CREDENTIALS

# Resolve log file path now that .vm2.env has been loaded.
LOG_FILE="${VM2_LOG_FILE:-/home/recovery/local-backup/state/vm2_service.log}"

# Resolve optional vars with safe defaults (only used if not set in .vm2.env).
VM2_POLL_SECONDS="${VM2_POLL_SECONDS:-15}"
VM2_INCOMING_DIR="${VM2_INCOMING_DIR:-/home/recovery/local-backup/incoming}"
VM2_ENCRYPTED_ROOT="${VM2_ENCRYPTED_ROOT:-/home/recovery/local-backup/encrypted}"
VM2_WORK_DIR="${VM2_WORK_DIR:-}"
VM2_DELETE_INCOMING_ON_SUCCESS="${VM2_DELETE_INCOMING_ON_SUCCESS:-1}"
VM2_RESTORE_REQUEST_DIR="${VM2_RESTORE_REQUEST_DIR:-/home/recovery/local-backup/restore-requests}"
VM2_OUTGOING_ROOT="${VM2_OUTGOING_ROOT:-/home/recovery/local-backup/outgoing/primary}"
VM2_VM1_DEST_ROOT_DEFAULT="${VM2_VM1_DEST_ROOT_DEFAULT:-primary@192.168.10.128:/home/primary/data/backup-incoming}"
STATE_DIR="${VM2_STATE_DIR:-/home/recovery/local-backup/state}"
LOCK_FILE="$STATE_DIR/vm2_service.lock"

# Cloud upload settings.
VM2_ENABLE_GENERAL_UPLOAD="${VM2_ENABLE_GENERAL_UPLOAD:-1}"
VM2_UPLOAD_GENERAL_CMD="${VM2_UPLOAD_GENERAL_CMD:-}"
VM2_ENABLE_IMMUTABLE_UPLOAD="${VM2_ENABLE_IMMUTABLE_UPLOAD:-0}"
VM2_UPLOAD_IMMUTABLE_CMD="${VM2_UPLOAD_IMMUTABLE_CMD:-}"

# rclone remotes for cloud restore.
VM2_RCLONE_GENERAL_REMOTE="${VM2_RCLONE_GENERAL_REMOTE:-}"
VM2_RCLONE_IMMUTABLE_REMOTE="${VM2_RCLONE_IMMUTABLE_REMOTE:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_PY="$SCRIPT_DIR/vm2_cycle_processor.py"
RECOVERY_PY="$SCRIPT_DIR/vm2_recovery_sender.py"

# ── global state ──────────────────────────────────────────────────────────────
# PID of the background backup worker subshell.
BACKUP_PID=""

# Named pipe used to signal the restore listener from the backup worker.
SIGNAL_PIPE=""

# ── cleanup ───────────────────────────────────────────────────────────────────
cleanup() {
  log_info "vm2_service shutting down …"
  if [[ -n "$BACKUP_PID" ]] && kill -0 "$BACKUP_PID" 2>/dev/null; then
    kill "$BACKUP_PID" 2>/dev/null || true
    wait "$BACKUP_PID" 2>/dev/null || true
  fi
  if [[ -n "$SIGNAL_PIPE" && -p "$SIGNAL_PIPE" ]]; then
    rm -f "$SIGNAL_PIPE" 2>/dev/null || true
  fi
  log_info "vm2_service stopped."
}
trap cleanup EXIT INT TERM

# ── directory setup ───────────────────────────────────────────────────────────
ensure_dirs() {
  mkdir -p \
    "$VM2_INCOMING_DIR" \
    "$VM2_ENCRYPTED_ROOT" \
    "$VM2_RESTORE_REQUEST_DIR" \
    "$VM2_OUTGOING_ROOT" \
    "$STATE_DIR" \
    || true
}

# ── parse restore request JSON ────────────────────────────────────────────────
# Prints key=value lines: version, chain, source, send_to_vm1, vm1_dest_root
parse_request_json() {
  local req_file="$1"
  python3 - <<'PY' "$req_file" "$VM2_VM1_DEST_ROOT_DEFAULT"
import json, sys
from pathlib import Path

req_path = Path(sys.argv[1])
def_vm1  = sys.argv[2]

obj = json.loads(req_path.read_text(encoding='utf-8'))

version = obj.get('version') or obj.get('chain_v')
if not isinstance(version, str) or not version.strip():
    raise SystemExit("missing required field: version")

chain_n = obj.get('chain')
if chain_n is None:
    raise SystemExit("missing required field: chain")
try:
    chain_n = int(chain_n)
except (TypeError, ValueError):
    raise SystemExit("field 'chain' must be an integer")
if chain_n < 1:
    raise SystemExit("field 'chain' must be >= 1")

source = obj.get('source', 'local')
if source not in ('local', 'general', 'immutable'):
    raise SystemExit("invalid source (must be local|general|immutable)")

send = obj.get('send_to_vm1', True)
if isinstance(send, bool):
    send_to_vm1 = 1 if send else 0
elif isinstance(send, (int, float)):
    send_to_vm1 = 1 if int(send) != 0 else 0
else:
    send_to_vm1 = 1 if str(send).strip().lower() not in ('0','false','no','off','') else 0

vm1_dest = obj.get('vm1_dest_root', def_vm1)
if not isinstance(vm1_dest, str) or not vm1_dest.strip():
    vm1_dest = def_vm1

print(f"version={version.strip()}")
print(f"chain={chain_n}")
print(f"source={source}")
print(f"send_to_vm1={send_to_vm1}")
print(f"vm1_dest_root={vm1_dest.strip()}")
PY
}

# ── backup worker ─────────────────────────────────────────────────────────────
# Called in a background subshell. Loops forever, running the cycle processor
# every VM2_POLL_SECONDS. Writes "tick" to SIGNAL_PIPE after each pass so the
# restore listener knows the backup round has finished.
run_backup_loop() {
  local pipe="$1"

  export VM2_DELETE_INCOMING_ON_SUCCESS
  export VM2_ENABLE_GENERAL_UPLOAD
  export VM2_UPLOAD_GENERAL_CMD
  export VM2_UPLOAD_IMMUTABLE_CMD
  export VM2_ENABLE_IMMUTABLE_UPLOAD

  while true; do
    local args
    args=(
      "--once"
      "--incoming-dir"   "$VM2_INCOMING_DIR"
      "--encrypted-root" "$VM2_ENCRYPTED_ROOT"
    )
    [[ -n "$VM2_WORK_DIR" ]] && args+=("--work-dir" "$VM2_WORK_DIR")

    "$BACKUP_PY" "${args[@]}" || true

    sleep "$VM2_POLL_SECONDS"
  done
}

# ── execute one restore request ───────────────────────────────────────────────
execute_restore_request() {
  local req="$1"
  local base done_file err_file inprog

  base="${req%.json}"
  done_file="$base.done"
  err_file="$base.error"
  inprog="$base.inprogress"

  # Atomically claim the request.
  if ! mv "$req" "$inprog" 2>/dev/null; then
    log_info "restore: could not claim $(basename "$req") (already taken?)"
    return 0
  fi

  log_info "━━ restore request received: $(basename "$inprog") ━━"

  # Parse.
  local version chain source send_to_vm1 vm1_dest_root
  version=""; chain=""; source="local"; send_to_vm1=1
  vm1_dest_root="$VM2_VM1_DEST_ROOT_DEFAULT"

  local kv_lines
  if ! mapfile -t kv_lines < <(parse_request_json "$inprog" 2>&1); then
    log_error "failed to parse restore request — $(cat "$inprog" 2>/dev/null | head -3)"
    echo "parse_error" > "$err_file"
    mv "$inprog" "$base.json" 2>/dev/null || true
    return 0
  fi

  for line in "${kv_lines[@]}"; do
    case "$line" in
      version=*)     version="${line#version=}";;
      chain=*)       chain="${line#chain=}";;
      source=*)      source="${line#source=}";;
      send_to_vm1=*) send_to_vm1="${line#send_to_vm1=}";;
      vm1_dest_root=*) vm1_dest_root="${line#vm1_dest_root=}";;
    esac
  done

  if [[ -z "$version" || -z "$chain" ]]; then
    log_error "restore request missing version or chain field"
    echo "missing_fields" > "$err_file"
    return 0
  fi

  log_info "  version  : $version"
  log_info "  chain    : $chain  (oldest $chain cycle(s))"
  log_info "  source   : $source"
  log_info "  send_vm1 : $send_to_vm1"
  [[ "$send_to_vm1" == "1" ]] && log_info "  vm1_dest : $vm1_dest_root"

  # Build recovery args.
  local args
  args=(
    "--version"        "$version"
    "--chain"          "$chain"
    "--source"         "$source"
    "--encrypted-root" "$VM2_ENCRYPTED_ROOT"
    "--outgoing-root"  "$VM2_OUTGOING_ROOT"
  )

  if [[ "$send_to_vm1" == "1" ]]; then
    export VM1_RESTORE_DEST_ROOT="$vm1_dest_root"
    args+=("--send-to-vm1")
  fi

  # Cloud source validation.
  if [[ "$source" == "general" || "$source" == "immutable" ]]; then
    if ! command -v rclone >/dev/null 2>&1; then
      log_error "rclone not found; required for source=$source"
      echo "rclone_not_found" > "$err_file"
      return 0
    fi
    if [[ "$source" == "general" ]]; then
      if [[ -z "${VM2_RCLONE_GENERAL_REMOTE:-}" ]]; then
        log_error "VM2_RCLONE_GENERAL_REMOTE not set (required for source=general)"
        echo "missing_rclone_general" > "$err_file"
        return 0
      fi
      export VM2_RCLONE_GENERAL_REMOTE
    else
      if [[ -z "${VM2_RCLONE_IMMUTABLE_REMOTE:-}" ]]; then
        log_error "VM2_RCLONE_IMMUTABLE_REMOTE not set (required for source=immutable)"
        echo "missing_rclone_immutable" > "$err_file"
        return 0
      fi
      export VM2_RCLONE_IMMUTABLE_REMOTE
    fi
  fi

  # Execute.
  if "$RECOVERY_PY" "${args[@]}" >> "$LOG_FILE" 2>&1; then
    log_good "━━ restore SUCCEEDED: $version / $chain cycle(s) ━━"
    echo "ok" > "$done_file"
    rm -f "$err_file" "$inprog" 2>/dev/null || true
  else
    log_error "━━ restore FAILED: $version / $chain cycle(s) ━━"
    echo "failed" > "$err_file"
    log_info "  → inspect $inprog, then rename to .json to retry"
  fi
}

# ── drain all pending restore requests ───────────────────────────────────────
# Processes every *.json in restore-requests/ that isn't already .done
process_pending_requests() {
  shopt -s nullglob
  local req found=0
  for req in "$VM2_RESTORE_REQUEST_DIR"/*.json; do
    local base="${req%.json}"
    [[ -f "$base.done" ]] && continue   # already finished
    found=1
    execute_restore_request "$req"
  done
  return 0
}

# ── restore listener (main foreground loop) ───────────────────────────────────
# Uses inotifywait if available for instant wakeup, otherwise falls back to
# polling with a short sleep.
run_restore_listener() {
  local pipe="$1"
  local use_inotify=0

  if command -v inotifywait >/dev/null 2>&1; then
    use_inotify=1
    log_info "restore listener: using inotifywait (instant wakeup on request arrival)"
  else
    log_info "restore listener: inotifywait not found — using poll fallback (${VM2_POLL_SECONDS}s)"
    log_info "  → install inotify-tools for instant restore request detection"
  fi

  # Drain anything already sitting in the request dir at startup.
  process_pending_requests

  while true; do
    if [[ "$use_inotify" == "1" ]]; then
      # Block until a file is written/moved into restore-requests/, or timeout.
      # -t timeout ensures we don't block forever if inotifywait dies.
      local event_file=""
      event_file=$(
        inotifywait -q \
          --format "%f" \
          --event close_write,moved_to \
          --timeout "$VM2_POLL_SECONDS" \
          "$VM2_RESTORE_REQUEST_DIR" 2>/dev/null
      ) || true   # timeout exits 1, that's fine

      if [[ -n "$event_file" && "$event_file" == *.json ]]; then
        log_info "┌─ restore request detected: $event_file"
        # Give writer a moment to fully flush if using close_write.
        sleep 0.2
        process_pending_requests
        log_info "└─ restore listener: back to watching restore-requests/"
      else
        # Timeout or non-JSON event: just drain in case anything arrived.
        process_pending_requests
      fi

    else
      # Fallback: poll every VM2_POLL_SECONDS.
      sleep "$VM2_POLL_SECONDS"
      process_pending_requests
    fi
  done
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
  ensure_dirs

  # Dependency checks.
  for dep in python3 sha256sum rsync flock; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      log_error "$dep not found (required)"
      exit 2
    fi
  done

  # AES key required for encryption of every backup cycle.
  require_env VM2_AES_KEY_B64

  # Single-instance lock (prevents two service instances running in parallel).
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log_error "another vm2_service.sh is already running (lock: $LOCK_FILE)"
    exit 1
  fi

  # ── startup banner ──────────────────────────────────────────────────────────
  log_info "╔══════════════════════════════════════════════════╗"
  log_info "║           vm2_service  starting up               ║"
  log_info "╚══════════════════════════════════════════════════╝"
  log_info "  incoming     : $VM2_INCOMING_DIR"
  log_info "  encrypted    : $VM2_ENCRYPTED_ROOT"
  log_info "  restore-reqs : $VM2_RESTORE_REQUEST_DIR"
  log_info "  outgoing     : $VM2_OUTGOING_ROOT"
  log_info "  poll_seconds : $VM2_POLL_SECONDS"

  if [[ "$VM2_ENABLE_GENERAL_UPLOAD" == "1" && -n "$VM2_UPLOAD_GENERAL_CMD" ]]; then
    log_info "  cloud general  : ENABLED"
  else
    log_info "  cloud general  : disabled"
  fi
  if [[ "$VM2_ENABLE_IMMUTABLE_UPLOAD" == "1" && -n "$VM2_UPLOAD_IMMUTABLE_CMD" ]]; then
    log_info "  cloud immutable: ENABLED"
  else
    log_info "  cloud immutable: disabled"
  fi

  # ── named pipe for backup→listener signalling ───────────────────────────────
  SIGNAL_PIPE="$STATE_DIR/vm2_backup.pipe"
  rm -f "$SIGNAL_PIPE" 2>/dev/null || true
  mkfifo "$SIGNAL_PIPE"

  # ── start backup worker in background ──────────────────────────────────────
  log_info "starting backup worker (background) …"
  run_backup_loop "$SIGNAL_PIPE" &
  BACKUP_PID=$!
  log_info "backup worker PID: $BACKUP_PID"

  # ── start restore listener in foreground ────────────────────────────────────
  log_info "starting restore listener (foreground) …"
  log_info "  drop a JSON file into $VM2_RESTORE_REQUEST_DIR to trigger restore"
  run_restore_listener "$SIGNAL_PIPE"
}

main "$@"
