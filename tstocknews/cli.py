"""Daily pipeline, resumable manual bootstrap, and quota-aware delivery."""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys

from . import STRATEGY_VERSION
from .engine import screen, track, MIN_HISTORY_SESSIONS
from .line import prepare, send
from .official import Client, SourceError, holiday_days, sync, today, universe_and_financials
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
    closures = read(root / "extra_closures.json", {})
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
    args = parser.parse_args(argv)
    root, reports = Path(args.data_dir), Path(args.report_dir)
    root.mkdir(parents=True, exist_ok=True)
    day = getattr(args, "date", None) or today().isoformat()
    try:
        if args.command == "bootstrap":
            end = args.end or today().isoformat()
            if end > today().isoformat() or args.days < 1:
                raise ValueError("Invalid bootstrap range")
            start = (date.fromisoformat(end) - timedelta(days=args.days)).isoformat()
            def progress(collected):
                status = {"status": "BOOTSTRAPPING", "last_completed": collected, "start": start, "end": end}
                write(root / "status.json", status)
                print(json.dumps(status), flush=True)
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
        print(json.dumps(output, ensure_ascii=False))
        error_path = root / "error.json"
        if error_path.exists():
            error_path.unlink()
        return 0
    except Exception as error:
        # Source errors only include official URLs; LINE errors do not disclose token/group.
        write(root / "error.json", {"date": day, "type": type(error).__name__, "message": str(error)})
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
