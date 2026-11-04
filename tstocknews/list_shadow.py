"""Independent same-day comparison for the buffered growth/flow test strategy."""
import argparse
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from statistics import mean, median, pstdev
from tempfile import TemporaryFile
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile

from . import STRATEGY_VERSION
from .cli import push
from .engine import _valid_price
from .eps_archive import restore_archived_eps
from .eps_facts import extract_comparison
from .line import prepare
from .network import tls_context
from .official import today
from .report import daily_change_pct
from .storage import digest, read, write

VERSION = "test-growth-flow-top20-keep40-breadth40-55-v1"
EXCLUDED = frozenset(("1218", "1563", "3356", "3591", "3665", "6173"))
MEMBER = re.compile(r"-(cr|ir)-(\d{4})-(\d{4}Q[1-4])\.html$", re.I)
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
TEST_ID = "list-membership-v1"


def identity(row):
    return row["market"], row["symbol"]


def index(rows, key):
    indexed = {}
    for row in rows:
        value = key(row)
        if value in indexed and indexed[value] != row:
            raise ValueError("Conflicting official observations")
        indexed[value] = row
    return indexed


def market_features(snapshots, universe):
    """Use the research windows and the same NT$30m median turnover proxy."""
    prices, flow = defaultdict(dict), defaultdict(dict)
    dates = [snapshot["date"] for snapshot in snapshots]
    for snapshot in snapshots:
        for key, row in index(snapshot["prices"], identity).items():
            if _valid_price(row) and not row.get("corporate_action_unresolved"):
                prices[key][snapshot["date"]] = row
        if snapshot["date"] not in dates[-20:]:
            continue
        for key, row in index(snapshot["institutional"], identity).items():
            flow[key][snapshot["date"]] = row["net_buy_shares"]
    rows = []
    for key, company in index(universe, identity).items():
        if key[1] in EXCLUDED or len(prices[key]) != 121:
            continue
        bars = [prices[key][day] for day in dates]
        closes = [row["close"] for row in bars]
        volumes = [row["volume_shares"] for row in bars]
        if median(c * v for c, v in zip(closes[-20:], volumes[-20:])) < 30_000_000:
            continue
        average = mean(closes[-60:])
        row = dict(company, price_change_pct=bars[-1].get("price_change_pct"),
                   momentum=closes[-6]/closes[-66]-1, trend=closes[-1]/average-1,
                   trend_slope=average/mean(closes[-80:-20])-1,
                   return5_abs=abs(closes[-1]/closes[-6]-1),
                   vol20=pstdev(b/a-1 for a,b in zip(closes[-21:],closes[-20:])))
        for n in (5,20):
            values = [flow[key].get(day) for day in dates[-n:]]
            volume = sum(volumes[-n:])
            row[f"flow{n}"] = sum(values)/volume if volume and None not in values else None
        rows.append(row)
    return rows


def load_eps_period(period, symbols):
    """Parse one official bulk filing archive for a required fiscal period."""
    year, quarter = int(period[:4]), int(period[-1])
    compact = period.replace("-", "")
    url = "https://mopsov.twse.com.tw/server-java/FileDownLoad?" + urlencode({
        "step": 9, "functionName": "show_file2",
        "fileName": f"tifrs-{compact}.zip", "filePath": f"/ifrs/{year}/"})
    archive_hash = sha256()
    with TemporaryFile() as local:
        size = 0
        request = Request(url, headers={"User-Agent": "TStockNews/0.1"})
        with urlopen(request, timeout=45, context=tls_context()) as response:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("Official EPS archive exceeds size bound")
                local.write(block)
                archive_hash.update(block)
        local.seek(0)
        with ZipFile(local) as archive:
            members = defaultdict(list)
            for member in archive.infolist():
                match = MEMBER.search(member.filename)
                if match and match[2] in symbols and match[3].upper() == compact:
                    members[match[2]].append((match[1].lower(), member))
            results = {}
            for symbol, filings in members.items():
                scope = "cr" if any(kind == "cr" for kind, _ in filings) else "ir"
                comparable = set()
                for kind, member in filings:
                    if kind != scope:
                        continue
                    if member.file_size > 16 * 1024 * 1024:
                        raise ValueError("Official EPS member exceeds size bound")
                    value = extract_comparison(archive.read(member), symbol=symbol, year=year,
                                               quarter=quarter, member_name=member.filename)
                    if value["status"] == "KNOWN":
                        comparable.add((value["eps_ytd"], value["eps_prior_ytd"]))
                if len(comparable) == 1:
                    results[symbol] = comparable.pop()
    return results, dict(period=period, url=url, sha256=archive_hash.hexdigest(), size=size)


