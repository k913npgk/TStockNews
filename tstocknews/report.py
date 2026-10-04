"""Compact ranked-name reports for mobile LINE readers."""
from collections import Counter
from statistics import mean, median


SEPARATOR = "━━━━━━━━━━━━"


def pct(value):
    return "資料未提供" if value is None else f"{value:+.2%}"


def render(day, result, performance, strategy, include_performance=True):
    lines = [f"📊 台股每日篩選｜{day}",
             f"今日符合 {result['eligible_count']} 檔，列出 {len(result['candidates'])} 檔", ""]
    for i, row in enumerate(result["candidates"], 1):
        rank = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"[i - 1] if i <= 20 else str(i)
        lines.append(f"{rank} {row.get('name', '')}")
    if result["candidates"]:
        lines.append("")
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
