# 可重複執行的 Actions → LINE 測試

## 操作

1. 開啟 [Daily Taiwan stock screener](https://github.com/k913npgk/TStockNews/actions/workflows/daily.yml)，按 **Run workflow**。
2. Branch 選 `main`，mode 選 `test`。
3. date 留空代表 workflow 建立時的台灣日期；也可填 `YYYY-MM-DD`。非交易日依官方年度日曆、已確認臨時休市日及週末回退至上一交易日。例如 `2026-10-03` → `2026-10-02`。
4. 勾選 `send_line=true`，按 Run workflow，才會發送 LINE。保持 false 則只產生報告與 artifact。
5. 打開此次 run 的 Summary，確認 `TEST_REPORT_READY`、`target_date`、`analysis_status`；發送時須另確認 delivery=`SENT`，並在 LINE 群組檢視以「【測試報告｜不計入績效】」開頭的訊息。
6. 下載 `isolated-test-<run_id>` artifact，或查看 `screener-tests` 分支內 `test-runs/<run_id>/reports/<target_date>.md`。

CLI 等價操作（從任意終端機，不含 Secrets）：

```powershell
gh workflow run daily.yml --ref main -f mode=test -f date=2026-10-03 -f send_line=true
gh run list --workflow daily.yml --limit 5
gh run view <run_id>
gh run download <run_id> --name isolated-test-<run_id> --dir test-runs/download-<run_id>
```

## 資料和日期規則

- 行情／法人輸入日期不晚於目標交易日，完整檢查暖機中應有的交易日；不會用缺報價自行推定休市。
- 至少需要 120 個有效交易日暖機。測試不自動執行長批次 bootstrap，雲端測試上限為 5 分鐘；資料不足會產生明確 incomplete 報告。
- 財報優先使用目標日以前保存的快照；若不存在，依使用者授權取得執行時最新財報，僅存本次測試區。例如 10/03 執行，行情／法人截至 10/02，財報取得日 10/03，觀察的下一交易日 10/05。
- 不改寫原始 `observed_date`／`available_date`；較晚財報透過明確的測試資料截止日使用，報告揭露兩個日期及「不是歷史當時資訊重現」。正式歷史推薦仍禁止使用較晚財報。
- 正式與測試共用推薦建構函式、KD／MACD、五項門檻、排名及報告呈現。測試會建立相同欄位結構的推薦／signals，ID 使用 `test:<run_id>:` 前綴，保存至 `test-runs/<run_id>/data/recommendations/<target_date>.json`，標記 `test_only` 及 `excluded_from_performance`。
- 報告以手機 LINE 短行、每檔分組呈現股價／成交、法人、營收／財報與 KD／MACD；保留數值，移除「為什麼入選」、內部版本與診斷代碼。正式績效保留全部五個追蹤期間，以中文呈現等待／缺漏狀態。
- 當日漲跌金額使用官方行情欄位；漲跌幅以 `官方漲跌金額 ÷（收盤價 − 官方漲跌金額）` 計算，採當日參考價，不直接用前日收盤替代。無比價或缺資料顯示「資料未提供」，不補零。舊快取缺欄位時，測試僅在自身副本補取目標日官方行情，先核對原有 OHLCV 一致，不改寫正式資料；來源失敗或行情衝突即停止。
- 測試不呼叫績效追蹤、不建立測試績效；正式追蹤器也會拒絕計入帶測試標記的訊號。官方來源失敗即停止，不以資料缺漏冒充篩選成功。

## 隔離與重跑

正式 job 與 test job 互斥。test job 輸出只放在 `test-runs/<GitHub run_id>/`，僅持久化到 `screener-tests`，不 push `screener-data`；不改寫正式推薦、績效、報告、錯誤或 LINE 發送狀態。程式分支不包含執行資料。

Summary 的 `financial_observed_date`、`next_trading_day`、`warmup_sessions`、`financial_coverage` 可檢視資料範圍。只有 `analysis_status=COMPLETE` 代表完整五項篩選已完成；符合數可以為零，不會為了填滿報告放寬條件。

每次 **Run workflow** 建立不同測試編號，可以重複檢視同一目標日。**Re-run jobs** 沿用同一編號、不可改寫報告及相同 LINE retry key：已 SENT 不重送；網路失敗可在 23 小時內重試；超過 23 小時停止，先核對群組再決定是否建立新測試。報告和 retry key 在外部 push 前先存到測試分支。額度不足／非免費方案／重試逾期會讓發送 step 失敗，不能把綠色 Actions 當成已送達。

测试訊息仍消耗 LINE 實際免費配額；收件群組沿用正式 Secrets。只隔離分析及績效資料，沒有免計費的 LINE 發送方式。LINE API 接受 request 與使用者已閱讀是不同驗收，最後仍需在群組確認顯示。

本地預覽（不發送、不寫正式資料）：

```powershell
python -B -m tstocknews test-report --date 2026-10-03 --run-id local-20261003
```

## GitHub push 管控

程式使用 `codex/` 分支 → 本地 unittest → push → PR → CI `unit-tests` → 合併 main。main 禁止直接 push、force push、刪除；資料分支保留 Actions 一般更新，禁止 force push 與刪除。`screener-data` 與 `screener-tests` 不互相合併。

目前 checkout 與新的 clone 應執行 `git config core.hooksPath .githooks`。hook 拒絕直接 push main、資料分支、非 codex 分支、非 fast-forward、刪除及執行資料／憑證檔，並於程式 push 前執行測試。GitHub 分支保護負責伺服器端檢查。

已完成的本地暖機可用 `python scripts/publish-warmup.py` 做一次性受檢查同步；僅匯入 `data/days` 與暖機狀態，遇既有日資料衝突即停止。這是人工暖機匯入的明確例外，日常資料由 Actions 維護。不要上傳 `data/source_check.json`、群組 ID 或整個本地 data 目錄。
