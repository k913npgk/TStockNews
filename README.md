# TStockNews：每日台灣股票篩選與追蹤

目標：每天台灣時間 21:07 由 GitHub Actions 收集公開資料，篩選可能轉強的台灣股票，產生最多 10 檔的報告，透過 LINE Messaging API 發送到群組，並追蹤 1／3／5／10／20 個交易日後的表現。

公開專案：[k913npgk/TStockNews](https://github.com/k913npgk/TStockNews)。

目前已完成 150 個交易日的行情／法人暖機與 LINE Secrets 設定。正式與測試共用推薦建構、四項指標條件、綜合排序與報告；測試推薦獨立保存、不計入績效。操作見 [Actions → LINE 測試](docs/test-flow.md)，自動推播及最新驗收見 [部署指南](docs/deployment.md)。

已確認：上市＋上櫃普通股、四項條件全部通過、不足 10 檔不補、LINE 群組 2 人；採用下列門檻與交易日績效定義（2026-11-24 經使用者決定升級為 `rules-v9-four-indicator-top10`，既有歷史推薦不回寫）。

| 條件 | 已確認定義（`rules-v9-four-indicator-top10`） |
|---|---|
| KD | 日 KD(9,3,3)：`K > D` 或預備黃金交叉（`0 ≤ D−K ≤ 3` 且較前一日縮小） |
| MACD | MACD(12,26,9)：`DIF > DEA` |
| 法人 | 當日三大法人合計淨買超 > 0，且近 20 個交易日法人資料完整 |
| 成交量 | 當日至少 2,000 張（2,000,000 股），且近 20 個交易日中位成交額（收盤 × 成交股數）至少 3,000 萬元 |
| 綜合排序 | 候選池內橫斷面百分位排名（0～1）加權：`40% 近 20 日法人買超占成交量比` ＋ `20% KD(K 值)` ＋ `20% MACD(DIF／收盤)` ＋ `20% 當日成交額`，取前 10 檔 |
| 績效 | 推薦後下一交易日開盤為基準，追蹤第 1／3／5／10／20 個交易日收盤；5／20 日提供約週／約月摘要 |

完整規格見 [正式四指標綜合排序規格](docs/production-four-indicator.md) 與 [docs/project-plan.md](docs/project-plan.md)。

手機 LINE 入選名單以兩行顯示「排名＋股票名稱（代碼）」及「當日漲跌幅」，最多前 10 檔。漲跌以 ▲／▼ 與百分比顯示，持平與缺資料明確標示。完整股價、法人、技術指標、百分位與綜合分數仍保存在推薦 JSON；報告保留日期、符合檔數與績效摘要。

## 第一版原則

- 使用免費資源，執行前檢查配額；不自動升級付費方案。
- 初期輸出「規則篩選排名」，經歷史驗證前不輸出起漲機率百分比。
- 每天的推薦、規則版本與當時可用資料固定保存，不能用後來的財報改寫原推薦。
- 績效尚未到期、停牌、缺價或公司行動資料不完整，皆保留明確狀態，不能記成 0%。
- 排程以 21:07 為預定啟動時間；GitHub Actions 可能延遲。
- Cloudflare Cron 外部觸發與 21:20／21:30 查核配置見 [排程部署](docs/cloudflare-scheduler.md)，部署驗收後才切換主排程。
- 同一天重跑不得重複推薦或重複發送；發送失敗可單獨重試。

排序已確認：近三個交易日法人淨買超股數合計越高越前，依序以近三個月營收年增率、MACD 上升幅度及股票代號決定同分順序。策略版本為 `rules-v8-monthly-revenue-growth-top20`，保留 v7 同口徑累計 EPS 同比改善與所有 C5 門檻，新增最近三個月每月營收皆年增，再取前20檔；原買超占成交量比例仍保存在推薦 JSON 供觀察。研究起漲事件為下一交易日開盤至第 10 個交易日收盤價格報酬至少 +5%；此門檻不用來放寬入選規則。

C5 於 2026-10-28 經使用者決定採用，從合併後首次新建的每日推薦生效；既有推薦不回寫。公式、缺漏處理與快照升級見 [正式版營收加速規格](docs/production-revenue-acceleration.md)。

## 執行與部署

Python 3.11 以上，雲端 workflow 使用 3.12。Linux 無外部執行依賴；Windows 使用 truststore 驗證系統憑證。

```powershell
python -m pip install -e .
python -B -m unittest discover -s tests -v
# 一次性歷史暖機，可能超過 5 分鐘，由使用者執行；可用同一指令續跑
python -B -m tstocknews bootstrap --days 220 *> bootstrap.log
# 每日產生報告；不會直接發送 LINE
python -B -m tstocknews daily
```

暖機進度：查看 `data/status.json` 的 `last_completed`，以及 `bootstrap.log`；完成狀態為 `BOOTSTRAP_COMPLETE`。每日報告在 `reports/YYYY-MM-DD.md`。請勿用今天取得的財報補跑過去的推薦。

GitHub 與 LINE 設定、長批次查核方式及實作限制見 [部署指南](docs/deployment.md)。

## 正式排程

每日台灣時間 21:07 更新行情／法人及當日財報、保存正式推薦與 LINE retry key，再發送報告。交易日暖機不足、來源失敗或發送受阻，Actions 顯示失敗；非交易日不發正式報告。自動推播開關為 repository variable `LINE_ENABLED=true`。

## 實作順序

1. 已建立資料收集、指標、篩選、報告、紀錄與交易日績效追蹤。
2. 已建立 GitHub Actions、壓縮狀態持久化及 LINE 額度／重試防護，部署待驗證。
3. 待補現金流觀察與完整公司行動處理；當前技術指標為未還原價格，績效為原始價格報酬。
4. 累積前瞻紀錄，建立歷史驗證；通過驗證後才考慮機率估計。

EPS改善於2026-10-29經使用者決定採用，從合併後首次新建推薦生效；既有推薦不回寫。來源、比較口徑與快照保存見[正式EPS改善規格](docs/production-eps-improvement.md)。

每月營收皆年增於2026-10-30經使用者決定採用，從合併後首次新建推薦生效。公式、缺漏處理及驗證限制見[正式每月營收成長規格](docs/production-monthly-revenue-growth.md)。
