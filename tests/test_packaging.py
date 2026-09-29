#!/usr/bin/env python3
"""Repository level checks: the manifests a release depends on.

These run in CI without STM32CubeMX, Keil or Codex, so a broken manifest is
caught on a pull request rather than at install time.
"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "stm32cubemx-control"


class ManifestTest(unittest.TestCase):
    def test_plugin_manifest(self):
        data = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        for key in ("name", "version", "description", "author", "interface"):
            self.assertIn(key, data)
        self.assertEqual(data["name"], "stm32cubemx-control")
        parts = data["version"].split(".")
        self.assertEqual(len(parts), 3, "version must be semver")
        self.assertTrue(all(p.isdigit() for p in parts),
                        "version must be plain semver without a development suffix")
        for key in ("displayName", "shortDescription", "longDescription",
                    "developerName", "category"):
            self.assertIn(key, data["interface"])
        self.assertNotIn("mcpServers", data,
                         "the MCP server is registered by install.ps1, not shipped")

    def test_marketplace_manifest(self):
        data = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
        entries = [p for p in data["plugins"] if p.get("name") == "stm32cubemx-control"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["source"]["path"], "./plugins/stm32cubemx-control")
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")
        self.assertTrue(entry.get("category"))

    def test_skill_frontmatter(self):
        text = (PLUGIN / "skills" / "stm32cubemx-control" / "SKILL.md").read_text(
            encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        head = text.split("---")[1]
        for key in ("name:", "description:"):
            self.assertIn(key, head)

    def test_no_machine_specific_paths_ship(self):
        offenders = []
        for path in sorted(PLUGIN.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in (".py", ".json", ".md"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in ("E:/Codex", "E:\\Codex", "C:/Program Files/python",
                           ":\\Users\\"):
                if needle in text:
                    offenders.append("{} contains {}".format(
                        path.relative_to(ROOT), needle))
        self.assertEqual(offenders, [], "machine specific paths must not be shipped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
