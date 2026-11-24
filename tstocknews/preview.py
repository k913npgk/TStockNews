"""Same recommendations as production, isolated test records with no performance writes."""
from datetime import date, timedelta
from copy import deepcopy
from pathlib import Path
import re
import shutil

from . import STRATEGY_VERSION
from .analysis import recommendations
from .engine import MIN_HISTORY_SESSIONS
from .official import Client, SourceError, extra_closures, holiday_days, quote_rows, sync, today, universe_snapshot
from .report import render
from .storage import digest, read, write


def target_session(root, requested):
    current = date.fromisoformat(requested)
    if current > today():
        raise ValueError("Cannot preview a future date")
    client, calendars = Client(root), {}
    closures = extra_closures(root)
    # A missing quote is never used to infer a holiday.
    for _ in range(370):
        if current.year not in calendars:
            calendars[current.year] = holiday_days(client, current.year)
        day = current.isoformat()
        if current.weekday() < 5 and day not in calendars[current.year] and day not in closures:
            return day
        current -= timedelta(days=1)
    raise SourceError("No official trading session found within 370 days")


def enrich_daily_changes(data, target):
    """Add missing official display fields only to this run's copied quotes."""
    path = data / "days" / (target + ".json.gz")
    saved = read(path)
    if saved is None or all("price_change" in row and "price_change_pct" in row for row in saved["prices"]):
        return
    client, official = Client(data), {}
    for market, base, query in (
        ("twse", "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
         {"date": target.replace("-", ""), "type": "ALLBUT0999", "response": "json"}),
        ("tpex", "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
         {"date": target.replace("-", "/"), "response": "json"}),
    ):
        official.update({(row["market"], row["symbol"]): row
                         for row in quote_rows(client.json(base, **query), market, target)})
    for row in saved["prices"]:
        fresh = official.get((row["market"], row["symbol"]))
        if fresh is None or any(row.get(key) != fresh.get(key)
                                for key in ("open", "high", "low", "close", "volume_shares")):
            raise SourceError("Official target-day quotes conflict with the cached market snapshot")
        if "price_change" not in row or "price_change_pct" not in row:
            row.update(price_change=fresh["price_change"], price_change_pct=fresh["price_change_pct"])
    saved["source_audit"] = saved.get("source_audit", []) + client.audit
    write(path, saved)


