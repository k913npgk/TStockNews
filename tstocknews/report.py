"""Human-readable report without unvalidated probability claims."""
from collections import Counter
from statistics import mean, median


def pct(value):
    return "尚無結果" if value is None else f"{value:+.2%}"


def render(day, result, performance, strategy, include_performance=True):
    lines = [f"台股每日篩選｜{day}", f"策略：{strategy}；五項全部通過",
             "規則排名；起漲機率尚未驗證。價格績效不含成本與股息。",
             "技術指標使用官方未還原日價格；除權息可能影響訊號。", ""]
    candidates = result["candidates"]
    lines.append(f"符合 {result['eligible_count']} 檔，列出 {len(candidates)} 檔（不足不補）。")
    for i, row in enumerate(candidates, 1):
        lines.append(f"{i}. {row['symbol']} {row.get('name', '')}｜{row['market']}")
        # The engine returns indicator values in a stable dictionary.
        values = row.get("indicators", row)
        lines.append(f"   法人買超比例 {pct(values.get('institutional_buy_volume_ratio'))}；"
                     f"三月營收年增 {pct(row.get('revenue_yoy_3m', values.get('revenue_yoy_3m')))}")
        lines.append(f"   收盤 {row['close']:.2f}；成交 {row['volume_shares']/1000:,.1f}張；累計EPS {row['eps']:.2f}")
        margin = row.get("operating_margin")
        lines.append(f"   財報 {row.get('fiscal_period', '')}；營收窗口截至 {row.get('revenue_end_month') or '資料未提供'}；"
                     f"營業利益率 {pct(margin) if margin is not None else '資料未提供'}")
        lines.append(f"   K/D {row['kd_k']:.2f}/{row['kd_d']:.2f}；DIF/DEA {row['macd_dif']:.3f}/{row['macd_dea']:.3f}")
    if not candidates:
        lines.append("今日沒有符合全部條件的股票。")
    if not include_performance:
        lines += ["", "測試推薦獨立保存，不計算任何測試績效。",
                  "資料品質／篩選摘要：", str(result.get("diagnostics", {}).get("exclusion_counts", {})),
                  "現金流來源尚未接入；利潤率觀察欄位見每日財報快照。"]
        return "\n".join(lines) + "\n"
    lines += ["", "歷次推薦績效｜下一交易日開盤為基準"]
    for h in (1, 3, 5, 10, 20):
        rows = [r for r in performance if r["horizon"] == h]
        mature = [r for r in rows if r["status"] == "MATURE" and r.get("price_return") is not None]
        returns = [r["price_return"] for r in mature]
        label = {5: "（約一週）", 20: "（約一月）"}.get(h, "")
        summary = (f"平均 {pct(mean(returns))}，中位數 {pct(median(returns))}，"
                   f"正報酬 {sum(v > 0 for v in returns) / len(returns):.1%}") if returns else "尚無到期有效樣本"
        status = Counter(r["status"] for r in rows)
        lines.append(f"{h}交易日{label}：{len(returns)}有效／{len(rows)}筆；{summary}")
        other = {k: v for k, v in status.items() if k != "MATURE"}
        if other:
            lines.append("   待定／缺失：" + ", ".join(f"{k}={v}" for k, v in sorted(other.items())))
    event = [r for r in performance if r["horizon"] == 10 and r["status"] == "MATURE"
             and r.get("price_return") is not None]
    lines.append(f"10日+5%研究目標：{sum(r['price_return'] >= .05 for r in event)}/{len(event)}筆；"
                 "歷史達標比例尚不能當成個股起漲機率。")
    diagnostics = result.get("diagnostics", {})
    lines += ["", "資料品質／篩選摘要：", str(diagnostics.get("exclusion_counts", {})),
              "現金流來源尚未接入；利潤率觀察欄位見每日財報快照。",
              "同一股票可多日入選，重疊樣本具相關性；這份報告追蹤訊號價格。"]
    return "\n".join(lines) + "\n"
