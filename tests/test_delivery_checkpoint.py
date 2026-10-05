import importlib.util
from pathlib import Path
import tempfile
import unittest

from tstocknews.storage import digest, write

spec = importlib.util.spec_from_file_location("delivery_checkpoint", Path(__file__).parents[1] / "scripts" / "check-delivery.py")
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


class DeliveryCheckpointTests(unittest.TestCase):
    def test_test_receipt_cannot_skip_production_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            write(root / "delivery" / "2026-10-05.json", {"status": "SENT", "test_only": True})
            with self.assertRaisesRegex(ValueError, "Test delivery"):
                checkpoint.already_sent(root, Path(tmp) / "reports", "2026-10-05")

    def test_sent_skips_sources_only_with_same_day_immutable_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, reports = Path(tmp) / "data", Path(tmp) / "reports"
            reports.mkdir()
            day = "2026-10-05"
            text = "台股報告\n"
            (reports / (day + ".md")).write_text(text, encoding="utf-8")
            write(root / "delivery" / (day + ".json"), {"status": "SENT", "payload_hash": digest(text)})
            self.assertTrue(checkpoint.already_sent(root, reports, day))
            self.assertFalse(checkpoint.already_sent(root, reports, "2026-10-06"))
            (reports / (day + ".md")).write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                checkpoint.already_sent(root, reports, day)

    def test_pending_delivery_must_continue_and_missing_sent_report_must_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, reports = Path(tmp) / "data", Path(tmp) / "reports"
            path = root / "delivery" / "2026-10-05.json"
            write(path, {"status": "PENDING"})
            self.assertFalse(checkpoint.already_sent(root, reports, "2026-10-05"))
            write(path, {"status": "SENT", "payload_hash": "missing"})
            with self.assertRaises(ValueError):
                checkpoint.already_sent(root, reports, "2026-10-05")
