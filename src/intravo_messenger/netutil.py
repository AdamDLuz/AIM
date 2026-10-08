"""Local addresses and the private-network check."""

from __future__ import annotations

import ipaddress
import socket


def normalize_ip(ip: str) -> str:
    if ip.startswith("::ffff:"):
        mapped = ip.rsplit(":", 1)[-1]
        try:
            ipaddress.ip_address(mapped)
        except ValueError:
            return ip
        return mapped
    return ip


def is_lan_address(ip: str) -> bool:
    """Loopback, RFC1918, link-local, and IPv6 unique-local."""
    try:
        addr = ipaddress.ip_address(normalize_ip(ip))
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def local_ipv4s() -> list[str]:
    found: list[str] = []

    def add(ip: str) -> None:
        if ip and not ip.startswith("127.") and ip not in found:
            found.append(ip)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    for probe in ("8.8.8.8", "192.168.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 9))
            add(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            sock.close()
    return found


def broadcast_targets(ipv4s: list[str] | None = None) -> list[str]:
    """Global broadcast plus a /24 broadcast for each local IPv4.

    Home networks are almost always /24. A machine on a wider subnet can
    still be pinned with `ivm peers add`.
    """
    targets = ["255.255.255.255"]
    for ip in ipv4s if ipv4s is not None else local_ipv4s():
        parts = ip.split(".")
        if len(parts) != 4:
            continue
        guess = ".".join(parts[:3] + ["255"])
        if guess not in targets:
            targets.append(guess)
    return targets
