#!/usr/bin/env bash
# Stop the vm2_service.sh and all its child processes.
if pgrep -f vm2_service.sh > /dev/null; then
  pkill -f vm2_service.sh
  echo "Service stopped."
else
  echo "Service is not running."
fi
# Clean up the lock file so the service can be restarted cleanly.
rm -f /home/recovery/local-backup/state/vm2_service.lock
