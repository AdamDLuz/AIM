"""Beacon encode/decode and the local-network check."""

from __future__ import annotations

import socket
import unittest

from tests.support import ROOT  # noqa: F401

from intravo_messenger.discovery import decode_beacon, encode_beacon
from intravo_messenger.netutil import broadcast_targets, is_lan_address


class DiscoveryTests(unittest.TestCase):
    def test_beacon_roundtrip(self) -> None:
        payload = {
            "v": 1,
            "node_id": "abc",
            "name": "adam-win",
            "os": "windows",
            "port": 4777,
            "github_root": r"C:\Users\AdamLuz\Documents\GitHub",
            "agents": ["claude", "grok", "codex"],
            "exec_enabled": True,
        }
        raw = encode_beacon(payload)
        self.assertTrue(raw.startswith(b"IVM1"))
        self.assertNotIn(b"secret", raw)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2)
        port = sock.getsockname()[1]
        try:
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(raw, ("127.0.0.1", port))
                data, _addr = sock.recvfrom(2048)
            finally:
                sender.close()
        finally:
            sock.close()
        decoded = decode_beacon(data)
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded["name"], "adam-win")
        self.assertEqual(decoded["port"], 4777)
        self.assertNotIn("secret", decoded)

    def test_rejects_junk(self) -> None:
        self.assertIsNone(decode_beacon(b"NOPE"))
        self.assertIsNone(decode_beacon(b"IVM1{"))
        self.assertIsNone(decode_beacon(encode_beacon({"v": 1, "node_id": "x", "name": "n"})[:4] + b"{}"))

    def test_lan_addresses(self) -> None:
        self.assertFalse(is_lan_address("8.8.8.8"))
        self.assertFalse(is_lan_address("1.1.1.1"))
        self.assertTrue(is_lan_address("10.1.2.3"))
        self.assertTrue(is_lan_address("192.168.1.9"))
        self.assertTrue(is_lan_address("172.16.0.1"))
        self.assertTrue(is_lan_address("127.0.0.1"))
        self.assertTrue(is_lan_address("fe80::1"))
        self.assertTrue(is_lan_address("fd00::1"))
        self.assertFalse(is_lan_address("not-an-ip"))

    def test_broadcast_targets(self) -> None:
        targets = broadcast_targets(["192.168.1.20", "10.0.0.5"])
        self.assertEqual(targets[0], "255.255.255.255")
        self.assertIn("192.168.1.255", targets)
        self.assertIn("10.0.0.255", targets)


if __name__ == "__main__":
    unittest.main()