def financial_features(rows, financial, day, load_period=load_eps_period):
    funds = index(financial["fundamentals"], identity)
    needed = defaultdict(set)
    for row in rows:
        fund = funds.get(identity(row), {})
        period = fund.get("fiscal_period", "")
        if fund.get("available_date", "9999") <= day and re.fullmatch(r"\d{4}-Q[1-4]", period):
            needed[period].add(row["symbol"])
    if len(needed) > 4:
        raise ValueError("Too many distinct fiscal periods in test pool")
    comparisons, sources = {}, []
    for period, symbols in sorted(needed.items()):
        comparisons[period], source = load_period(period, symbols)
        sources.append(source)
    for row in rows:
        fund = funds.get(identity(row), {})
        if fund.get("available_date", "9999") > day:
            fund = {}
        current, prior = fund.get("revenue_yoy_3m"), fund.get("revenue_yoy_prev_3m")
        pair = comparisons.get(fund.get("fiscal_period"), {}).get(row["symbol"])
        # A filing and snapshot must agree on current cumulative EPS.
        row.update(revenue=current, acceleration=current-prior if None not in (current,prior) else None,
                   earnings=(pair[0]-pair[1])/max(abs(pair[1]), .1) if pair and pair[0]==fund.get("eps") else None)
    if rows and not any(row["earnings"] is not None for row in rows):
        raise ValueError("No verified EPS comparison for the test pool")
    return sources


def rank_rows(rows):
    blocks = (
        (.15, (("momentum",1), ("trend",1), ("trend_slope",1))),
        (.35, (("revenue",1), ("acceleration",1), ("earnings",1))),
        (.35, (("flow5",1), ("flow20",1))),
        (.15, (("vol20",-1), ("return5_abs",-1))),
    )
    scores = defaultdict(float)
    for weight, fields in blocks:
        values = defaultdict(list)
        for field, direction in fields:
            observed = sorted(row[field]*direction for row in rows if row[field] is not None)
            for row in rows:
                value = row[field]
                rank = 0.0
                if value is not None:
                    x = value * direction
                    rank = .5 if len(observed)==1 else (bisect_left(observed,x)+bisect_right(observed,x)-1)/2/(len(observed)-1)
                values[identity(row)].append(rank)
        for key, ranks in values.items():
            scores[key] += weight * (sum(ranks)/len(fields))
    return sorted(rows, key=lambda row: (-scores[identity(row)], row["symbol"], row["market"]))


def breadth_state(rows, previous):
    if not rows:
        raise ValueError("No eligible stocks for test breadth")
    breadth = sum(row["trend"] > 0 for row in rows) / len(rows)
    return breadth >= (.40 if previous.get("active") else .55), breadth


def decide(ranked, previous):
    active, breadth = breadth_state(ranked, previous)
    if not active:
        return active, breadth, []
    top = ranked[:40]
    prior = {identity(row) for row in previous.get("members", [])}
    kept = [row for row in top if identity(row) in prior][:20]
    entrants = [row for row in top if identity(row) not in prior]
    return active, breadth, kept + entrants[:20-len(kept)]


def next_session_rises(rows, quotes):
    observed = [quotes.get(identity(row)) for row in rows]
    valid = [row for row in observed if _valid_price(row)]
    return dict(up=sum(row["close"] > row["open"] for row in valid), known=len(valid), total=len(rows))


