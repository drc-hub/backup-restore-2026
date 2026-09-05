from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


JAKARTA_TZ = "Asia/Jakarta"


def parse_timestamp(ts: str) -> _dt.datetime:
    """Parse ISO8601 timestamp.

    If no timezone offset is provided, assume Asia/Jakarta.
    Returned datetime is timezone-aware.
    """
    dt = _dt.datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        if ZoneInfo is None:
            raise ValueError("timestamp is naive and zoneinfo is unavailable")
        dt = dt.replace(tzinfo=ZoneInfo(JAKARTA_TZ))
    return dt


@dataclass(frozen=True)
class CycleInfo:
    cycle_id: str
    path: Path
    cycle_timestamp: _dt.datetime


def read_cycle_timestamp_from_manifest(cycle_dir: Path) -> _dt.datetime:
    manifest = cycle_dir / "manifest.json"
    with open(manifest, "r", encoding="utf-8") as f:
        obj = json.load(f)
    ts = obj.get("cycle_timestamp")
    if not isinstance(ts, str) or not ts:
        raise ValueError(f"missing cycle_timestamp in {manifest}")
    return parse_timestamp(ts)


def list_cycles_from_encrypted_meta(encrypted_root: Path) -> list[CycleInfo]:
    """List cycles based on <cycle_id>.tar.aes256gcm.meta.json.

    Requires that metadata contains `cycle_timestamp`.
    """
    cycles: list[CycleInfo] = []
    if not encrypted_root.exists():
        return cycles
    for meta_path in sorted(encrypted_root.glob("*.tar.aes256gcm.meta.json")):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            cycle_id = obj.get("cycle_id")
            ts = obj.get("cycle_timestamp")
            if not isinstance(cycle_id, str) or not cycle_id:
                continue
            if not isinstance(ts, str) or not ts:
                continue
            cycles.append(
                CycleInfo(
                    cycle_id=cycle_id,
                    path=encrypted_root / cycle_id,
                    cycle_timestamp=parse_timestamp(ts),
                )
            )
        except Exception:
            continue
    return cycles


def pick_target_cycle_id(cycles: list[CycleInfo], *, target_time: _dt.datetime) -> Optional[str]:
    # Choose newest cycle where cycle_timestamp <= target_time.
    best: Optional[CycleInfo] = None
    for c in cycles:
        if c.cycle_timestamp <= target_time:
            if best is None or c.cycle_timestamp > best.cycle_timestamp:
                best = c
    return best.cycle_id if best else None


def select_cycle_ids_upto(cycle_ids: list[str], target_cycle_id: str) -> list[str]:
    # Lexicographic ordering contract.
    out = [cid for cid in sorted(cycle_ids) if cid <= target_cycle_id]
    return out
