import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import List

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None


def jakarta_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Jakarta")
        except Exception:
            pass
    return timezone(timedelta(hours=7))


def _parse_rsync_targets(env_var: str) -> List[str]:
    """Parse a comma-separated list of rsync targets from an env var.

    Each target is either:
      - An absolute local path (e.g. /home/recovery/local-backup/incoming)
      - A remote rsync target (e.g. recovery@192.168.10.129:/home/recovery/local-backup/incoming)

    Blank entries are silently ignored.
    """
    raw = os.environ.get(env_var, "")
    return [t.strip() for t in raw.split(",") if t.strip()]


@dataclass(frozen=True)
class BackupConfig:
    # -------------------------------------------------------------------------
    # Feature 4: Version-control chain directory
    # -------------------------------------------------------------------------
    # The active chain version string, e.g. "chain-v1".  All cycle output is
    # placed under  cycles_root / chain_version / <cycle_id>/.
    # On restore, the restore script bumps this value (writes chain-v2, etc.)
    # so fresh post-restore cycles never pollute the old poisoned chain.
    #
    # Read from state DB at runtime; this env-var sets the *initial* version
    # when the state DB has no record yet.
    chain_version_init: str = os.environ.get("CHAIN_VERSION_INIT", "chain-v1")

    state_db: str = "/home/primary/data/backup-metadata/pfc_index.db"
    staging_root: str = "/home/primary/data/backup-staging"

    # cycles_root is now the *parent* of the versioned chain directory.
    # The actual output directory used at runtime is cycles_root/chain_version/.
    # The BackupConfig itself exposes cycles_root as the parent so that
    # retention, transfer, and restore tools can discover chain dirs within it.
    cycles_root: str = "/home/primary/data/backup-cycles"

    pg_wal_archive: str = "/home/primary/data/transactional/postgres/wal_archive"
    unstructured_dir: str = "/home/primary/data/unstructured"

    # Container names (used for docker exec fallbacks / base backups)
    pg_docker_container: str = os.environ.get("PG_DOCKER_CONTAINER", "postgres_live")
    pg_compress_level: int = int(os.environ.get("PG_COMPRESS_LEVEL", "10"))
    mongo_docker_container: str = os.environ.get("MONGO_DOCKER_CONTAINER", "mongodb_live")

    # Fixtures: seed representative office/PDF files into unstructured_dir so PFC covers them.
    fixtures_enable: bool = os.environ.get("FIXTURES_ENABLE", "1") == "1"
    fixtures_dir: str = os.environ.get("FIXTURES_DIR", "/home/primary/utilities/fixtures")
    fixtures_target_subdir: str = os.environ.get("FIXTURES_TARGET_SUBDIR", "fixtures")

    # PFC settings
    chunk_size: int = 1024 * 1024

    # PFC delta compression (artifact-level) for transfer
    # Uses zlib (stdlib) to avoid extra dependencies.
    pfc_compress: bool = os.environ.get("PFC_COMPRESS", "1") == "1"
    pfc_compress_level: int = int(os.environ.get("PFC_COMPRESS_LEVEL", "10"))

    # Mongo connection defaults (override via env)
    mongo_host: str = os.environ.get("MONGO_HOST", "127.0.0.1")
    mongo_port: int = int(os.environ.get("MONGO_PORT", "27017"))
    mongo_user: str = os.environ.get("MONGO_USER", "mongodb")
    mongo_password: str = os.environ.get("MONGO_PASSWORD", "password")
    mongo_auth_source: str = os.environ.get("MONGO_AUTH_SOURCE", "admin")
    mongo_uri: str | None = os.environ.get("MONGO_URI")

    mongo_server_selection_timeout_ms: int = int(os.environ.get("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000"))
    mongo_connect_timeout_ms: int = int(os.environ.get("MONGO_CONNECT_TIMEOUT_MS", "5000"))

    # Mongo oplog delta compression (artifact-level) for transfer
    mongo_compress: bool = os.environ.get("MONGO_COMPRESS", "1") == "1"
    mongo_compress_level: int = int(os.environ.get("MONGO_COMPRESS_LEVEL", "10"))

    # If pymongo isn't available, we can still extract oplog by shelling out to the MongoDB
    # container (default name matches utilities/mongodb/mongodb-manifest.yml).
    # (kept above for consistency across tools)

    # Postgres physical base backup (pg_basebackup) into cycle artifacts
    # Default is ON (scheduled) so restore validation is always possible.
    pg_basebackup_enable: bool = os.environ.get("PG_BASEBACKUP_ENABLE", "1") == "1"
    # When set, always take a basebackup on every cycle (manual quick tests).
    pg_basebackup_force: bool = os.environ.get("PG_BASEBACKUP_FORCE", "0") == "1"
    # Take a base backup if we have never taken one before (per state DB),
    # otherwise take one every N cycles.
    pg_basebackup_every_n_cycles: int = int(os.environ.get("PG_BASEBACKUP_EVERY_N_CYCLES", "7"))
    pg_basebackup_max_rate: str = os.environ.get("PG_BASEBACKUP_MAX_RATE", "50M")
    pg_basebackup_checkpoint: str = os.environ.get("PG_BASEBACKUP_CHECKPOINT", "spread")
    # Container path for wal_archive (matches docker-compose volume mount)
    pg_wal_archive_container_path: str = os.environ.get(
        "PG_WAL_ARCHIVE_CONTAINER_PATH", "/var/lib/postgresql/wal_archive"
    )

    # Mongo logical base backup (mongodump --archive --gzip --oplog) into cycle artifacts
    # Default is ON (scheduled) so restore validation is always possible.
    mongo_basebackup_enable: bool = os.environ.get("MONGO_BASEBACKUP_ENABLE", "1") == "1"
    # When set, always take a basebackup on every cycle (manual quick tests)
    mongo_basebackup_force: bool = os.environ.get("MONGO_BASEBACKUP_FORCE", "0") == "1"
    mongo_basebackup_db: str = os.environ.get("MONGO_BASEBACKUP_DB", "test")
    mongo_basebackup_oplog: bool = os.environ.get("MONGO_BASEBACKUP_OPLOG", "1") == "1"
    # Take a base backup if we have never taken one before (per state DB),
    # otherwise take one every N cycles.
    mongo_basebackup_every_n_cycles: int = int(os.environ.get("MONGO_BASEBACKUP_EVERY_N_CYCLES", "7"))

    # Postgres connection (only for optional LSN query)
    pg_enable_lsn: bool = os.environ.get("PG_ENABLE_LSN", "0") == "1"
    pg_db: str = os.environ.get("PG_DB", "transactiondb")
    pg_user: str = os.environ.get("PG_USER", "postgresql")
    pg_password: str = os.environ.get("PG_PASSWORD", "password")
    pg_host: str = os.environ.get("PG_HOST", "127.0.0.1")

    # Retention (protect 64GB disks)
    retention_max_cycles: int = int(os.environ.get("RETENTION_MAX_CYCLES", "30"))
    retention_max_bytes: int = int(os.environ.get("RETENTION_MAX_BYTES", str(50 * 1024 * 1024 * 1024)))

    # Safety: don't let a single cycle explode disk usage
    cycle_max_bytes: int = int(os.environ.get("CYCLE_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))

    # -------------------------------------------------------------------------
    # Feature 3: Multi-Destination Transfer
    # -------------------------------------------------------------------------
    # Master on/off switch (replaces old TRANSFER_ENABLE).
    transfer_enable: bool = os.environ.get("TRANSFER_ENABLE", "0") == "1"

    # RECOVERY_RSYNC_TARGETS — comma-separated list of rsync destinations.
    # Each entry is either:
    #   - A local absolute path          → /home/recovery/local-backup/incoming
    #   - A remote rsync target string   → recovery@192.168.10.129:/home/recovery/local-backup/incoming
    #
    # Example (both local backup VM and a cloud NFS mount):
    #   RECOVERY_RSYNC_TARGETS=recovery@192.168.10.129:/home/recovery/local-backup/incoming,/mnt/cloud-nfs/incoming
    #
    # Backwards-compatible: if RECOVERY_RSYNC_TARGETS is not set but
    # RECOVERY_RSYNC_TARGET (singular) is set, it is used as the single target.
    # The old RECOVERY_INCOMING_DIR local fallback is preserved as well.
    recovery_rsync_targets: tuple = ()   # populated below via __post_init__

    # Legacy single-target env vars (kept for backward compat).
    recovery_rsync_target: str | None = os.environ.get("RECOVERY_RSYNC_TARGET")
    recovery_ssh_port: int = int(os.environ.get("RECOVERY_SSH_PORT", "22"))
    recovery_ssh_key: str | None = os.environ.get("RECOVERY_SSH_KEY")
    recovery_incoming_dir: str = os.environ.get(
        "RECOVERY_INCOMING_DIR", "/home/recovery/local-backup/incoming"
    )

    transfer_verify: bool = os.environ.get("TRANSFER_VERIFY", "1") == "1"
    transfer_verify_raw: bool = os.environ.get("TRANSFER_VERIFY_RAW", "0") == "1"

    def __post_init__(self) -> None:
        # Build the effective target list from env, merging new multi-target
        # env var with the old single-target var for backward compatibility.
        targets_raw = _parse_rsync_targets("RECOVERY_RSYNC_TARGETS")

        if not targets_raw:
            # Fall back to old single-target + local-dir behaviour.
            if self.recovery_rsync_target:
                targets_raw = [self.recovery_rsync_target]
            else:
                targets_raw = [self.recovery_incoming_dir]

        # Use object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "recovery_rsync_targets", tuple(targets_raw))

    # -------------------------------------------------------------------------
    # VM1 receive directory — where cycles come BACK from the backup VM
    # during a restore operation (VM2 → VM1 rsync direction).
    # Intentionally a method-level annotation so frozen dataclass allows it.
    # -------------------------------------------------------------------------


# Separate constant so restore scripts can import it without instantiating BackupConfig.
RESTORE_INCOMING_DIR: str = os.environ.get(
    "RESTORE_INCOMING_DIR", "/home/primary/data/backup-incoming"
)


__all__ = ["BackupConfig", "jakarta_tz", "RESTORE_INCOMING_DIR"]
