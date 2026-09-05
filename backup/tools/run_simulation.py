#!/usr/bin/env python3

import argparse
import os
import time

from lib.config import BackupConfig
from lib.runner import run_cycle


def _now_s() -> float:
    return time.time()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the incremental backup cycle on a schedule (Primary VM).")
    ap.add_argument("--hours", type=float, default=7.0)
    ap.add_argument("--interval-min", type=int, default=20)
    ap.add_argument("--once", action="store_true", help="Run exactly one cycle and exit.")
    args = ap.parse_args()

    cfg = BackupConfig()

    if args.once:
        run_cycle(cfg)
        return 0

    deadline = _now_s() + args.hours * 3600.0
    interval_s = max(60, int(args.interval_min) * 60)

    cycle_num = 0
    while _now_s() < deadline:
        cycle_num += 1
        print(f"sim        <info>   Running cycle #{cycle_num}")
        run_cycle(cfg)

        remaining = deadline - _now_s()
        if remaining <= 0:
            break

        sleep_s = min(interval_s, int(remaining))
        print(f"sim        <info>   Sleeping {sleep_s}s")
        time.sleep(sleep_s)

    print("sim        <info>   Completed simulation window")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
