"""UDP beacons so machines on the same LAN find each other.

The beacon advertises the node name, OS, HTTP port, and GitHub folder.
It does not carry the shared secret. Commands still require that secret
over HTTP.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time

from aim.config import Config, os_label
from aim.netutil import broadcast_targets, local_ipv4s
from aim.store import Store, utc_now

log = logging.getLogger("aim.discovery")

MAGIC = b"AIM1"
# Earlier installs advertised this prefix. Accept it so a mixed LAN still discovers.
LEGACY_MAGIC = b"IVM1"
BEACON_INTERVAL_S = 3.0
PROBE_INTERVAL_S = 15.0


def encode_beacon(payload: dict) -> bytes:
    return _frame(MAGIC, payload)


def _frame(magic: bytes, payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > 1400:
        raise ValueError("beacon is larger than 1400 bytes")
    return magic + body


def decode_beacon(data: bytes) -> dict | None:
    magic = next((item for item in (MAGIC, LEGACY_MAGIC) if data.startswith(item)), None)
    if magic is None:
        return None
    try:
        obj = json.loads(data[len(magic) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != 1:
        return None
    if not obj.get("node_id") or not obj.get("name") or not obj.get("port"):
        return None
    return obj


def beacon_payload(cfg: Config) -> dict:
    return {
        "v": 1,
        "node_id": cfg.node_id,
        "name": cfg.name,
        "os": os_label(),
        "port": cfg.http_port,
        "github_root": cfg.github_root,
        "agents": list(cfg.agents),
        "exec_enabled": bool(cfg.exec_enabled),
    }


class Discovery:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.cfg.discovery_enabled:
            log.info("discovery disabled")
            return
        self._thread = threading.Thread(target=self._run, name="aim-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", int(self.cfg.discovery_port)))
            except OSError as exc:
                log.warning("discovery port %s is unavailable: %s", self.cfg.discovery_port, exc)
                return
            sock.settimeout(1.0)
            log.info("discovery listening on udp %s", self.cfg.discovery_port)
            next_send = 0.0
            next_probe = 0.0
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_send:
                    self._send(sock)
                    next_send = now + BEACON_INTERVAL_S
                if now >= next_probe:
                    self._probe_pinned()
                    next_probe = now + PROBE_INTERVAL_S
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        return
                    continue
                self._ingest(data, addr[0])
        finally:
            sock.close()

    def _send(self, sock: socket.socket) -> None:
        try:
            payload = beacon_payload(self.cfg)
            packets = (_frame(MAGIC, payload), _frame(LEGACY_MAGIC, payload))
        except ValueError as exc:
            log.warning("%s", exc)
            return
        for packet in packets:
            for target in broadcast_targets(local_ipv4s()):
                try:
                    sock.sendto(packet, (target, int(self.cfg.discovery_port)))
                except OSError:
                    continue

    def _ingest(self, data: bytes, source_ip: str) -> None:
        payload = decode_beacon(data)
        if payload is None:
            return
        if payload["node_id"] == self.cfg.node_id:
            return
        try:
            port = int(payload["port"])
        except (TypeError, ValueError):
            return
        if not 1 <= port <= 65535:
            return
        agents = payload.get("agents") if isinstance(payload.get("agents"), list) else []
        self.store.upsert_peer(
            {
                "node_id": str(payload["node_id"])[:80],
                "name": str(payload["name"])[:64],
                "os_name": str(payload.get("os") or "unknown")[:32],
                "host": source_ip,
                "port": port,
                "github_root": str(payload.get("github_root") or "")[:500],
                "agents": [str(item)[:32] for item in agents][:8],
                "exec_enabled": bool(payload.get("exec_enabled", True)),
                "last_seen": utc_now(),
                "pinned": False,
            }
        )

    def _probe_pinned(self) -> None:
        from aim.client import AimError
        from aim.peers import PeerError, learn_peer

        for peer in self.store.list_peers():
            if not peer.get("pinned"):
                continue
            try:
                learn_peer(self.store, self.cfg, peer["host"], int(peer["port"]), pinned=True)
            except (AimError, OSError, PeerError):
                continue