def preview(source, runs, run_id, requested, *, runtime_financials=False, runtime_source_run_id=None):
    source, runs = Path(source), Path(runs)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("Invalid test run ID")
    if runtime_source_run_id and (not runtime_financials or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", runtime_source_run_id)):
        raise ValueError('A valid runtime source run ID requires runtime financials')
    if source.resolve() == runs.resolve() or source.resolve() in runs.resolve().parents or runs.resolve() in source.resolve().parents:
        raise ValueError("Test directory must be separate from production inputs")
    root = runs / run_id
    if root.resolve().parent != runs.resolve() or root.resolve() == source.resolve() or source.resolve() in root.resolve().parents or root.resolve() in source.resolve().parents:
        raise ValueError("Resolved test run must remain isolated from production")
    if root.exists() and any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("Symlinks are not allowed in test outputs")
    manifest_path = root / "manifest.json"
    existing = read(manifest_path)
    if existing:
        if existing["requested_date"] != requested:
            raise ValueError("A test run ID cannot be reused with a different date")
        if existing.get("runtime_financials", False) != runtime_financials:
            raise ValueError("A test run ID cannot change its financial source mode")
        if existing.get('runtime_source_run_id') != runtime_source_run_id:
            raise ValueError('A test run ID cannot change its runtime source run')
        report = root / "reports" / (existing["target_date"] + ".md")
        if not report.exists() or digest(report.read_text(encoding="utf-8")) != existing["report_hash"]:
            raise ValueError("Test report missing or changed; refusing to revise a prepared delivery")
        return existing
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    closures = source / "extra_closures.json"
    if closures.exists():
        shutil.copyfile(closures, data / closures.name)
    target = target_session(data, requested)
    available = sorted(p for p in (source / "days").glob("*.json.gz") if p.name[:10] <= target)
    # Only immutable market inputs; never copy recommendations, performance or production delivery.
    for path in available[-MIN_HISTORY_SESSIONS:]:
        destination = data / "days" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    start = (date.fromisoformat(available[-1].name[:10]) + timedelta(days=1)).isoformat() if available else target
    if start <= target:
        sync(data, start, target)
    enrich_daily_changes(data, target)
    sessions, prices, institutions = [], [], []
    for path in sorted((data / "days").glob("*.json.gz")):
        value = read(path)
        if value["date"] <= target:
            sessions.append(value["date"])
            prices.extend(row for row in value["prices"] if row["date"] <= target)
            institutions.extend(row for row in value["institutional"] if row["date"] <= target)
    if target not in sessions:
        raise SourceError("Target session quotes are missing")
    calendar_client = Client(data)
    calendars, closures = {}, extra_closures(data)
    current = date.fromisoformat(sessions[0])
    while current <= date.fromisoformat(target):
        if current.year not in calendars:
            calendars[current.year] = holiday_days(calendar_client, current.year)
        ds = current.isoformat()
        if current.weekday() < 5 and ds not in calendars[current.year] and ds not in closures and ds not in sessions:
            raise SourceError(f"Missing expected warm-up session {ds}; resume bootstrap first")
        current += timedelta(days=1)
    # Prefer the archived target-day ordinary-share universe; otherwise observe today's universe.
    universe_doc = None
    for folder in ("universe", "financials"):
        if universe_doc is not None:
            break
        for path in sorted((source / folder).glob("*.json.gz"), reverse=True):
            if path.name[:10] > target:
                continue
            value = read(path)
            if value and value.get("universe") and value.get("observed_date", path.name[:10]) <= target:
                if value.get("observed_date", path.name[:10]) != path.name[:10]:
                    raise SourceError("Universe snapshot filename/observation date mismatch")
                universe_doc = {"observed_date": value.get("observed_date", path.name[:10]),
                                "universe": value["universe"]}
                break
    if universe_doc is None:
        observed_today = today().isoformat()
        universe_doc = universe_snapshot(Client(data), observed_today)
    observed = universe_doc.get("observed_date", target)
    upcoming = date.fromisoformat(target) + timedelta(days=1)
    for _ in range(370):
        if upcoming.year not in calendars:
            calendars[upcoming.year] = holiday_days(calendar_client, upcoming.year)
        next_day = upcoming.isoformat()
        if upcoming.weekday() < 5 and next_day not in calendars[upcoming.year] and next_day not in closures:
            break
        upcoming += timedelta(days=1)
    else:
        raise SourceError("Next trading session not found")
    warnings = []
    if len(sessions) < MIN_HISTORY_SESSIONS:
        warnings.append("WARMUP_REQUIRED")
    if observed > today().isoformat() or not universe_doc.get("universe"):
        raise SourceError("Universe snapshot contains unavailable or future observations")
    result = None
    if not warnings:
        write(data / "universe" / (observed + ".json.gz"), universe_doc)
        result = recommendations(target, sessions, prices, institutions, universe_doc, test_run_id=run_id)
        write(data / "recommendations" / (target + ".json"), result)
        write(root / "screen.json", result)
    header = ["【測試報告｜不計入績效】", f"執行指定日：{requested}",
              f"行情／法人截至：{target}", f"普通股清單取得日：{observed}",
              f"下一交易日：{next_day}", ""]
    if result is not None:
        body = render(target, result, [], STRATEGY_VERSION, include_performance=False)
    else:
        target_prices = [row for row in prices if row["date"] == target]
        body = ("資料不足，尚無法完成篩選。\n"
                "符合檔數未知，不代表沒有股票符合條件。\n"
                f"已準備 {len(sessions)}／{MIN_HISTORY_SESSIONS} 個交易日資料。\n"
                f"上市行情：{sum(r['market'] == 'twse' for r in target_prices)} 檔\n"
                f"上櫃行情：{sum(r['market'] == 'tpex' for r in target_prices)} 檔\n")
    text = "\n".join(header) + body
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / (target + ".md")).write_text(text, encoding="utf-8")
    manifest = {"run_id": run_id, "requested_date": requested, "target_date": target,
                "test_only": True, "excluded_from_performance": True,
                "status": "TEST_REPORT_READY", "analysis_status": "COMPLETE" if result is not None else "INCOMPLETE",
                "warnings": warnings, "selected": len(result["candidates"]) if result is not None else None,
                "universe_observed_date": observed,
                "runtime_financials": runtime_financials,
                "runtime_source_run_id": runtime_source_run_id,
                "next_trading_day": next_day, "warmup_sessions": len(sessions),
                "universe_coverage": {market: sum(r["market"] == market for r in universe_doc["universe"])
                                      for market in ("twse", "tpex")},
                "recommendations": str(data / "recommendations" / (target + ".json")) if result else None,
                "report_hash": digest(text), "input_hash": digest({"prices": prices, "institutions": institutions, "universe": universe_doc})}
    write(manifest_path, manifest)
    return manifest
