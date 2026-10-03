"""Short, grouped plain-text reports for mobile LINE readers."""
from collections import Counter
import math
import re
from statistics import mean, median


SEPARATOR = "━━━━━━━━━━━━"


def pct(value):
    return "資料未提供" if value is None else f"{value:+.2%}"


def daily_change(row):
    change, rate = row.get("price_change"), row.get("price_change_pct")
    if change is None or not math.isfinite(change):
        return "資料未提供"
    marker = "▲ " if change > 0 else "▼ " if change < 0 else ""
    amount = f"{change:+.2f}" if change else "0.00"
    percent = (f"{rate:+.2%}" if rate else "0.00%") if rate is not None and math.isfinite(rate) else "幅度未提供"
    return f"{marker}{amount} 元（{percent}）"


def fiscal_label(value):
    match = re.fullmatch(r"(\d{4})-?Q([1-4])", value or "")
    if not match:
        return value or "資料未提供"
    period = {"1": "第一季", "2": "上半年", "3": "前三季", "4": "全年"}[match[2]]
    return f"{match[1]} 年{period}"


def render(day, result, performance, strategy, include_performance=True):
    lines = [f"📊 台股每日篩選｜{day}",
             f"今日符合 {result['eligible_count']} 檔，列出 {len(result['candidates'])} 檔", ""]
    for i, row in enumerate(result["candidates"], 1):
        values = row.get("indicators", row)
        market = {"twse": "上市", "tpex": "上櫃"}.get(row["market"], row["market"])
        rank = "①②③④⑤⑥⑦⑧⑨⑩"[i - 1] if i <= 10 else str(i)
        month = row.get("revenue_end_month")
        lines += [SEPARATOR, f"{rank} {row.get('name', '')}（{row['symbol']}）｜{market}", "",
                  "💰 股價與成交", f"收盤價：{row['close']:.2f} 元",
                  f"當日漲跌：{daily_change(row)}",
                  f"成交量：{row['volume_shares']/1000:,.1f} 張", "",
                  "🏦 法人", "近 3 日法人買超占成交量：",
                  pct(values.get("institutional_buy_volume_ratio")), "",
                  "📋 營收與財報", "近 3 個月營收年增：" + pct(row.get("revenue_yoy_3m", values.get("revenue_yoy_3m"))),
                  f"營收資料截至：{month.replace('-', '/') if month else '資料未提供'}",
                  f"財報期間：{fiscal_label(row.get('fiscal_period'))}",
                  f"累計每股盈餘 EPS：{row['eps']:.2f} 元",
                  "營業利益率：" + pct(row.get("operating_margin")), "",
                  "📉 技術指標", f"KD：K {row['kd_k']:.2f}／D {row['kd_d']:.2f}",
                  "MACD：", f"DIF {row['macd_dif']:.3f}／DEA {row['macd_dea']:.3f}", ""]
    if not result["candidates"]:
        lines += ["今天沒有符合條件的股票，", "持續觀察即可。", ""]
    if include_performance:
        lines += [SEPARATOR, "📈 歷次入選表現", ""]
        for horizon in (1, 3, 5, 10, 20):
            rows = [r for r in performance if r["horizon"] == horizon]
            mature = [r for r in rows if r["status"] == "MATURE" and r.get("price_return") is not None]
            returns = [r["price_return"] for r in mature]
            label = {5: "（約一週）", 20: "（約一個月）"}.get(horizon, "")
            lines.append(f"持有 {horizon} 個交易日{label}")
            if returns:
                lines += [f"平均：{pct(mean(returns))}", f"中位數：{pct(median(returns))}",
                          f"上漲比例：{sum(v > 0 for v in returns) / len(returns):.1%}"]
            else:
                lines.append("尚無到期且資料完整的結果")
            lines.append(f"有效紀錄：{len(returns)}／{len(rows)} 筆")
            statuses = Counter(r["status"] for r in rows
                               if r["status"] != "MATURE" or r.get("price_return") is None)
            pending = statuses.pop("PENDING_ENTRY", 0) + statuses.pop("NOT_MATURE", 0)
            if pending:
                lines.append(f"尚待追蹤：{pending} 筆")
            names = {"MISSING_PRICE": "價格資料不足", "SUSPENDED": "停牌待確認",
                     "CORPORATE_ACTION_UNRESOLVED": "除權息等資料待確認", "MATURE": "價格資料不足"}
            descriptions = Counter()
            for status, count in statuses.items():
                descriptions[names.get(status, "其他待確認")] += count
            for name, count in sorted(descriptions.items()):
                lines.append(f"{name}：{count} 筆")
            if horizon == 10:
                lines.append(f"漲幅達 5%：{sum(v >= .05 for v in returns)}／{len(returns)} 筆" if returns else "漲幅達 5%：尚無結果")
            lines.append("")
    else:
        lines += ["測試推薦獨立保存，不計算任何測試績效。", ""]
    lines += [SEPARATOR, "💡 閱讀提醒", "排名不代表上漲機率。"]
    if include_performance:
        lines += ["績效以入選後下一交易日開盤價計算，", "未扣交易費用，也未計股息。",
                  "尚未到期或缺少資料的紀錄不計入。", "同一股票可重複入選，紀錄不等於投資組合報酬。"]
    lines += ["KD、MACD 使用未還原股價，", "除權息可能影響技術指標。"]
    return "\n".join(lines) + "\n"
