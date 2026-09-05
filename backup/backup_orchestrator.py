"""backup_orchestrator.py

Lightweight incremental backup orchestrator (primary VM).

Writes per-cycle artifacts under `/home/primary/data/backup-cycles/<cycle_id>/`:
- pg/: WAL files harvested since last cycle timestamp
- mongo/: oplog delta JSON since last stored ts
- pfc/: chunk deltas for unstructured data
- checksums.sha256 + manifest.json

All timestamps are Asia/Jakarta (UTC+7).
"""

#!/usr/bin/env python3

import os

from lib.config import BackupConfig
from lib.runner import run_cycle


def main() -> int:
    try:
        if os.environ.get("BACKUP_PAUSED", "0") in {"1", "true", "TRUE", "yes", "YES"}:
            print("main       <info>   BACKUP_PAUSED=1; skipping backup cycle.")
            return 0
        cfg = BackupConfig()
        run_cycle(cfg)
        return 0
    except Exception as e:
        print(f"main       <error>  Backup cycle failed: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
