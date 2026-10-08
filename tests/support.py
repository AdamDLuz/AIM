"""Shared fixtures. Tests set AIM_HOME and never touch the real profile."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TEST_SECRET = "unit-test-secret-not-a-network-key-0123456789"


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class LiveNode:
    """One messenger on 127.0.0.1 with discovery off."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.github = base / "GitHub"
        (self.github / "Demo").mkdir(parents=True)
        self._old = os.environ.get("AIM_HOME")
        os.environ["AIM_HOME"] = str(self.home)
        from aim.client import Client
        from aim.config import create_config, save_config
        from aim.server import App, AimHTTPServer
        from aim.store import Store

        port = free_port()
        cfg = create_config(
            name="test-node",
            github_root=str(self.github),
            secret=TEST_SECRET,
            http_port=port,
        )
        cfg.bind_host = "127.0.0.1"
        cfg.discovery_enabled = False
        save_config(cfg)
        self.cfg = cfg
        store = Store(self.home / "messenger.db")
        self.app = App(cfg, store)
        self.httpd = AimHTTPServer(("127.0.0.1", port), self.app)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="aim-test-http",
            daemon=True,
        )
        self.thread.start()
        self.client = Client("127.0.0.1", port, TEST_SECRET, timeout=10)
        deadline = time.time() + 5
        while True:
            try:
                self.client.health()
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
        finally:
            if self._old is None:
                os.environ.pop("AIM_HOME", None)
            else:
                os.environ["AIM_HOME"] = self._old
            self._tmp.cleanup()


def wait_task(client, task_id: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while True:
        task = client.get_task(task_id)["task"]
        if task.get("status") not in {"queued", "running"}:
            return task
        if time.time() > deadline:
            raise TimeoutError(task_id)
        time.sleep(0.05)
