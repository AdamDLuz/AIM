"""HTTP calls from the CLI and from a daemon delivering a task result."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import http.client


class IvmError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, host: str, port: int, secret: str, timeout: float = 30):
        self.host = host
        self.port = int(port)
        self.secret = secret
        self.timeout = timeout
        self.last_saved: Path | None = None

    def health(self) -> dict:
        return self.request_json("GET", "/v1/health", auth=False, timeout=3)

    def identity(self) -> dict:
        return self.request_json("GET", "/v1/identity", timeout=5)

    def repos(self) -> dict:
        return self.request_json("GET", "/v1/repos", timeout=10)

    def send_message(
        self,
        *,
        from_node: str,
        from_agent: str,
        to_agent: str,
        body: str,
        kind: str = "chat",
        payload: dict | None = None,
    ) -> dict:
        return self.request_json(
            "POST",
            "/v1/messages",
            {
                "from_node": from_node,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "body": body,
                "kind": kind,
                "payload": payload or {},
            },
        )

    def inbox(self, agent: str | None, wait: int = 0, limit: int = 50) -> dict:
        query = f"/v1/inbox?limit={int(limit)}&wait={int(wait)}"
        if agent:
            query += f"&agent={quote(agent)}"
        # The server long-polls, so the client has to outlast it.
        return self.request_json("GET", query, timeout=max(self.timeout, wait + 10))

    def ack(self, ids: list[str]) -> dict:
        return self.request_json("POST", "/v1/inbox/ack", {"ids": ids})

    def create_task(self, spec: dict) -> dict:
        return self.request_json("POST", "/v1/tasks", spec, timeout=30)

    def get_task(self, task_id: str) -> dict:
        return self.request_json("GET", f"/v1/tasks/{quote(task_id)}", timeout=15)

    def send_file(self, local: Path, dest_rel: str, from_node: str, from_agent: str) -> dict:
        size = local.stat().st_size
        timeout = max(60, min(3600, int(size / (256 * 1024)) + 30))
        with local.open("rb") as handle:
            status, headers, body = self._request(
                "POST",
                "/v1/files",
                body=handle,
                headers={
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream",
                    "X-IVM-Filename": quote(local.name),
                    "X-IVM-Dest": quote(dest_rel, safe="/"),
                    "X-IVM-From-Node": quote(from_node),
                    "X-IVM-From-Agent": quote(from_agent),
                },
                timeout=timeout,
            )
        return _json_or_error(status, body)

    def fetch_file(self, file_id: str, dest: Path) -> dict:
        status, headers, size = self._request(
            "GET",
            f"/v1/files/{quote(file_id)}",
            timeout=3600,
            dest=dest,
        )
        if status >= 400:
            raise IvmError(f"HTTP {status}", status)
        saved = self.last_saved or dest
        return {
            "ok": True,
            "path": str(saved),
            "bytes": size,
            "sha256": _header(headers, "X-IVM-Sha256"),
            "filename": unquote(_header(headers, "X-IVM-Filename") or saved.name),
        }

    def export_path(self, rel: str, dest: Path) -> dict:
        raw = json.dumps({"path": rel}).encode("utf-8")
        status, headers, size = self._request(
            "POST",
            "/v1/export",
            body=raw,
            headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
            timeout=3600,
            dest=dest,
        )
        if status >= 400:
            raise IvmError(f"HTTP {status}", status)
        saved = self.last_saved or dest
        return {
            "ok": True,
            "path": str(saved),
            "bytes": size,
            "sha256": _header(headers, "X-IVM-Sha256"),
            "filename": unquote(_header(headers, "X-IVM-Filename") or saved.name),
            "kind": _header(headers, "X-IVM-Kind") or "file",
        }

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        auth: bool = True,
        timeout: float | None = None,
    ) -> dict:
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        status, _headers, raw = self._request(
            method,
            path,
            body=body,
            headers=headers,
            auth=auth,
            timeout=timeout,
        )
        if not isinstance(raw, (bytes, bytearray)):
            raise IvmError("expected a json body")
        return _json_or_error(status, raw)

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict | None = None,
        auth: bool = True,
        timeout: float | None = None,
        dest: Path | None = None,
    ) -> tuple[int, dict[str, str], bytes | int]:
        outgoing = {"User-Agent": "intravo-messenger", "Connection": "close"}
        if auth:
            outgoing["Authorization"] = f"Bearer {self.secret}"
        if headers:
            outgoing.update(headers)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            conn.request(method, path, body=body, headers=outgoing)
            response = conn.getresponse()
            header_map = {key: value for key, value in response.getheaders()}
            if dest is not None and response.status < 400:
                final = dest
                if dest.exists() and dest.is_dir():
                    name = Path(unquote(_header(header_map, "X-IVM-Filename") or "download")).name
                    if not name or name in {".", ".."}:
                        name = "download"
                    final = dest / name
                final.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with final.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        size += len(chunk)
                self.last_saved = final
                return response.status, header_map, size
            payload = response.read()
            if dest is not None and response.status >= 400:
                _json_or_error(response.status, payload)
            return response.status, header_map, payload
        except IvmError:
            raise
        except (TimeoutError, OSError) as exc:
            raise IvmError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        finally:
            conn.close()


def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def _json_or_error(status: int, raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError as exc:
        raise IvmError(f"HTTP {status}: response was not json", status) from exc
    if not isinstance(data, dict):
        raise IvmError(f"HTTP {status}: response was not an object", status)
    if status >= 400 or data.get("ok") is False:
        raise IvmError(str(data.get("error") or f"HTTP {status}"), status)
    data.setdefault("ok", True)
    return data
