"""HTTP API. Every route except the version check requires the shared secret,
and every route refuses a client that is not on a local network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from aim.config import VERSION, Config, agent_name, home_dir, node_name_ok, os_label
from aim.jail import PathJailError, is_inside, resolve_inside
from aim.netutil import is_lan_address, normalize_ip
from aim.store import Store
from aim.tasks import TaskError, TaskRunner, _zip_tree

log = logging.getLogger("aim.http")

JSON_LIMIT = 1_000_000
BODY_CHARS = 64_000


def header_value(headers, name: str, default: str = "") -> str:
    """Read an X-AIM-* header, or the earlier X-IVM-* name."""
    value = headers.get(name)
    if value:
        return value
    if name.startswith("X-AIM-"):
        legacy = headers.get("X-IVM-" + name[len("X-AIM-") :])
        if legacy:
            return legacy
    return default


class App:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.tasks = TaskRunner(cfg, store)
        self.stop = threading.Event()


class AimHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, app: App):
        self.app = app
        super().__init__(server_address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError, OSError):
            return

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._responded = False
        try:
            self._dispatch("GET")
        except Exception as exc:  # noqa: BLE001
            log.exception("request failed")
            self._json(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        self._responded = False
        try:
            self._dispatch("POST")
        except Exception as exc:  # noqa: BLE001
            log.exception("request failed")
            self._json(500, {"ok": False, "error": str(exc)})

    def _dispatch(self, method: str) -> None:
        if not self._lan_ok():
            self._json(403, {"ok": False, "error": "refusing a client outside the local network"})
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        if path == "/v1/health":
            self._json(200, {"ok": True, "version": VERSION})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "bad or missing token"})
            return
        if method == "GET" and path == "/v1/identity":
            self._identity()
            return
        if method == "GET" and path == "/v1/repos":
            self._repos()
            return
        if method == "GET" and path == "/v1/inbox":
            self._inbox(query)
            return
        if method == "POST" and path == "/v1/stop":
            self._stop()
            return
        if method == "POST" and path == "/v1/inbox/ack":
            self._ack()
            return
        if method == "POST" and path == "/v1/messages":
            self._post_message()
            return
        if method == "POST" and path == "/v1/tasks":
            self._post_task()
            return
        if method == "GET" and path == "/v1/tasks":
            self._json(200, {"ok": True, "tasks": [_brief(item) for item in self.app.store.list_tasks()]})
            return
        if method == "GET" and path.startswith("/v1/tasks/"):
            self._get_task(path[len("/v1/tasks/") :])
            return
        if method == "POST" and path == "/v1/files":
            self._upload()
            return
        if method == "GET" and path.startswith("/v1/files/"):
            self._download(path[len("/v1/files/") :])
            return
        if method == "POST" and path == "/v1/export":
            self._export()
            return
        self._json(404, {"ok": False, "error": f"no such route: {method} {path}"})

    def _lan_ok(self) -> bool:
        if self.app.cfg.allow_public:
            return True
        return is_lan_address(normalize_ip(self.client_address[0]))

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        got = header[7:].strip().encode("utf-8")
        expected = self.app.cfg.secret.encode("utf-8")
        if len(got) != len(expected):
            return False
        return hmac.compare_digest(got, expected)

    def _identity(self) -> None:
        cfg = self.app.cfg
        self._json(
            200,
            {
                "ok": True,
                "node_id": cfg.node_id,
                "name": cfg.name,
                "os": os_label(),
                "github_root": cfg.github_root,
                "agents": list(cfg.agents),
                "exec_enabled": bool(cfg.exec_enabled),
                "version": VERSION,
                "http_port": cfg.http_port,
            },
        )

    def _repos(self) -> None:
        root = Path(self.app.cfg.github_root)
        names: list[str] = []
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.lower())
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    names.append(child.name)
            except OSError:
                continue
            if len(names) >= 500:
                break
        self._json(200, {"ok": True, "github_root": str(root), "repos": names})

    def _inbox(self, query: dict) -> None:
        agent = (query.get("agent") or [None])[0]
        if agent in ("*", "all"):
            agent = None
        elif agent:
            try:
                agent = agent_name(agent)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
        try:
            wait = int((query.get("wait") or ["0"])[0])
            limit = int((query.get("limit") or ["50"])[0])
        except ValueError:
            self._json(400, {"ok": False, "error": "wait and limit must be integers"})
            return
        wait = max(0, min(wait, 60))
        limit = max(1, min(limit, 200))
        deadline = time.monotonic() + wait
        while True:
            rows = self.app.store.list_messages(agent=agent, unread_only=True, limit=limit)
            if rows or time.monotonic() >= deadline:
                self._json(200, {"ok": True, "messages": rows})
                return
            if self.app.stop.wait(0.4):
                self._json(200, {"ok": True, "messages": rows})
                return

    def _ack(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        ids = payload.get("ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            self._json(400, {"ok": False, "error": "ids must be a list of strings"})
            return
        count = self.app.store.ack(ids[:200])
        self._json(200, {"ok": True, "acked": count})

    def _post_message(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            self._json(400, {"ok": False, "error": "body is required"})
            return
        if len(body) > BODY_CHARS:
            self._json(413, {"ok": False, "error": "message is too long"})
            return
        try:
            from_agent = agent_name(str(payload.get("from_agent") or "unknown"))
            to_agent = agent_name(str(payload.get("to_agent") or "*")) if payload.get("to_agent") not in (None, "", "*") else "*"
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        from_node = _node(payload.get("from_node"))
        kind = str(payload.get("kind") or "chat")
        if kind not in {"chat", "task_result", "task_notice", "file_notice"}:
            kind = "chat"
        extra = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        message_id = uuid.uuid4().hex
        self.app.store.add_message(
            message_id=message_id,
            from_node=from_node,
            from_agent=from_agent,
            to_agent=to_agent,
            kind=kind,
            body=body,
            payload=extra,
        )
        self._json(200, {"ok": True, "id": message_id})

    def _post_task(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        payload["reply_host"] = normalize_ip(self.client_address[0])
        try:
            port = int(payload.get("reply_port") or 0)
        except (TypeError, ValueError):
            port = 0
        payload["reply_port"] = port if 1 <= port <= 65535 else None
        try:
            task = self.app.tasks.submit(payload)
        except TaskError as exc:
            status = 403 if "disabled" in str(exc) else 429 if "queued" in str(exc) else 400
            self._json(status, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "task": task})

    def _get_task(self, task_id: str) -> None:
        if not _file_id(task_id):
            self._json(400, {"ok": False, "error": "bad task id"})
            return
        task = self.app.store.get_task(task_id)
        if not task:
            self._json(404, {"ok": False, "error": "no such task"})
            return
        self._json(200, {"ok": True, "task": task})

    def _upload(self) -> None:
        length = self._content_length(self.app.cfg.max_file_bytes)
        if length is None:
            return
        filename = Path(unquote(header_value(self.headers, "X-AIM-Filename", "file"))).name
        dest_rel = unquote(header_value(self.headers, "X-AIM-Dest")).strip()
        if not dest_rel:
            self._json(400, {"ok": False, "error": "X-AIM-Dest is required"})
            return
        try:
            from_agent = agent_name(unquote(header_value(self.headers, "X-AIM-From-Agent", "unknown")))
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        from_node = _node(unquote(header_value(self.headers, "X-AIM-From-Node", "unknown")))
        try:
            dest = resolve_inside(Path(self.app.cfg.github_root), dest_rel)
            _reject_home(dest, allow_blobs=False)
        except PathJailError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        tmp = home_dir() / "tmp" / f"upload-{uuid.uuid4().hex}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        remaining = length
        try:
            with tmp.open("wb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        self._json(400, {"ok": False, "error": "upload ended early"})
                        return
                    handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _move_into_place(tmp, dest)
        except OSError as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        file_id = uuid.uuid4().hex
        self.app.store.add_file(
            {
                "id": file_id,
                "from_node": from_node,
                "from_agent": from_agent,
                "filename": filename or dest.name,
                "stored_path": str(dest),
                "dest_path": str(dest),
                "size": length,
                "sha256": digest.hexdigest(),
            }
        )
        self.app.store.add_message(
            message_id=uuid.uuid4().hex,
            from_node=from_node,
            from_agent=from_agent,
            to_agent="*",
            kind="file_notice",
            body=f"file arrived at {dest_rel} ({length} bytes)",
            payload={"file_id": file_id, "path": dest_rel, "bytes": length},
        )
        self._json(
            200,
            {
                "ok": True,
                "id": file_id,
                "path": str(dest),
                "bytes": length,
                "sha256": digest.hexdigest(),
            },
        )

    def _download(self, file_id: str) -> None:
        if not _file_id(file_id):
            self._json(400, {"ok": False, "error": "bad file id"})
            return
        row = self.app.store.get_file(file_id)
        if not row:
            self._json(404, {"ok": False, "error": "no such file"})
            return
        try:
            path = _readable(Path(row["stored_path"]), self.app.cfg)
        except PathJailError as exc:
            self._json(403, {"ok": False, "error": str(exc)})
            return
        if not path.is_file():
            self._json(404, {"ok": False, "error": "file is no longer on disk"})
            return
        self._send_file(path, row["filename"], "file")

    def _export(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        rel = payload.get("path")
        if not isinstance(rel, str) or not rel.strip():
            self._json(400, {"ok": False, "error": "path is required"})
            return
        try:
            target = resolve_inside(Path(self.app.cfg.github_root), rel)
            _reject_home(target, allow_blobs=False)
        except PathJailError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        if not target.exists():
            self._json(404, {"ok": False, "error": f"path not found: {rel}"})
            return
        if target.is_file():
            self._send_file(target, target.name, "file")
            return
        if not target.is_dir():
            self._json(400, {"ok": False, "error": "path is not a file or directory"})
            return
        tmp = home_dir() / "tmp" / f"export-{uuid.uuid4().hex}.zip"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            _zip_tree(target, tmp, int(self.app.cfg.max_file_bytes))
            self._send_file(tmp, target.name + ".zip", "directory")
        except TaskError as exc:
            self._json(413, {"ok": False, "error": str(exc)})
        finally:
            tmp.unlink(missing_ok=True)

    def _send_file(self, path: Path, filename: str, kind: str) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        for prefix in ("X-AIM-", "X-IVM-"):
            self.send_header(prefix + "Filename", quote(filename))
            self.send_header(prefix + "Sha256", digest.hexdigest())
            self.send_header(prefix + "Kind", kind)
        self.send_header("Connection", "close")
        self._responded = True
        self.close_connection = True
        self.end_headers()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_json(self) -> dict | None:
        length = self._content_length(JSON_LIMIT)
        if length is None:
            return None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "invalid json"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"ok": False, "error": "json object required"})
            return None
        return data

    def _content_length(self, limit: int) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            self._json(411, {"ok": False, "error": "Content-Length required"})
            return None
        try:
            length = int(raw)
        except ValueError:
            self._json(400, {"ok": False, "error": "bad Content-Length"})
            return None
        if length < 0 or length > int(limit):
            self._json(413, {"ok": False, "error": "body too large"})
            return None
        return length

    def _json(self, status: int, payload: dict) -> None:
        if self._responded:
            return
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self._responded = True
        self.close_connection = True
        self.end_headers()
        self.wfile.write(raw)

    def _stop(self) -> None:
        ip = normalize_ip(self.client_address[0])
        if not ipaddress_is_loopback(ip):
            self._json(403, {"ok": False, "error": "stop is only accepted from this machine"})
            return
        self._json(200, {"ok": True, "stopping": True})
        threading.Thread(target=self.server.shutdown, name="aim-shutdown", daemon=True).start()


def _node(value: object) -> str:
    text = str(value or "unknown").strip()
    if node_name_ok(text):
        return text
    return "unknown"


def _file_id(value: str) -> bool:
    return len(value) == 32 and all(ch in "0123456789abcdef" for ch in value)


def _brief(task: dict) -> dict:
    item = dict(task)
    item.pop("output", None)
    return item


def _reject_home(path: Path, *, allow_blobs: bool) -> None:
    home = home_dir().resolve()
    resolved = path.resolve()
    if allow_blobs and is_inside(resolved, home / "blobs"):
        return
    if is_inside(resolved, home):
        raise PathJailError("path is inside the messenger home directory")


def _readable(path: Path, cfg: Config) -> Path:
    resolved = path.resolve()
    home = home_dir()
    if is_inside(resolved, home / "blobs"):
        return resolved
    github_root = Path(cfg.github_root)
    if is_inside(resolved, github_root) and not is_inside(resolved, home):
        return resolved
    raise PathJailError("file is not in an allowed folder")


def _move_into_place(src: Path, dest: Path) -> None:
    try:
        os.replace(src, dest)
    except OSError:
        shutil.copy2(src, dest)
        src.unlink(missing_ok=True)


def ipaddress_is_loopback(ip: str) -> bool:
    return ip in {"127.0.0.1", "::1"}
