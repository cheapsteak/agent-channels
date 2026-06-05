# tests/test_bridge.py
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bridge_helpers import BridgeTestCase, stub_slack  # noqa: E402
from agent_channels import bridge  # noqa: E402


class ConfigTests(BridgeTestCase):
    def test_add_get_remove_roundtrip(self):
        self.assertIsNone(bridge.get_bridge("help"))
        bridge.add_bridge("help", "C0123", label="Help channel")
        got = bridge.get_bridge("help")
        self.assertIsNotNone(got)
        assert got is not None  # narrow for type-checkers
        self.assertEqual(got["slack_channel"], "C0123")
        self.assertEqual(got["label"], "Help channel")
        self.assertTrue(bridge.remove_bridge("help"))
        self.assertIsNone(bridge.get_bridge("help"))
        self.assertFalse(bridge.remove_bridge("help"))

    def test_load_bridges_missing_returns_empty(self):
        self.assertEqual(bridge.load_bridges(), {})

    def test_load_bridges_corrupt_returns_empty(self):
        from agent_channels import ensure_dirs

        ensure_dirs()
        bridge.bridges_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(bridge.load_bridges(), {})


class RenderTests(BridgeTestCase):
    def test_render_text_format(self):
        record = {"seq": 7, "from": "auth-rewrite", "body": "stuck on JWT refresh"}
        text = bridge.render_text("help", record)
        self.assertEqual(text, "`auth-rewrite` in #help (#7)\nstuck on JWT refresh")

    def test_render_text_missing_fields(self):
        text = bridge.render_text("help", {})
        self.assertEqual(text, "`?` in #help (#?)\n")


if __name__ == "__main__":
    unittest.main()
