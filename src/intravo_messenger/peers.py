"""Resolve a peer name to the machine that should receive the work."""

from __future__ import annotations

from datetime import datetime, timezone

from intravo_messenger.store import Store, utc_now

ONLINE_SECONDS = 20


class PeerError(ValueError):
    pass


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_seconds(last_seen: str | None, now: datetime | None = None) -> float | None:
    seen = _parse_time(last_seen)
    if seen is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - seen).total_seconds())


def annotate(peer: dict, now: datetime | None = None) -> dict:
    item = dict(peer)
    age = age_seconds(item.get("last_seen"), now)
    item["age_seconds"] = None if age is None else round(age, 1)
    item["online"] = age is not None and age <= ONLINE_SECONDS
    item["self"] = False
    return item


def resolve_peer(peers: list[dict], query: str) -> dict:
    """Match a name, node id, host, or unique prefix.

    `host:port` is accepted. Matching is case-insensitive for names.
    """
    raw = query.strip()
    if not raw:
        raise PeerError("missing peer")
    host = raw
    port: int | None = None
    if raw.count(":") == 1 and raw.rsplit(":", 1)[1].isdigit():
        host, port_text = raw.rsplit(":", 1)
        port = int(port_text)
    needle = host.lower()

    def matches_host(peer: dict) -> bool:
        if peer["host"].lower() != host.lower():
            return False
        return port is None or int(peer["port"]) == port

    exact = [
        peer
        for peer in peers
        if peer["name"].lower() == needle
        or peer["node_id"].lower() == needle
        or matches_host(peer)
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        names = ", ".join(sorted(peer["name"] for peer in exact))
        raise PeerError(f"peer {query!r} is ambiguous: {names}")

    prefix = [peer for peer in peers if peer["name"].lower().startswith(needle)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        names = ", ".join(sorted(peer["name"] for peer in prefix))
        raise PeerError(f"peer {query!r} is ambiguous: {names}")
    raise PeerError(f"no peer named {query!r}. Run: ivm peers")


def learn_peer(store: Store, cfg, host: str, port: int, pinned: bool = False) -> dict:
    """Ask a machine who it is and remember the answer.

    `cfg` is a Config. Imported loosely so this module stays usable in unit
    tests that only have the store.
    """
    from intravo_messenger.client import Client

    client = Client(host, int(port), cfg.secret, timeout=5)
    identity = client.identity()
    node_id = str(identity.get("node_id") or "").strip()
    if not node_id:
        raise PeerError(f"{host}:{port} did not identify itself")
    name = str(identity.get("name") or host).strip()
    if not name:
        name = host
    store.upsert_peer(
        {
            "node_id": node_id[:80],
            "name": name[:64],
            "os_name": str(identity.get("os") or "unknown")[:32],
            "host": host,
            "port": int(port),
            "github_root": str(identity.get("github_root") or "")[:500],
            "agents": [str(item)[:32] for item in (identity.get("agents") or [])][:8],
            "exec_enabled": bool(identity.get("exec_enabled", True)),
            "last_seen": utc_now(),
            "pinned": pinned,
        }
    )
    placeholder = f"addr:{host}:{int(port)}"
    if node_id != placeholder:
        store.delete_peer(node_id=placeholder)
    for peer in store.list_peers():
        if peer["node_id"] == node_id:
            return peer
    raise PeerError(f"lost peer {name} after saving it")


def remember_address(store: Store, host: str, port: int, pinned: bool = False) -> None:
    """Keep a manually added address until a beacon fills in its identity."""
    existing = store.get_peer_by_host(host, port)
    if existing and existing.get("node_id") and not existing["node_id"].startswith("addr:"):
        if pinned:
            existing["pinned"] = True
            store.upsert_peer(
                {
                    "node_id": existing["node_id"],
                    "name": existing["name"],
                    "os_name": existing["os"],
                    "host": existing["host"],
                    "port": existing["port"],
                    "github_root": existing.get("github_root") or "",
                    "agents": existing.get("agents") or [],
                    "exec_enabled": existing.get("exec_enabled", True),
                    "last_seen": existing.get("last_seen"),
                    "pinned": True,
                }
            )
        return
    store.upsert_peer(
        {
            "node_id": f"addr:{host}:{port}",
            "name": host,
            "os_name": "unknown",
            "host": host,
            "port": port,
            "github_root": "",
            "agents": [],
            "exec_enabled": True,
            "last_seen": existing.get("last_seen") if existing else "1970-01-01T00:00:00+00:00",
            "pinned": pinned,
        }
    )
