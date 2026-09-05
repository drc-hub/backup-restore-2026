import csv
import os
import fcntl
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

TELEMETRY_CSV = Path("/home/recovery/local-backup/state/workflow_telemetry.csv")

@dataclass
class WorkflowTelemetry:
    timestamp: str
    workflow_type: str
    chain_v: str
    cycle_id: str
    raw_size_bytes: Optional[int] = None
    encrypted_size_bytes: Optional[int] = None
    duration_aes256_sec: Optional[float] = None
    duration_cloud_transfer_sec: Optional[float] = None
    duration_vm1_transfer_sec: Optional[float] = None
    total_workflow_sec: Optional[float] = None

def write_telemetry(record: WorkflowTelemetry) -> None:
    file_exists = TELEMETRY_CSV.exists()
    
    fields = [
        "timestamp",
        "workflow_type",
        "chain_v",
        "cycle_id",
        "raw_size_bytes",
        "encrypted_size_bytes",
        "duration_aes256_sec",
        "duration_cloud_transfer_sec",
        "duration_vm1_transfer_sec",
        "total_workflow_sec"
    ]
    
    row = {f: getattr(record, f) for f in fields}
    
    with open(TELEMETRY_CSV, "a", newline="") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            writer = csv.DictWriter(f, fieldnames=fields)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
