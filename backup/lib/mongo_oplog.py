from __future__ import annotations

import os
import time
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qs, urlencode
import shutil
import subprocess

from .state import get_metadata, set_metadata

try:
    from pymongo import MongoClient
    from bson.timestamp import Timestamp as BsonTimestamp
    from bson.json_util import dumps as bson_dumps
except Exception:
    MongoClient = None
    BsonTimestamp = None
    bson_dumps = None


def build_default_mongo_uri(*, host: str, port: int, user: str, password: str, auth_source: str) -> str:
    u = quote_plus(user)
    p = quote_plus(password)
    a = quote_plus(auth_source)
    return f"mongodb://{u}:{p}@{host}:{port}/?authSource={a}&directConnection=true"


def normalize_mongo_uri(uri: str) -> str:
    if not uri:
        return uri
    try:
        parsed = urlparse(uri)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        host = (parsed.hostname or "").lower()
        if host in ("127.0.0.1", "localhost") and "directConnection" not in qs:
            qs["directConnection"] = ["true"]
            parsed = parsed._replace(query=urlencode(qs, doseq=True))
            return urlunparse(parsed)
        return uri
    except Exception:
        return uri


def connect_mongo(*, mongo_uri: str, server_selection_timeout_ms: int, connect_timeout_ms: int):
    if MongoClient is None:
        return None
    return MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=server_selection_timeout_ms,
        connectTimeoutMS=connect_timeout_ms,
    )


def _extract_oplog_delta_via_mongosh(
    *,
    conn_state,
    docker_container: str,
    mongo_user: str,
    mongo_password: str,
    mongo_auth_source: str,
    out_path: str,
) -> str | None:
    started = time.perf_counter()
    print(f"mongo      <info>   Falling back to docker+mongosh (container={docker_container})")

    docker_path = shutil.which("docker")
    if not docker_path:
        print("mongo      <info>   docker not found on PATH, cannot extract oplog")
        return None

    # Read last stored ts from state DB.
    last_ts_raw = get_metadata(conn_state, "mongo_last_ts")
    t_val = 0
    i_val = 0
    if last_ts_raw:
        try:
            t_str, i_str = last_ts_raw.split(":")
            t_val = int(t_str)
            i_val = int(i_str)
        except Exception:
            t_val = 0
            i_val = 0

    if last_ts_raw:
        print(f"mongo      <info>   Last stored ts: {last_ts_raw}")
    else:
        print("mongo      <info>   Last stored ts: <none> (starting from 0:0)")

    # Connect *inside* the container via localhost.
    mongo_uri = build_default_mongo_uri(
        host="localhost",
        port=27017,
        user=mongo_user,
        password=mongo_password,
        auth_source=mongo_auth_source,
    )

    # Stream EJSON lines from mongosh and wrap them into a JSON array file.
    # mongosh prints each doc on a single line; we ignore any non-doc lines.
    js = (
        "const last = Timestamp(" + str(t_val) + "," + str(i_val) + ");"
        "const coll = db.getSiblingDB('local').getCollection('oplog.rs');"
        "let latest = last; let n = 0;"
        "const cur = coll.find({ts: {$gt: last}}).sort({$natural: 1});"
        "while (cur.hasNext()) { const d = cur.next(); latest = d.ts; print(EJSON.stringify(d)); n++; }"
        "print('__LATEST_TS__=' + latest.t + ':' + latest.i + ':' + n);"
    )

    cmd = [
        docker_path,
        "exec",
        docker_container,
        "mongosh",
        mongo_uri,
        "--quiet",
        "--eval",
        js,
    ]

    tmp_path = out_path + ".tmp"
    written = 0
    latest_ts_raw = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception as e:
        print(f"mongo      <info>   docker exec failed: {e} (skipping Mongo for this cycle)")
        return None

    try:
        with open(tmp_path, "w", encoding="utf-8") as out:
            out.write("[\n")
            first = True

            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("__LATEST_TS__="):
                    latest_ts_raw = line.split("=", 1)[1]
                    continue
                # Accept only object-ish EJSON lines.
                if not line.startswith("{"):
                    continue
                if not first:
                    out.write(",\n")
                else:
                    first = False
                out.write(line)
                written += 1

            out.write("\n]\n")

        rc = proc.wait(timeout=30)
        if rc != 0:
            raise RuntimeError(f"mongosh exited non-zero (code={rc})")

        os.replace(tmp_path, out_path)
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        print(f"mongo      <info>   Oplog extraction failed: {e} (skipping Mongo for this cycle)")
        return None

    # Update stored ts only if we actually wrote docs.
    if latest_ts_raw and written > 0:
        # latest_ts_raw format: t:i:n
        try:
            t_str, i_str, n_str = latest_ts_raw.split(":")
            if int(n_str) > 0:
                set_metadata(conn_state, "mongo_last_ts", f"{int(t_str)}:{int(i_str)}")
                print(f"mongo      <info>   Updated last ts -> {int(t_str)}:{int(i_str)}")
        except Exception:
            pass

    try:
        out_size = int(os.path.getsize(out_path))
    except Exception:
        out_size = 0

    elapsed = time.perf_counter() - started
    print(
        f"[MONGO] Oplog summary: entries={written}, out={out_path} ({out_size} bytes), elapsed={elapsed:.2f}s"
    )
    return out_path


