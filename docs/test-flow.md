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

- 正式行情／法人／已保存財報僅作輸入。輸入日期必須不晚於目標交易日，完整檢查暖機中應有的交易日；不會用缺報價自行推定休市。
- 至少需要 120 個有效交易日暖機。測試不自動執行長批次 bootstrap，雲端測試上限為 5 分鐘；資料不足會產生明確 incomplete 報告。
- 目標日為今天時可取得當日財報快照，僅存本次測試區；歷史目標日只使用目標日以前已保存的快照。不取今天財報回填歷史，不變更 available_date。
- 歷史財報快照不存在時，仍可驗證 Actions、目標日行情、持久化和 LINE 發送；報告明示「無法完成五項篩選、合格檔數未知」。這不是完整篩選驗收通過。
- 技術指標、五項門檻和排名沿用正式策略；完整資料才產生候選。測試不建立 signal、不呼叫績效追蹤、不提供測試績效。

## 隔離與重跑

正式 job 與 test job 互斥。test job 輸出只放在 `test-runs/<GitHub run_id>/`，僅持久化到 `screener-tests`，不 push `screener-data`；不改寫正式推薦、績效、報告、錯誤或 LINE 發送狀態。程式分支不包含執行資料。

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
