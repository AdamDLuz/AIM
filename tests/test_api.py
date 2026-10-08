"""Live loopback API: mail, tasks, and files."""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.support import ROOT, LiveNode, wait_task  # noqa: F401

from intravo_messenger.cli import main
from intravo_messenger.client import Client, IvmError
from intravo_messenger.peers import learn_peer, remember_address
from intravo_messenger.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.node = LiveNode()

    def tearDown(self) -> None:
        self.node.stop()

    def test_inbox_is_per_agent(self) -> None:
        self.node.client.send_message(
            from_node="test-node",
            from_agent="claude",
            to_agent="grok",
            body="for-grok-only",
        )
        grok = self.node.client.inbox("grok")["messages"]
        claude = self.node.client.inbox("claude")["messages"]
        self.assertTrue(any(item["body"] == "for-grok-only" for item in grok))
        self.assertFalse(any(item["body"] == "for-grok-only" for item in claude))

    def test_echo_task_and_return_file(self) -> None:
        demo = self.node.github / "Demo"
        created = self.node.client.create_task(
            {
                "from_node": "test-node",
                "from_agent": "claude",
                "to_agent": "grok",
                "cwd": "Demo",
                "shell": "echo ivm-ok",
                "timeout_s": 30,
            }
        )
        task = wait_task(self.node.client, created["task"]["id"])
        self.assertEqual(task["exit_code"], 0)
        self.assertIn("ivm-ok", task["output"])

        writer = self.node.client.create_task(
            {
                "from_node": "test-node",
                "from_agent": "claude",
                "to_agent": "grok",
                "cwd": "Demo",
                "argv": [
                    sys.executable,
                    "-c",
                    "open('hello.txt','w',encoding='utf-8').write('hello')",
                ],
                "return_files": ["hello.txt"],
                "timeout_s": 30,
            }
        )
        done = wait_task(self.node.client, writer["task"]["id"])
        self.assertEqual(done["status"], "ok", done.get("error"))
        self.assertEqual(len(done["result_files"]), 1)
        file_id = done["result_files"][0]["id"]
        dest_dir = self.node.home / "fetched"
        dest_dir.mkdir()
        saved = self.node.client.fetch_file(file_id, dest_dir)
        text = Path(saved["path"]).read_text(encoding="utf-8")
        self.assertEqual(text, "hello")
        self.assertTrue((demo / "hello.txt").is_file())

    def test_send_file_and_reject_escape(self) -> None:
        local = self.node.home / "note.txt"
        local.write_text("hi", encoding="utf-8")
        result = self.node.client.send_file(local, "Demo/incoming/note.txt", "test-node", "claude")
        self.assertTrue(result["ok"])
        landed = self.node.github / "Demo" / "incoming" / "note.txt"
        self.assertEqual(landed.read_text(encoding="utf-8"), "hi")
        with self.assertRaises(IvmError) as caught:
            self.node.client.send_file(local, "../outside.txt", "test-node", "claude")
        self.assertEqual(caught.exception.status, 400)
        self.assertFalse((self.node.github.parent / "outside.txt").exists())

    def test_export_file_and_directory(self) -> None:
        demo = self.node.github / "Demo"
        (demo / "one.txt").write_text("one", encoding="utf-8")
        (demo / "sub").mkdir()
        (demo / "sub" / "two.txt").write_text("two", encoding="utf-8")
        file_out = self.node.home / "one.txt"
        exported = self.node.client.export_path("Demo/one.txt", file_out)
        self.assertEqual(Path(exported["path"]).read_text(encoding="utf-8"), "one")
        zip_out = self.node.home / "Demo.zip"
        packed = self.node.client.export_path("Demo", zip_out)
        self.assertEqual(packed["kind"], "directory")
        self.assertTrue(zipfile.is_zipfile(packed["path"]))
        names = zipfile.ZipFile(packed["path"]).namelist()
        self.assertTrue(any(name.endswith("one.txt") for name in names))
        self.assertTrue(any(name.endswith("two.txt") for name in names))

    def test_bad_token_and_bad_cwd(self) -> None:
        bad = Client("127.0.0.1", self.node.cfg.http_port, "x" * 40)
        with self.assertRaises(IvmError) as caught:
            bad.identity()
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(IvmError) as cwd:
            self.node.client.create_task(
                {"cwd": "..", "shell": "echo hi", "timeout_s": 5, "from_agent": "claude"}
            )
        self.assertEqual(cwd.exception.status, 400)

    def test_cli_whoami_and_send_hide_secret(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--home", str(self.node.home), "--json", "whoami"])
        self.assertEqual(code, 0, err.getvalue())
        text = out.getvalue()
        self.assertNotIn('"secret"', text)
        self.assertIn("test-node", text)
        out.seek(0)
        out.truncate()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                [
                    "--home",
                    str(self.node.home),
                    "--json",
                    "send",
                    "test-node",
                    "--from",
                    "claude",
                    "--to",
                    "grok",
                    "--message",
                    "from-cli",
                ]
            )
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertNotIn(self.node.cfg.secret, out.getvalue())
        inbox = self.node.client.inbox("grok")["messages"]
        self.assertTrue(any(item["body"] == "from-cli" for item in inbox))

    def test_learn_peer_replaces_placeholder(self) -> None:
        store = Store(self.node.home / "peers-other.db")
        remember_address(store, "127.0.0.1", self.node.cfg.http_port, pinned=True)
        peer = learn_peer(store, self.node.cfg, "127.0.0.1", self.node.cfg.http_port, pinned=True)
        self.assertEqual(peer["name"], "test-node")
        self.assertEqual(peer["github_root"], str(self.node.github))
        self.assertNotIn("secret", peer)
        ids = [item["node_id"] for item in store.list_peers()]
        self.assertNotIn(f"addr:127.0.0.1:{self.node.cfg.http_port}", ids)

    def test_directory_zip_skips_symlink_escape(self) -> None:
        outside = self.node.github.parent / "outside-secret.txt"
        outside.write_text("do-not-pack", encoding="utf-8")
        pack = self.node.github / "Demo" / "pack"
        pack.mkdir()
        (pack / "ok.txt").write_text("ok", encoding="utf-8")
        try:
            (pack / "leak").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink not permitted: {exc}")
        zip_out = self.node.home / "pack.zip"
        packed = self.node.client.export_path("Demo/pack", zip_out)
        archive = zipfile.ZipFile(packed["path"])
        names = archive.namelist()
        self.assertIn("ok.txt", names)
        blob = b"".join(archive.read(name) for name in names)
        self.assertNotIn(b"do-not-pack", blob)


if __name__ == "__main__":
    unittest.main()