def extract_oplog_delta(*, conn_state, mongo_client, out_path: str) -> str | None:
    # NOTE: This function writes an uncompressed JSON array to `out_path`.
    # Artifact-level compression to `oplog_delta.json.z` is handled by the cycle runner
    # (see utilities/backup/lib/runner.py) when `MONGO_COMPRESS=1` (default).
    started = time.perf_counter()
    print("mongo      <info>   Starting oplog extraction")
    if MongoClient is None or mongo_client is None:
        docker_container = os.environ.get("MONGO_DOCKER_CONTAINER", "mongodb_live")
        user = os.environ.get("MONGO_USER", "mongodb")
        password = os.environ.get("MONGO_PASSWORD", "password")
        auth_source = os.environ.get("MONGO_AUTH_SOURCE", "admin")
        return _extract_oplog_delta_via_mongosh(
            conn_state=conn_state,
            docker_container=docker_container,
            mongo_user=user,
            mongo_password=password,
            mongo_auth_source=auth_source,
            out_path=out_path,
        )
    if BsonTimestamp is None or bson_dumps is None:
        print("mongo      <info>   bson helpers not available, skipping oplog extraction")
        return None

    local = mongo_client["local"]
    oplog = local["oplog.rs"]

    last_ts_raw = get_metadata(conn_state, "mongo_last_ts")
    if last_ts_raw:
        try:
            t_str, i_str = last_ts_raw.split(":")
            last_ts = BsonTimestamp(int(t_str), int(i_str))
        except Exception:
            last_ts = BsonTimestamp(0, 0)
    else:
        last_ts = BsonTimestamp(0, 0)

    if last_ts_raw:
        print(f"mongo      <info>   Last stored ts: {last_ts_raw}")
    else:
        print("mongo      <info>   Last stored ts: <none> (starting from 0:0)")

    tmp_path = out_path + ".tmp"
    written = 0
    latest_ts = last_ts
    try:
        cursor = oplog.find({"ts": {"$gt": last_ts}}, cursor_type=0).sort([("$natural", 1)])
        with open(tmp_path, "w", encoding="utf-8") as out:
            out.write("[\n")
            first = True
            for doc in cursor:
                if not first:
                    out.write(",\n")
                else:
                    first = False
                out.write(bson_dumps(doc))
                written += 1
                if "ts" in doc and isinstance(doc["ts"], BsonTimestamp):
                    latest_ts = doc["ts"]
            out.write("\n]\n")
        os.replace(tmp_path, out_path)
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        print(f"mongo      <info>   Oplog extraction failed: {e} (skipping Mongo for this cycle)")
        return None

    try:
        out_size = int(os.path.getsize(out_path))
    except Exception:
        out_size = 0

    if latest_ts is not None and written > 0:
        set_metadata(conn_state, "mongo_last_ts", f"{latest_ts.time}:{latest_ts.inc}")
        print(f"mongo      <info>   Updated last ts -> {latest_ts.time}:{latest_ts.inc}")
    elapsed = time.perf_counter() - started
    print(
        f"[MONGO] Oplog summary: entries={written}, out={out_path} ({out_size} bytes), elapsed={elapsed:.2f}s"
    )
    return out_path


__all__ = [
    "MongoClient",
    "build_default_mongo_uri",
    "normalize_mongo_uri",
    "connect_mongo",
    "extract_oplog_delta",
]
