#!/usr/bin/env bash
rm -f /home/recovery/local-backup/restore-requests/* 2>/dev/null || true
set -a
source /home/recovery/utilities/.vm2.env
set +a
nohup /home/recovery/utilities/vm2_service.sh > /home/recovery/local-backup/state/vm2.log 2>&1 &
echo "Service PID: $!"