def official_reference(source, day):
    formal = read(source/"recommendations"/f"{day}.json", {})
    revisions = []
    for manifest_path in sorted((source/"corrections").glob("*/manifest.json")):
        manifest = read(manifest_path)
        entry = manifest.get("reports", {}).get(day)
        if entry:
            revision = read(manifest_path.parent/"recommendations"/f"{day}.json")
            if not revision or digest(revision) != entry.get("result_hash"):
                raise ValueError("Formal correction does not match its manifest")
            revisions.append(revision)
    if len(revisions) > 1:
        raise ValueError("Multiple corrections for the same official session")
    return revisions[0] if revisions else formal


def render(snapshot, previous):
    if snapshot["continuity"] != "CONTINUOUS":
        previous = {}
    prior = {identity(row):row for row in previous.get("members", [])}
    current = {identity(row):row for row in snapshot["members"]}
    name = lambda row: f"{row.get('name','')}（{row['symbol']}）"
    formal_label = "正式 v8 更正版" if snapshot.get("formal_corrected") else "正式 v8"
    eps_label = (f"測試 EPS 同期比較：{snapshot['eps_known']}／{snapshot['pool_size']} 檔"
                 if snapshot["eps_known"] is not None else "廣度觸發空榜，略過 EPS 排名。")
    lines = [f"🧪 測試中策略｜{snapshot['date']}", "成長＋資金計分｜前20入榜、前40留任",
             f"合格池廣度 {snapshot['breadth']:.1%}｜{'開啟' if snapshot['active'] else '空榜'}",
             f"同日{formal_label}：{len(snapshot['formal'])} 檔｜測試：{len(current)} 檔｜重疊 {len(set(current)&{identity(row) for row in snapshot['formal']})} 檔",
             eps_label, "",
             "測試榜單｜次交易日開盤模擬"]
    for key,row in current.items():
        lines.append(f"{'留榜' if key in prior else '入榜'} {name(row)} {daily_change_pct(row.get('price_change_pct'))}")
    if not current:
        lines.append("空榜")
    lines += ["", "落榜 " + ("、".join(name(prior[key]) for key in prior if key not in current) or "0 檔"),
              "正式榜單 " + ("、".join(name(row) for row in snapshot["formal"]) or "0 檔")]
    if snapshot["continuity"] == "CONTINUOUS":
        lines += ["", "前一交易日榜單｜今日開盤至收盤"]
        for label, result in snapshot["outcomes"].items():
            lines.append(f"{label}：{result['up']}／{result['known']} 檔上漲，價格完整 {result['known']}／{result['total']} 檔")
    else:
        lines += ["", "首次建榜，等待下一交易日比較。" if snapshot["continuity"] == "INITIAL"
                  else "前次測試紀錄與本日不連續，本日重新建榜。"]
    lines += ["", "2026年1–10月歷史探索（來回成本0.6%）",
              "測試版 +80.40%｜最大回撤 −11.36%｜落榜交易股價上漲 50.63%",
              "同版7–10月 −7.93%｜上漲 39.18%",
              "每日比較自測試啟用起保存。歷史數字採未還原股價及研究公告假設。"]
    return "\n".join(lines) + "\n"


