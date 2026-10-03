"""Daily pipeline, resumable manual bootstrap, and quota-aware delivery."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys

from . import STRATEGY_VERSION
from .engine import screen, track, MIN_HISTORY_SESSIONS
from .line import prepare, send
from .official import Client, SourceError, extra_closures, holiday_days, sync, today, universe_and_financials
from .report import render
from .storage import digest, read, write


def load_history(root):
    sessions, prices, institutional = [], [], []
    for path in sorted((root / "days").glob("*.json.gz")):
        daily = read(path)
        sessions.append(daily["date"])
        prices.extend(daily["prices"])
        institutional.extend(daily["institutional"])
    return sessions, prices, institutional


def daily(root, report_root, day):
    if day > today().isoformat():
        raise ValueError("Cannot run a future date")
    closures = extra_closures(root)
    if date.fromisoformat(day).weekday() >= 5 or day in closures:
        write(root / "status.json", {"date": day, "status": "NON_TRADING_DAY"})
        return {"date": day, "status": "NON_TRADING_DAY"}
    holidays = holiday_days(Client(root), date.fromisoformat(day).year)
    if day in holidays:
        write(root / "status.json", {"date": day, "status": "NON_TRADING_DAY"})
        return {"date": day, "status": "NON_TRADING_DAY"}
    existing = sorted((root / "days").glob("*.json.gz"))
    start = (date.fromisoformat(existing[-1].name[:10]) + timedelta(days=1)).isoformat() if existing else day
    sync(root, min(start, day), day)
    sessions, prices, institutions = load_history(root)
    if len([s for s in sessions if s <= day]) < MIN_HISTORY_SESSIONS:
        status = {"date": day, "status": "WARMUP_REQUIRED", "sessions": len(sessions),
                  "required": MIN_HISTORY_SESSIONS,
                  "command": "python -m tstocknews bootstrap --days 220"}
        write(root / "status.json", status)
        return status
    snapshot_path = root / "financials" / (day + ".json.gz")
    if not snapshot_path.exists():
        financial = universe_and_financials(Client(root), day)
        write(snapshot_path, financial)
    financial = read(snapshot_path)
    fundamentals = financial["fundamentals"]
    result_path = root / "recommendations" / (day + ".json")
    result = read(result_path)
    if result is None:
        result = screen(day, sessions, financial["universe"], prices, fundamentals, institutions)
        result["strategy_version"] = STRATEGY_VERSION
        result["data_hash"] = digest({"history": prices, "financial": financial})
        result["signals"] = [{"signal_id": f"{STRATEGY_VERSION}:{day}:{r['market']}:{r['symbol']}",
                              "date": day, "rank": i, "strategy_version": STRATEGY_VERSION,
                              **r} for i, r in enumerate(result["candidates"], 1)]
        write(result_path, result)
    signals = []
    for path in sorted((root / "recommendations").glob("*.json")):
        if path.stem <= day:
            signals.extend(read(path)["signals"])
    performance = track(signals, sessions, prices, day)
    write(root / "performance" / (day + ".json"), performance)
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / (day + ".md")
    # Immutable message content is essential for LINE's request retry semantics.
    if not report_path.exists():
        report_path.write_text(render(day, result, performance, STRATEGY_VERSION), encoding="utf-8")
    status = {"date": day, "status": "REPORT_READY", "eligible_count": result["eligible_count"],
              "selected": len(result["candidates"]), "report": str(report_path)}
    write(root / "status.json", status)
    return status


def push(root, report_root, day):
    report_path = report_root / (day + ".md")
    if not report_path.exists():
        return {"date": day, "status": "NO_REPORT"}
    text = report_path.read_text(encoding="utf-8")
    path = root / "delivery" / (day + ".json")
    delivery = read(path)
    payload_hash = digest(text)
    if delivery is None:
        delivery = prepare(payload_hash)
        write(path, delivery)  # workflow persists this BEFORE attempting the network send
    if delivery["payload_hash"] != payload_hash:
        raise ValueError("Report changed after preparing delivery; refused duplicate/revised push")
    delivery = send(text, delivery)
    write(path, delivery)
    return {"date": day, "status": delivery["status"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--report-dir", default="reports")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap", help="Manual, resumable OHLCV/法人 warm-up; may exceed 5 minutes")
    boot.add_argument("--days", type=int, default=220)
    boot.add_argument("--end", default=None)
    for name in ("daily", "prepare-send", "send"):
        p = sub.add_parser(name)
        p.add_argument("--date", default=None)
        if name == "send":
            p.add_argument("--require-sent", action="store_true", help="Fail Actions if delivery was blocked")
    test = sub.add_parser("test-report", help="Isolated Actions/LINE preview; never creates production signals")
    test.add_argument("--date", default=None)
    test.add_argument("--run-id", required=True)
    test.add_argument("--test-dir", default="test-runs")
    args = parser.parse_args(argv)
    root, reports = Path(args.data_dir), Path(args.report_dir)
    if args.command != "test-report":
        root.mkdir(parents=True, exist_ok=True)
    day = getattr(args, "date", None) or today().isoformat()
    bootstrap_status = None
    try:
        if args.command == "test-report":
            from .preview import preview
            output = preview(root, Path(args.test_dir), args.run_id, day)
        elif args.command == "bootstrap":
            end = args.end or today().isoformat()
            if end > today().isoformat() or args.days < 1:
                raise ValueError("Invalid bootstrap range")
            start = (date.fromisoformat(end) - timedelta(days=args.days)).isoformat()
            previous = read(root / "status.json", {})
            last_completed = previous.get("last_completed") if (
                previous.get("start") == start and previous.get("end") == end) else None
            bootstrap_status = {"status": "BOOTSTRAPPING", "last_completed": last_completed,
                                "start": start, "end": end}
            write(root / "status.json", bootstrap_status)
            def progress(collected):
                bootstrap_status["last_completed"] = collected
                write(root / "status.json", bootstrap_status)
                print(json.dumps(bootstrap_status), flush=True)
            sync(root, start, end, progress)
            sessions, _, _ = load_history(root)
            output = {"status": "BOOTSTRAP_COMPLETE", "sessions": len(sessions), "end": end}
            write(root / "status.json", output)
        elif args.command == "daily":
            output = daily(root, reports, day)
        elif args.command == "prepare-send":
            path = reports / (day + ".md")
            if not path.exists():
                output = {"status": "NO_REPORT"}
            else:
                delivery_path = root / "delivery" / (day + ".json")
                delivery = read(delivery_path)
                if delivery is None:
                    delivery = prepare(digest(path.read_text(encoding="utf-8")))
                    write(delivery_path, delivery)
                output = {"status": delivery["status"]}
        else:
            output = push(root, reports, day)
            if args.require_sent and output["status"] != "SENT":
                print(json.dumps(output, ensure_ascii=False))
                return 1
        print(json.dumps(output, ensure_ascii=False))
        error_path = (Path(args.test_dir) / args.run_id / "error.json") if args.command == "test-report" else root / "error.json"
        if error_path.exists():
            error_path.unlink()
        return 0
    except Exception as error:
        # Source errors only include official URLs; LINE errors do not disclose token/group.
        detail = {"date": day, "type": type(error).__name__, "message": str(error)}
        # A failed preview must not create/overwrite the production error/status files.
        if args.command != "test-report":
            write(root / "error.json", detail)
        if args.command == "bootstrap":
            write(root / "status.json", {**(bootstrap_status or {}),
                                         "status": "BOOTSTRAP_FAILED", "error": detail})
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
