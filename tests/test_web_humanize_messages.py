import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web


class HumanizeMessagesTests(unittest.TestCase):
    def test_source_open_gap_is_humanized(self):
        raw = "Source has open ETHUSDT long qty=1.0000, but no local open position was found."
        text = web._humanize_copy_note(raw)
        self.assertIn("ETHUSDT", text)
        self.assertIn("1.0000", text)
        self.assertNotIn("Source has open", text)
        self.assertNotIn("no local open position was found", text)

    def test_close_ignored_is_humanized(self):
        raw = "[Live Binance Signal] source close ignored: no remaining local position (opened=0.00000000, closed=0.00000000)"
        text = web._humanize_copy_note(raw)
        self.assertIn("0.00000000", text)
        self.assertNotIn("source close ignored", text)
        self.assertNotIn("no remaining local position", text)

    def test_min_qty_is_humanized(self):
        raw = "quantity 0.0008595773100372736 is below minQty 0.001 (margin=0.39899)"
        text = web._humanize_copy_note(raw)
        self.assertIn("0.001", text)
        self.assertIn("0.39899", text)
        self.assertNotIn("is below minQty", text)


if __name__ == "__main__":
    unittest.main()
