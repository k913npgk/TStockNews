# TStockNews：每日台灣股票篩選與追蹤

目標：每天台灣時間 21:07 由 GitHub Actions 收集公開資料，篩選可能轉強的台灣股票，產生最多 10 檔的報告，透過 LINE Messaging API 發送到群組，並追蹤 1／3／5／10／20 個交易日後的表現。

公開專案：[k913npgk/TStockNews](https://github.com/k913npgk/TStockNews)。

目前狀態：第一版程式與排程已發布，19 項測試通過；官方單日行情／法人及財報／三個月營收來源已做小量驗證。[雲端非交易日驗證](https://github.com/k913npgk/TStockNews/actions/runs/37121041985)通過，資料分支持久化成功。尚未完成全歷史暖機、完整交易日雲端驗收與真實 LINE 群組發送；LINE 自動推播尚未啟用。

已確認：上市＋上櫃普通股、五項條件全部通過、不足 10 檔不補、LINE 群組 2 人；採用下列門檻與交易日績效定義。

| 條件 | 已確認定義 |
|---|---|
| 財報與營收 | 最近已公布累計 EPS > 0；近三個月合計營收年增 > 0。現金流與利潤率列報告供觀察 |
| KD | 當天日 KD(9,3,3) 黃金交叉 |
| MACD | MACD(12,26,9) DIF > DEA，且最近三個交易日 DIF 逐日上升 |
| 法人 | 三大法人合計連續三個交易日淨買超 |
| 成交量 | 當日至少 2,000 張，即 2,000,000 股 |
| 績效 | 推薦後下一交易日開盤為基準，追蹤第 1／3／5／10／20 個交易日收盤；5／20 日提供約週／約月摘要 |

完整規格見 [docs/project-plan.md](docs/project-plan.md)。

## 第一版原則

- 使用免費資源，執行前檢查配額；不自動升級付費方案。
- 初期輸出「規則篩選排名」，經歷史驗證前不輸出起漲機率百分比。
- 每天的推薦、規則版本與當時可用資料固定保存，不能用後來的財報改寫原推薦。
- 績效尚未到期、停牌、缺價或公司行動資料不完整，皆保留明確狀態，不能記成 0%。
- 排程以 21:07 為預定啟動時間；GitHub Actions 可能延遲。
- 同一天重跑不得重複推薦或重複發送；發送失敗可單獨重試。

排序已確認：三日法人淨買超／同期成交量優先，依序以近三個月營收年增率、MACD 上升幅度及股票代號決定同分順序。研究起漲事件為下一交易日開盤至第 10 個交易日收盤價格報酬至少 +5%；此門檻不用來放寬入選規則。

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

## 尚需設定

1. LINE 官方帳號、Bot 加群、Group ID 與 GitHub Secrets。
2. 手動完成一次性歷史暖機，再驗證雲端報告及實際發送。

## 實作順序

1. 已建立資料收集、指標、篩選、報告、紀錄與交易日績效追蹤。
2. 已建立 GitHub Actions、壓縮狀態持久化及 LINE 額度／重試防護，部署待驗證。
3. 待補現金流觀察與完整公司行動處理；當前技術指標為未還原價格，績效為原始價格報酬。
4. 累積前瞻紀錄，建立歷史驗證；通過驗證後才考慮機率估計。
