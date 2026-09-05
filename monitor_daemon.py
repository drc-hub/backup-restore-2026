#!/usr/bin/env python3
"""
monitor_daemon.py
Monitors system-wide performance, resource usage, and time taken.
Outputs metrics in JSON Lines format for data analysis.
"""

import json
import time
import os
import datetime
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:
    import pytz as ZoneInfo # fallback if somehow not 3.9+

LOG_FILE = Path("/home/recovery/local-backup/state/monitor_data.jsonl")

def get_cpu_times():
    try:
        with open("/proc/stat", "r") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = line.split()
                    user, nice, system, idle, iowait, irq, softirq = map(float, parts[1:8])
                    idle_time = idle + iowait
                    total_time = user + nice + system + idle + iowait + irq + softirq
                    return idle_time, total_time
    except Exception:
        pass
    return 0.0, 0.0

def get_mem_info():
    mem = {"total": 0, "available": 0, "free": 0}
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if line.startswith("MemTotal:"):
                    mem["total"] = int(parts[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem["available"] = int(parts[1]) * 1024
                elif line.startswith("MemFree:"):
                    mem["free"] = int(parts[1]) * 1024
    except Exception:
        pass
    return mem

def get_disk_info(path="/"):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {"total": total, "free": free, "used": used, "percent": round((used/total)*100, 2) if total > 0 else 0}
    except Exception:
        return {"total": 0, "free": 0, "used": 0, "percent": 0.0}

def get_load_avg():
    try:
        with open("/proc/loadavg", "r") as f:
            return f.read().strip().split()[:3]
    except Exception:
        return ["0.00", "0.00", "0.00"]

def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    poll_interval = 10.0 # seconds
    
    prev_idle, prev_total = get_cpu_times()
    
    print(f"Starting monitor_daemon. Logging to {LOG_FILE}")
    
    while True:
        start_time = time.time()
        
        # Calculate CPU usage
        time.sleep(poll_interval)
        curr_idle, curr_total = get_cpu_times()
        
        total_diff = curr_total - prev_total
        idle_diff = curr_idle - prev_idle
        cpu_percent = 0.0
        if total_diff > 0:
            cpu_percent = round(100.0 * (1.0 - idle_diff / total_diff), 2)
        
        prev_idle, prev_total = curr_idle, curr_total
        
        mem = get_mem_info()
        mem_used = mem["total"] - mem["available"]
        mem_percent = round((mem_used / mem["total"]) * 100, 2) if mem["total"] > 0 else 0.0
        
        disk = get_disk_info("/home/recovery/local-backup")
        load_avg = get_load_avg()
        
        end_time = time.time()
        time_taken = round(end_time - start_time - poll_interval, 4)
        
        jakarta_tz = ZoneInfo("Asia/Jakarta") if 'ZoneInfo' in globals() else None
        
        metrics = {
            "timestamp": datetime.datetime.now(jakarta_tz).isoformat(),
            "cpu_percent": cpu_percent,
            "memory": {
                "total_bytes": mem["total"],
                "available_bytes": mem["available"],
                "percent_used": mem_percent
            },
            "disk": disk,
            "load_average": load_avg,
            "metrics_collection_time_sec": time_taken,
            "event": "system_poll"
        }
        
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(metrics) + "\n")

if __name__ == "__main__":
    main()
