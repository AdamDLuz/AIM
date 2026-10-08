"""Installer copies one skill text and does not print the key."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT, TEST_SECRET  # noqa: F401

from aim.config import resolve_home
from aim.install import (
    _append_path_block,
    _symlink_local_bin,
    install_machine,
    install_skills,
    migrate_legacy_home,
)


class InstallTests(unittest.TestCase):
    def test_skill_copies_match_canonical(self) -> None:
        canonical = ROOT / "skills" / "aim" / "SKILL.md"
        text = canonical.read_text(encoding="utf-8")
        self.assertIn("name: aim", text)
        self.assertIn("user-invocable: true", text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            dest = root / "skills" / "aim"
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text(text, encoding="utf-8")
            profile = Path(tmp) / "profile"
            written = install_skills(root, profile)
            self.assertEqual(len(written), 4)
            for path in written:
                self.assertEqual(path.read_text(encoding="utf-8"), text)
                self.assertNotIn("config.json", path.parts[-3:])

    def test_unix_path_block_and_local_bin_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "messenger" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "aim").write_text("#!/bin/sh\n", encoding="utf-8")
            profile = root / "profile"
            rc = profile / ".zprofile"
            rc.parent.mkdir()
            rc.write_text("# existing\n", encoding="utf-8")
            first = _append_path_block(rc, bin_dir)
            second = _append_path_block(rc, bin_dir)
            self.assertIn(".zprofile", first)
            self.assertIn("already", second)
            text = rc.read_text(encoding="utf-8")
            self.assertEqual(text.count("# >>> aim >>>"), 1)
            self.assertIn(f'export PATH="{bin_dir}:$PATH"', text)
            note = _symlink_local_bin(bin_dir, profile)
            link = profile / ".local" / "bin" / "aim"
            self.assertTrue(link.is_symlink(), note)
            self.assertEqual(link.resolve(), (bin_dir / "aim").resolve())
            legacy = profile / ".local" / "bin" / "ivm"
            legacy.symlink_to(bin_dir / "ivm")
            again = _symlink_local_bin(bin_dir, profile)
            self.assertIn("already linked", again)
            self.assertFalse(legacy.exists())

    def test_path_block_replaces_legacy_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            rc = root / ".zprofile"
            rc.write_text(
                "# existing\n\n# >>> intravo-messenger >>>\n"
                'export PATH="/old:$PATH"\n# <<< intravo-messenger <<<\n',
                encoding="utf-8",
            )
            note = _append_path_block(rc, bin_dir)
            text = rc.read_text(encoding="utf-8")
            self.assertIn("added", note)
            self.assertNotIn("intravo-messenger", text)
            self.assertEqual(text.count("# >>> aim >>>"), 1)
            self.assertIn("# existing", text)

    def test_resolve_home_and_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".intravo-messenger"
            legacy.mkdir()
            (legacy / "config.json").write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_home(home, {}), legacy)
            self.assertEqual(resolve_home(home, {"IVM_HOME": str(home / "override")}), home / "override")
            self.assertEqual(
                resolve_home(home, {"AIM_HOME": str(home / "new"), "IVM_HOME": str(home / "old")}),
                home / "new",
            )
            (legacy / "daemon.lock").write_text("1", encoding="utf-8")
            self.assertIsNone(migrate_legacy_home(home, {}))
            self.assertTrue(legacy.is_dir())
            (legacy / "daemon.lock").unlink()
            moved = migrate_legacy_home(home, {})
            self.assertEqual(moved, str(home / ".aim"))
            self.assertTrue((home / ".aim" / "config.json").is_file())
            self.assertFalse(legacy.exists())
            current = home / ".aim"
            self.assertEqual(resolve_home(home, {}), current)

    def test_install_machine_keeps_secret_and_skips_user_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "aim-home"
            github = Path(tmp) / "GitHub"
            github.mkdir()
            profile = Path(tmp) / "profile"
            old = os.environ.get("AIM_HOME")
            os.environ["AIM_HOME"] = str(home)
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
                    os.environ.pop("AIM_HOME", None)
                else:
                    os.environ["AIM_HOME"] = old
            self.assertTrue(result["ok"])
            self.assertEqual(result["name"], "unit-node")
            self.assertFalse(result["started"])
            blob = "\n".join(result["notes"] + again["notes"])
            self.assertNotIn(TEST_SECRET, blob)
            self.assertIn("pair-export", blob)
            cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["secret"], TEST_SECRET)
            self.assertEqual(cfg["github_root"], str(github))
            launcher = home / "bin" / "aim.cmd"
            self.assertTrue(launcher.is_file())
            self.assertNotIn(TEST_SECRET, launcher.read_text(encoding="utf-8"))
            skill = profile / ".grok" / "skills" / "aim" / "SKILL.md"
            canonical = (ROOT / "skills" / "aim" / "SKILL.md").read_text(encoding="utf-8")
            self.assertEqual(skill.read_text(encoding="utf-8"), canonical)


if __name__ == "__main__":
    unittest.main()
