# Cloudflare 排程 → GitHub Actions → LINE

Cloudflare Worker 只負責觸發與讀取執行結果；股票資料收集、五項篩選、排名、正式推薦、績效與 LINE 發送仍由 GitHub-hosted runner 執行。正式資料仍只寫 `screener-data`，隔離測試仍只寫 `screener-tests`。

## 每日行為

| 台灣時間 | UTC Cron | 動作 |
| --- | --- | --- |
| 21:07 | `7 13 * * *` | 讀取當日送達狀態；未送達且沒有對應 run 時，觸發 `daily.yml` |
| 21:20 | `20 13 * * *` | 查核是否建立 run；只補觸發漏掉的 run，排隊或執行中不重複觸發 |
| 21:30 | `30 13 * * *` | 讀取當日 `delivery=SENT` 或當日 `NON_TRADING_DAY`；未確認時記錄失敗，供人工查核 |

`date` 使用 Cloudflare 事件的 `scheduledTime` 加 UTC+8，並明確傳給 GitHub；不使用 Worker 實際執行時的日期。所有 dispatch 固定 `ref=main`、`mode=daily`、`send_line=true`、`scheduler=cloudflare`。每日都觸發，由正式流程的官方交易日曆判斷休市。

`run-name` 帶模式與目標日期，隔離測試或前一日延遲作業不能充當今日完成證據。GitHub 同一狀態分支維持全域 `concurrency`，不取消執行中的作業。已 `SENT` 的當日報告會先核對 payload hash，通過後直接略過官方資料收集與推播；報告缺失或被改寫則明確失敗。

完成但未送達、失敗或取消的 run 不會自動重新分析。21:30 未確認送達會讓 Cloudflare Cron Events 顯示失敗，log 只包含日期、階段、狀態與 run ID。這是查核記錄，**不會另外發送 LINE 或電子郵件警報**；需要登入 Cloudflare 或 GitHub 查看。run 的 `success` 不能替代正式 `SENT` 記錄。

## 帳戶與秘密

使用 Cloudflare Workers Free。此 Worker 不需要 KV、Durable Objects、公開 HTTP 路由或網域。

建立一枚 GitHub fine-grained PAT，只選 `k913npgk/TStockNews`，Repository permissions 設定：

- **Actions: Read and write**：查詢 run 與 `workflow_dispatch`。
- **Contents: Read-only**：讀取 `screener-data` 的 delivery／status。

不授予 Contents write；不把 LINE token 或群組 ID 移到 Cloudflare。PAT 存在 Cloudflare Secret `GITHUB_TOKEN`，到期前需更新。PAT 不應貼到聊天或寫入程式、設定、`.env`、log。

若需建立 PAT，可使用 [GitHub fine-grained token 設定](https://github.com/settings/personal-access-tokens/new)。API 權限依 [GitHub workflow dispatch 文件](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。Cloudflare Secret 使用 [官方加密 binding](https://developers.cloudflare.com/workers/configuration/secrets/)。

## 部署與切換

先將程式透過通過 CI 的 PR 合併 main，保留原 GitHub cron。以下 PowerShell 指令在專案根目錄執行：

```powershell
npm --prefix cloudflare ci
npm --prefix cloudflare run login
npm --prefix cloudflare run deploy:check
npm --prefix cloudflare run deploy:disabled
npm --prefix cloudflare run secret:github
npm --prefix cloudflare run deploy
```

`secret put` 使用隱藏輸入貼 PAT。若 Cloudflare 帳戶有多個 account，先指定部署 account；account ID 可透過本機 `CLOUDFLARE_ACCOUNT_ID` 環境變數指定，不需要寫入版本控制。登入與 Secret 必須在部署前可用。沒有 Secret 時不得切換。

確認 Cloudflare dashboard 的 Worker `tstocknews-scheduler` 有三個 Cron Triggers、`GITHUB_TOKEN` Secret、`DISPATCH_ENABLED=true`、Observability logs。Cron 變更可能需 [最多 15 分鐘傳播](https://developers.cloudflare.com/workers/configuration/cron-triggers/)。

核對部署的 Secret、Cron、啟用開關及遠端 scheduled handler 查核通過後，切換主排程；首日仍需從 Cron Events／GitHub Actions 核對自動觸發與正式送達：

```powershell
gh variable set SCHEDULER_PROVIDER --body cloudflare
```

原 GitHub `schedule` 仍可建立 run，但 job 會略過，不再收集或發送。手動 dispatch 照常可用。Cloudflare dispatch 同樣受 GitHub `LINE_ENABLED=true` 控制；將其改成 `false` 時可以產生報告，但不自動發送，此時 21:30 會顯示尚未確認送達。

回復 GitHub 排程：先把 Worker 的 `DISPATCH_ENABLED` 設為字串 `false` 並部署，再執行 `gh variable set SCHEDULER_PROVIDER --body github`。不要停用整個 GitHub workflow，否則手動與 Cloudflare dispatch 也會停止。

## 測試與查核

```powershell
python -B -m unittest discover -s tests -v
node --test cloudflare/worker.test.mjs
npm --prefix cloudflare run test:runtime
gh run list --workflow daily.yml --limit 10
```

單元測試不連網、不發 LINE、不寫正式或測試資料分支；涵蓋 UTC+8 日期、已送達略過、休市、漏觸發補跑、排隊／執行中去重、隔離測試排除、完成但未送達、API 失敗與分頁上限。Wrangler dry-run 驗證 Worker 打包與配置，不代表已部署或已送達。

若資料收集超過 5 分鐘，依 AGENTS.md 交由使用者從 run 連結查核，保留 checkpoint；不要取消或另開同日作業。GitHub-hosted runner 排隊與官方來源仍可能延遲，外部 cron 不保證 21:30 前完成。
