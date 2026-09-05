# Incremental Backup-Restore Architecture — Experiment Artifacts

This repository contains the implementation and experimental artifacts for an
incremental backup and recovery architecture for heterogeneous data
(PostgreSQL, MongoDB, and unstructured files) in a resource-constrained,
two-VM virtualized environment.

## Architecture Overview

The system consists of two virtual machines:

- **Primary VM** — generates the test workload (database insertions and
  unstructured file mutations), harvests deltas (PostgreSQL WAL, MongoDB
  oplog, and fixed-size chunks), and initiates backup/restore requests.
- **Backup VM** — receives cycle packages, verifies integrity, encrypts
  data using AES-256-GCM, and replicates encrypted archives to local and
  cloud object storage (general + immutable WORM buckets).

## Repository Structure

- `primary-vm/` — Primary VM scripts: workload generators, WAL/oplog
  harvesters, unstructured chunking and deduplication module.
- `backup-vm/` — Backup VM scripts: cycle processing, encryption,
  replication, and restore-request handling.
  - `lib/` — shared core modules (AES-256-GCM encrypt/decrypt, chunk
    selection, filesystem utilities, telemetry logging).
- `raw-result/` — raw experiment logs and monitoring data collected
  during the 7-hour evaluation run, including:
  - `backup.log` — orchestrator log for all 21 backup cycles.
  - `restore_telemetry.csv` — per-scenario restoration timing breakdown.
  - `pg_events.csv`, `mongo_events.csv`, `unstructured_events.csv` /
    `unstructured_growth.csv` — periodic monitoring of database record
    counts and unstructured dataset size.
  - `p-system_*`, `r-system_*`, `p-mem_*`, `r-mem_*` — system resource
    telemetry (CPU/memory pressure) captured via Netdata.
- `.vm2.env.example` — example environment configuration (template only;
  no real credentials or secret keys are included).

## Notes

- This repository is provided as supplementary material to support the
  reproducibility of the reported experimental results.
- Raw monitoring data in `raw-result/` corresponds directly to the
  performance metrics, compression ratios, and restoration verification
  results reported in the manuscript.