def capture(source, root, day):
    if date.fromisoformat(day) != today():
        raise ValueError("Test comparison requires the current Taiwan session")
    formal = official_reference(source, day)
    if formal.get("strategy_version") != STRATEGY_VERSION or formal.get("as_of", formal.get("date")) != day or formal.get("test_only"):
        raise ValueError("Same-day formal recommendation required")
    dest = root/"snapshots"/f"{day}.json"
    previous_paths = sorted(path for path in (root/"snapshots").glob("*.json") if path.stem < day)
    previous = read(previous_paths[-1]) if previous_paths else {}
    snapshot = read(dest)
    if snapshot is None:
        if any(path.stem > day for path in (root/"snapshots").glob("*.json")):
            raise ValueError("Cannot insert a test snapshot into observed history")
        financial = read(source/"financials"/f"{day}-c5.json.gz") or read(source/"financials"/f"{day}.json.gz")
        if not financial or financial.get("observed_date") != day:
            raise ValueError("Same-day observed financial snapshot required")
        financial = restore_archived_eps(financial, source, day)
        used = sorted(path for path in (source/"days").glob("*.json.gz") if path.name[:10] <= day)[-121:]
        if len(used) != 121 or used[-1].name[:10] != day:
            raise ValueError("121 market snapshots ending on target session required")
        snapshots = [read(path) for path in used]
        if [x["date"] for x in snapshots] != [path.name[:10] for path in used]:
            raise ValueError("Market snapshot dates differ from filenames")
        for item in snapshots:
            for field in ("prices","institutional"):
                if ({row["market"] for row in item[field]} != {"twse","tpex"} or
                        any(row["date"] != item["date"] for row in item[field])):
                    raise ValueError(f"Incomplete or mismatched {field} snapshot for {item['date']}")
        continuity = ("INITIAL" if not previous else
                      "CONTINUOUS" if previous["date"] == snapshots[-2]["date"] else "RESET_AFTER_GAP")
        quotes = index(snapshots[-1]["prices"], identity)
        outcomes = {label: next_session_rises(previous[key], quotes)
                    for label, key in (("正式 v8","formal"), ("測試策略","members"))} if continuity=="CONTINUOUS" else {}
        rows = market_features(snapshots, financial["universe"])
        roster_state = previous if continuity=="CONTINUOUS" else {}
        active, breadth = breadth_state(rows, roster_state)
        eps_sources, members, eps_known = [], [], None
        if active:
            eps_sources = financial_features(rows, financial, day)
            _, _, members = decide(rank_rows(rows), roster_state)
            eps_known = sum(row["earnings"] is not None for row in rows)
        if date.fromisoformat(day) != today():
            raise ValueError("Test observation crossed the intended Taipei session date")
        snapshot = dict(date=day, version=VERSION, test_only=True, excluded_from_performance=True,
                        active=active,breadth=breadth,members=members,formal=formal["candidates"],
                        formal_corrected=bool(formal.get("correction_batch")),
                        formal_hash=digest(formal), previous_hash=digest(previous), pool_size=len(rows),
                        eps_known=eps_known, eps_sources=eps_sources,
                        market_sha256={path.name:sha256(path.read_bytes()).hexdigest() for path in used},
                        financial_hash=digest(financial), continuity=continuity, outcomes=outcomes)
        write(dest,snapshot)
    if snapshot["version"]!=VERSION or snapshot["formal_hash"]!=digest(formal) or snapshot["previous_hash"]!=digest(previous):
        raise ValueError("Saved test snapshot input identity changed")
    report = root/"reports"/f"{day}.md"
    text = render(snapshot, previous)
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists() and report.read_text(encoding="utf-8") != text:
        raise ValueError("Saved test report changed")
    if not report.exists():
        report.write_text(text,encoding="utf-8")
    delivery = root/"data"/"delivery"/f"{day}.json"
    receipt = read(delivery)
    if receipt is None:
        write(delivery,prepare(digest(text)))
    elif receipt["payload_hash"]!=digest(text):
        raise ValueError("Saved test payload changed")
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("capture","send"))
    parser.add_argument("--date",required=True)
    parser.add_argument("--source",type=Path,default=Path("preview-source/data"))
    parser.add_argument("--root",type=Path,default=Path("test-runs")/TEST_ID)
    args=parser.parse_args()
    if args.mode=="capture":
        capture(args.source,args.root,args.date)
        result={"status":"TEST_REPORT_READY","date":args.date}
    else:
        result=push(args.root/"data",args.root/"reports",args.date)
    print(json.dumps(result))
    if args.mode=="send" and result["status"]!="SENT":
        raise SystemExit(1)


if __name__=="__main__":
    main()
