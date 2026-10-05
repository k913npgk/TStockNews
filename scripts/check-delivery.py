"""Check same-day immutable SENT state before fetching any official source."""
import argparse
from datetime import date
from pathlib import Path

from tstocknews.storage import digest, read


def already_sent(root, reports, day):
    date.fromisoformat(day)
    delivery = read(Path(root) / "delivery" / (day + ".json"))
    if delivery and (delivery.get("test_only") or delivery.get("excluded_from_performance")):
        raise ValueError("Test delivery found in production state")
    if not delivery or delivery.get("status") != "SENT":
        return False
    report = Path(reports) / (day + ".md")
    if not report.exists() or delivery.get("payload_hash") != digest(report.read_text(encoding="utf-8")):
        raise ValueError("SENT report is missing or changed; refused automatic processing")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--report-dir", default="reports")
    args = parser.parse_args()
    print(str(already_sent(args.data_dir, args.report_dir, args.date)).lower())
