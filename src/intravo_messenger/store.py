"""SQLite mailbox, task log, file index, and remembered peers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    from_node TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    payload TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_inbox
    ON messages (to_agent, read_at, created_at);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    from_node TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    reply_host TEXT,
    reply_port INTEGER,
    cwd TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    output TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    truncated INTEGER NOT NULL DEFAULT 0,
    result_files TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_at);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    from_node TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    dest_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peers (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    os_name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    github_root TEXT NOT NULL,
    agents TEXT NOT NULL,
    exec_enabled INTEGER NOT NULL DEFAULT 1,
    last_seen TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # A connection used as a context manager commits, but it does not
        # close. On Windows an unclosed handle keeps the database locked.
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_message(
        self,
        *,
        message_id: str,
        from_node: str,
        from_agent: str,
        to_agent: str,
        kind: str,
        body: str,
        payload: dict | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (id, created_at, from_node, from_agent, to_agent, kind, body, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    utc_now(),
                    from_node,
                    from_agent,
                    to_agent,
                    kind,
                    body,
                    json.dumps(payload or {}, separators=(",", ":")),
                ),
            )

    def list_messages(
        self,
        *,
        agent: str | None,
        unread_only: bool,
        limit: int,
    ) -> list[dict]:
        clauses = []
        args: list[Any] = []
        if agent:
            clauses.append("(to_agent = ? OR to_agent = '*' OR to_agent = 'all')")
            args.append(agent)
        if unread_only:
            clauses.append("read_at IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM messages{where} ORDER BY created_at ASC LIMIT ?"
        args.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_message(row) for row in rows]

    def ack(self, ids: Iterable[str]) -> int:
        id_list = [item for item in ids if item]
        if not id_list:
            return 0
        marks = ",".join("?" for _ in id_list)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE messages SET read_at = ? WHERE id IN ({marks}) AND read_at IS NULL",
                [utc_now(), *id_list],
            )
            return int(cur.rowcount)

    def create_task(self, row: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, created_at, from_node, from_agent, to_agent, reply_host, reply_port,
                    cwd, command, status, output, error, result_files
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '[]')
                """,
                (
                    row["id"],
                    row.get("created_at") or utc_now(),
                    row["from_node"],
                    row["from_agent"],
                    row["to_agent"],
                    row.get("reply_host"),
                    row.get("reply_port"),
                    row["cwd"],
                    row["command"],
                    row.get("status") or "queued",
                ),
            )

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "finished_at",
            "status",
            "exit_code",
            "output",
            "error",
            "truncated",
            "result_files",
        }
        keys = [key for key in fields if key in allowed]
        if not keys:
            return
        assignments = ", ".join(f"{key} = ?" for key in keys)
        values = []
        for key in keys:
            value = fields[key]
            if key == "result_files" and not isinstance(value, str):
                value = json.dumps(value, separators=(",", ":"))
            values.append(value)
        values.append(task_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)

    def get_task(self, task_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def list_tasks(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_task(row) for row in rows]

    def count_tasks(self, statuses: tuple[str, ...]) -> int:
        marks = ",".join("?" for _ in statuses)
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE status IN ({marks})",
                statuses,
            ).fetchone()
        return int(row["n"])

    def add_file(self, row: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO files (
                    id, created_at, from_node, from_agent, filename, stored_path,
                    dest_path, size, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row.get("created_at") or utc_now(),
                    row["from_node"],
                    row["from_agent"],
                    row["filename"],
                    row["stored_path"],
                    row["dest_path"],
                    int(row["size"]),
                    row["sha256"],
                ),
            )

    def get_file(self, file_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    def upsert_peer(self, peer: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO peers (
                    node_id, name, os_name, host, port, github_root, agents,
                    exec_enabled, last_seen, pinned
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    name = excluded.name,
                    os_name = excluded.os_name,
                    host = excluded.host,
                    port = excluded.port,
                    github_root = excluded.github_root,
                    agents = excluded.agents,
                    exec_enabled = excluded.exec_enabled,
                    last_seen = excluded.last_seen,
                    pinned = MAX(peers.pinned, excluded.pinned)
                """,
                (
                    peer["node_id"],
                    peer["name"],
                    peer["os_name"],
                    peer["host"],
                    int(peer["port"]),
                    peer.get("github_root") or "",
                    json.dumps(list(peer.get("agents") or []), separators=(",", ":")),
                    1 if peer.get("exec_enabled", True) else 0,
                    peer.get("last_seen") or utc_now(),
                    1 if peer.get("pinned") else 0,
                ),
            )

    def list_peers(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM peers ORDER BY name").fetchall()
        return [_peer(row) for row in rows]

    def delete_peer(self, *, node_id: str | None = None, host: str | None = None) -> int:
        with self._conn() as conn:
            if node_id:
                cur = conn.execute("DELETE FROM peers WHERE node_id = ?", (node_id,))
            elif host:
                cur = conn.execute("DELETE FROM peers WHERE host = ?", (host,))
            else:
                return 0
            return int(cur.rowcount)

    def get_peer_by_host(self, host: str, port: int | None = None) -> dict | None:
        with self._conn() as conn:
            if port is None:
                row = conn.execute(
                    "SELECT * FROM peers WHERE host = ? ORDER BY last_seen DESC LIMIT 1",
                    (host,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM peers WHERE host = ? AND port = ?",
                    (host, int(port)),
                ).fetchone()
        return _peer(row) if row else None


def _message(row: sqlite3.Row) -> dict:
    payload = {}
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "from_node": row["from_node"],
        "from_agent": row["from_agent"],
        "to_agent": row["to_agent"],
        "kind": row["kind"],
        "body": row["body"],
        "payload": payload,
        "read_at": row["read_at"],
    }


def _task(row: sqlite3.Row) -> dict:
    try:
        files = json.loads(row["result_files"] or "[]")
    except json.JSONDecodeError:
        files = []
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "from_node": row["from_node"],
        "from_agent": row["from_agent"],
        "to_agent": row["to_agent"],
        "reply_host": row["reply_host"],
        "reply_port": row["reply_port"],
        "cwd": row["cwd"],
        "command": row["command"],
        "status": row["status"],
        "exit_code": row["exit_code"],
        "output": row["output"],
        "error": row["error"],
        "truncated": bool(row["truncated"]),
        "result_files": files,
    }


def _peer(row: sqlite3.Row) -> dict:
    try:
        agents = json.loads(row["agents"] or "[]")
    except json.JSONDecodeError:
        agents = []
    return {
        "node_id": row["node_id"],
        "name": row["name"],
        "os": row["os_name"],
        "host": row["host"],
        "port": row["port"],
        "github_root": row["github_root"],
        "agents": agents,
        "exec_enabled": bool(row["exec_enabled"]),
        "last_seen": row["last_seen"],
        "pinned": bool(row["pinned"]),
    }
