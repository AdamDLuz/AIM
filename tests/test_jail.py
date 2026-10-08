"""Path jail: stay inside the GitHub folder, including Windows device names."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT  # noqa: F401  (puts src on sys.path)

from aim.jail import PathJailError, is_inside, resolve_inside


class JailTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "GitHub"
        (self.root / "Demo").mkdir(parents=True)
        self.sibling = Path(self._tmp.name) / "GitHub-evil"
        self.sibling.mkdir()
        (self.sibling / "secret.txt").write_text("no", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_relative_file_stays_inside(self) -> None:
        path = resolve_inside(self.root, "Demo/hello.txt")
        self.assertTrue(is_inside(path, self.root))

    def test_parent_escape(self) -> None:
        with self.assertRaises(PathJailError):
            resolve_inside(self.root, "..")
        with self.assertRaises(PathJailError):
            resolve_inside(self.root, "Demo/../../GitHub-evil/secret.txt")

    def test_sibling_prefix_is_outside(self) -> None:
        with self.assertRaises(PathJailError):
            resolve_inside(self.root, "../GitHub-evil/secret.txt")
        self.assertFalse(is_inside(self.sibling / "secret.txt", self.root))

    def test_absolute_outside(self) -> None:
        outside = Path(self._tmp.name) / "elsewhere.txt"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(PathJailError):
            resolve_inside(self.root, outside)

    def test_reserved_device_names(self) -> None:
        for name in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT1", "COM1.txt", "nul.txt"):
            with self.assertRaises(PathJailError, msg=name):
                resolve_inside(self.root, name)

    def test_illegal_characters(self) -> None:
        with self.assertRaises(PathJailError):
            resolve_inside(self.root, "Demo/bad<name.txt")

    def test_case_difference_stays_inside(self) -> None:
        path = resolve_inside(self.root, "demo/hello.txt")
        self.assertTrue(is_inside(path, self.root))

    def test_empty_is_the_root(self) -> None:
        self.assertTrue(is_inside(resolve_inside(self.root, ""), self.root))
        self.assertTrue(is_inside(resolve_inside(self.root, "."), self.root))

    def test_symlink_escape(self) -> None:
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "Demo" / "leak"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink not permitted: {exc}")
        with self.assertRaises(PathJailError):
            resolve_inside(self.root, "Demo/leak")


if __name__ == "__main__":
    unittest.main()
