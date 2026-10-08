"""Installer copies one skill text and does not print the key."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT, TEST_SECRET  # noqa: F401

from intravo_messenger.install import install_machine, install_skills


class InstallTests(unittest.TestCase):
    def test_skill_copies_match_canonical(self) -> None:
        canonical = ROOT / "skills" / "intravo-messenger" / "SKILL.md"
        text = canonical.read_text(encoding="utf-8")
        self.assertIn("name: intravo-messenger", text)
        self.assertIn("user-invocable: true", text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            dest = root / "skills" / "intravo-messenger"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text(text, encoding="utf-8")
            profile = Path(tmp) / "profile"
            written = install_skills(root, profile)
            self.assertEqual(len(written), 4)
            for path in written:
                self.assertEqual(path.read_text(encoding="utf-8"), text)
                self.assertNotIn("config.json", path.parts[-3:])

    def test_install_machine_keeps_secret_and_skips_user_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "ivm-home"
            github = Path(tmp) / "GitHub"
            github.mkdir()
            profile = Path(tmp) / "profile"
            old = os.environ.get("IVM_HOME")
            os.environ["IVM_HOME"] = str(home)
            try:
                result = install_machine(
                    name="unit-node",
                    github_root=str(github),
                    secret=TEST_SECRET,
                    autostart=False,
                    start=False,
                    edit_path=False,
                    user_home=profile,
                )
                again = install_machine(
                    name="unit-node",
                    github_root=str(github),
                    secret=None,
                    autostart=False,
                    start=False,
                    edit_path=False,
                    user_home=profile,
                )
            finally:
                if old is None:
                    os.environ.pop("IVM_HOME", None)
                else:
                    os.environ["IVM_HOME"] = old
            self.assertTrue(result["ok"])
            self.assertEqual(result["name"], "unit-node")
            self.assertFalse(result["started"])
            blob = "\n".join(result["notes"] + again["notes"])
            self.assertNotIn(TEST_SECRET, blob)
            self.assertIn("pair-export", blob)
            cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["secret"], TEST_SECRET)
            self.assertEqual(cfg["github_root"], str(github))
            launcher = home / "bin" / "ivm.cmd"
            self.assertTrue(launcher.is_file())
            self.assertNotIn(TEST_SECRET, launcher.read_text(encoding="utf-8"))
            skill = profile / ".grok" / "skills" / "intravo-messenger" / "SKILL.md"
            canonical = (ROOT / "skills" / "intravo-messenger" / "SKILL.md").read_text(encoding="utf-8")
            self.assertEqual(skill.read_text(encoding="utf-8"), canonical)


if __name__ == "__main__":
    unittest.main()
