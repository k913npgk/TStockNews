"""Same recommendations as production, isolated test records with no performance writes."""
from datetime import date, timedelta
from pathlib import Path
import re
import shutil

from . import STRATEGY_VERSION
from .analysis import recommendations
from .engine import MIN_HISTORY_SESSIONS
from .official import Client, SourceError, extra_closures, holiday_days, sync, today, universe_and_financials
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


def preview(source, runs, run_id, requested):
    source, runs = Path(source), Path(runs)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("Invalid test run ID")
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
    # Prefer the archived target-day inputs; runtime financials are allowed for tests.
    financial = None
    for path in sorted((source / "financials").glob("*.json.gz"), reverse=True):
        if path.name[:10] > target:
            continue
        value = read(path)
        if value.get("observed_date", path.name[:10]) <= target:
            if value.get("observed_date", path.name[:10]) != path.name[:10]:
                raise SourceError("Financial snapshot filename/observation date mismatch")
            financial = value
            break
    if financial is None:
        financial = universe_and_financials(Client(data), today().isoformat())
    observed = financial.get("observed_date", target)
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
    if financial is None:
        warnings.append("HISTORICAL_FINANCIAL_SNAPSHOT_MISSING")
    elif observed > today().isoformat() or not financial.get("fundamentals") or any(not row.get("available_date") or row["available_date"] > observed for row in financial["fundamentals"]):
        raise SourceError("Financial snapshot contains unavailable or future observations")
    result = None
    if not warnings:
        write(data / "financials" / (observed + ".json.gz"), financial)
        result = recommendations(target, sessions, prices, institutions, financial, test_run_id=run_id)
        write(data / "recommendations" / (target + ".json"), result)
        write(root / "screen.json", result)
    header = [f"【測試報告｜不計入績效】台股篩選｜{target}",
              f"執行指定日：{requested}；目標交易日：{target}；測試編號：{run_id}",
              f"行情／法人截至：{target}；財報／營收快照取得日：{observed}。",
              f"推薦觀察的下一交易日：{next_day}。",
              "本次建立獨立測試推薦；不計入績效、不改寫正式分析。",
              *( ["使用執行時財報搭配目標日行情，供下一交易日檢視；不是目標日當時資訊的歷史重現。"] if observed > target else []), ""]
    if result is not None:
        body = render(target, result, [], STRATEGY_VERSION, include_performance=False)
    else:
        target_prices = [row for row in prices if row["date"] == target]
        body = ("資料不足，無法完成五項篩選；合格檔數未知，不能解釋為零檔合格。\n"
                f"截至目標日行情／法人暖機：{len(sessions)}／{MIN_HISTORY_SESSIONS} 個交易日。\n"
                f"目標日行情：上市 {sum(r['market'] == 'twse' for r in target_prices)} 檔；"
                f"上櫃 {sum(r['market'] == 'tpex' for r in target_prices)} 檔。\n"
                + ("缺少目標日以前已保存的財報／營收快照；未使用較晚取得的資料回填。\n" if financial is None else "")
                + "資料狀態：" + ", ".join(warnings) + "\n")
    text = "\n".join(header) + body
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / (target + ".md")).write_text(text, encoding="utf-8")
    manifest = {"run_id": run_id, "requested_date": requested, "target_date": target,
                "test_only": True, "excluded_from_performance": True,
                "status": "TEST_REPORT_READY", "analysis_status": "COMPLETE" if result is not None else "INCOMPLETE",
                "warnings": warnings, "selected": len(result["candidates"]) if result is not None else None,
                "financial_observed_date": observed, "late_financial_snapshot": observed > target,
                "next_trading_day": next_day, "warmup_sessions": len(sessions),
                "financial_coverage": {market: {"universe": sum(r["market"] == market for r in financial["universe"]),
                    "eps": sum(r["market"] == market and r.get("eps") is not None for r in financial["fundamentals"]),
                    "revenue": sum(r["market"] == market and r.get("revenue_yoy_3m") is not None for r in financial["fundamentals"])}
                    for market in ("twse", "tpex")},
                "recommendations": str(data / "recommendations" / (target + ".json")) if result else None,
                "report_hash": digest(text), "input_hash": digest({"prices": prices, "institutions": institutions, "financial": financial})}
    write(manifest_path, manifest)
    return manifest
