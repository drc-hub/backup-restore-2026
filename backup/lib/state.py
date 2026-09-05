import sqlite3


def open_state_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT)")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chunk_hashes(
            file_path TEXT,
            chunk_index INTEGER,
            sha256 TEXT,
            PRIMARY KEY(file_path, chunk_index)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS file_state(
            file_path TEXT PRIMARY KEY,
            mtime INTEGER,
            size INTEGER
        )
        """
    )
    conn.commit()
    return conn


def get_metadata(conn: sqlite3.Connection, key: str, default=None):
    cur = conn.cursor()
    cur.execute("SELECT value FROM metadata WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else default


def set_metadata(conn: sqlite3.Connection, key: str, value) -> None:
    cur = conn.cursor()
    cur.execute("REPLACE INTO metadata(key,value) VALUES(?,?)", (key, str(value)))
    conn.commit()


def get_file_state(conn: sqlite3.Connection, relpath: str):
    cur = conn.cursor()
    cur.execute("SELECT mtime,size FROM file_state WHERE file_path=?", (relpath,))
    return cur.fetchone()


def set_file_state(conn: sqlite3.Connection, relpath: str, mtime: int, size: int) -> None:
    cur = conn.cursor()
    cur.execute("REPLACE INTO file_state(file_path,mtime,size) VALUES(?,?,?)", (relpath, int(mtime), int(size)))
    conn.commit()


__all__ = [
    "open_state_db",
    "get_metadata",
    "set_metadata",
    "get_file_state",
    "set_file_state",
]
