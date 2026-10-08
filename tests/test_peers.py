"""Peer name resolution."""

from __future__ import annotations

import unittest

from tests.support import ROOT  # noqa: F401

from intravo_messenger.peers import PeerError, resolve_peer


def _peer(name: str, node_id: str, host: str, port: int = 4777) -> dict:
    return {"name": name, "node_id": node_id, "host": host, "port": port}


class PeerResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.peers = [
            _peer("adam-mac", "aaa", "10.0.0.2"),
            _peer("adam-win", "bbb", "10.0.0.3"),
            _peer("ubuntu-box", "ccc", "10.0.0.4", 4777),
        ]

    def test_exact_name_node_and_host(self) -> None:
        self.assertEqual(resolve_peer(self.peers, "adam-mac")["node_id"], "aaa")
        self.assertEqual(resolve_peer(self.peers, "BBB")["name"], "adam-win")
        self.assertEqual(resolve_peer(self.peers, "10.0.0.4")["name"], "ubuntu-box")
        self.assertEqual(resolve_peer(self.peers, "10.0.0.4:4777")["name"], "ubuntu-box")

    def test_unique_prefix(self) -> None:
        self.assertEqual(resolve_peer(self.peers, "ubuntu")["node_id"], "ccc")

    def test_ambiguous_prefix(self) -> None:
        with self.assertRaises(PeerError) as caught:
            resolve_peer(self.peers, "adam")
        self.assertIn("ambiguous", str(caught.exception))

    def test_missing(self) -> None:
        with self.assertRaises(PeerError) as caught:
            resolve_peer(self.peers, "nope")
        self.assertIn("no peer", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
