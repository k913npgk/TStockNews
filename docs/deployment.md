# GitHub Actions 與 LINE 設定

## 現在可以驗證什麼

第一版已有兩市場資料收集、KD／MACD、五項篩選、前十排序、不可改寫的每日推薦、交易日價格追蹤、報告、LINE 剩餘額度與重試檢查。

單日官方行情與法人合計已核對；財報／月營收來源檢查經普通股分類後取得 1,976 檔、1,974 筆有效 EPS、1,971 筆三個月營收比率。這是來源覆蓋檢查，不是合格推薦數，也不是歷史驗證成績。實際股票範圍使用官方公司名單與 ISIN CFI=ESVUFR 普通股分類交集。

公開 repository 已建立：[TStockNews](https://github.com/k913npgk/TStockNews)。19 項測試在本地與 Linux Actions 通過；[2026-10-03 非交易日短版執行](https://github.com/k913npgk/TStockNews/actions/runs/37121041985)成功保存 screener-data 分支。此結果只驗證雲端測試、非交易日流程與持久化，尚未完成歷史暖機、完整交易日雲端流程或 LINE 真實群組發送，因此尚不能稱為正式上線。

## 本地暖機（使用者執行）

220 個曆日通常足夠包含 120 個交易日，實際仍依休市日曆。這是歷史價格／法人暖機，不會產生歷史推荐，也不會將最新財報回填為舊財報。

```powershell
python -m pip install -e .
python -B -m tstocknews bootstrap --days 220 *> bootstrap.log
```

這項程序可能超過 5 分鐘，依專案要求交由使用者自行確認完成；不由 agent 持續監控。

另開終端機查核：

```powershell
Get-Content data/status.json
Get-Content bootstrap.log -Tail 10
(Get-ChildItem data/days/*.json.gz).Count
```

`BOOTSTRAPPING` 表示尚在收集；`last_completed` 為最後完整保存的交易日。`BOOTSTRAP_COMPLETE` 表示指定範圍完成；sessions 至少 120 才具備技術暖機長度。失敗時看 log 或 `data/error.json`；同一指令可以略過已完成交易日續跑。

日常執行：

```powershell
python -B -m tstocknews daily
```

非交易日回傳 `NON_TRADING_DAY`；暖機不足回傳 `WARMUP_REQUIRED`。不發送測試或空的暖機報告。歷史指定日期只有已保存同日財報快照才可補跑。

## GitHub

1. 指定或建立 repository，將程式與 `.github/workflows/daily.yml` 放到預設分支。先確認官方資料的使用與再散布條件；私人 repository 可避免公開原始資料。
2. Actions 需有 contents write、actions read 權限；允許專用 `screener-data` 分支寫入。此分支保存 `data/` 與 `reports/`，不存 LINE 憑證或 groupId。
3. 到 Actions 手動執行，mode 選 `bootstrap`，send_line 保持 false。可在 Actions log 與 artifact 的 status.json 查看進度；此首次程序可能超過 5 分鐘。
4. 完成後在交易日晚間執行 mode=daily、send_line=false，檢查報告與資料品質。
5. LINE 完成設定後，手動執行一次 mode=daily、send_line=true。最後把 repository variable `LINE_ENABLED` 設為字串 `true`，才開啟每日自動推播。

預定排程為台灣時間 21:07，UTC cron=`7 13 * * *`。GitHub 不保證準時。跨午夜延遲時沿用原排定日期；若沒有當日財報快照，拒絕用隔日最新資料補成舊推薦。

公開 repository 的標準 runner 免費；私人 repository 依方案有免費額度。暖機與長期資料儲存也計入使用量，需設定零付費邊界並查核用量。Artifacts 只保留 7 天，永久正式狀態靠資料分支；日資料與財報使用確定性 gzip，降低容量。

## LINE

1. 建立 LINE 官方帳號，選台灣輕用量免費方案，啟用 Messaging API。
2. LINE Developers 的 Messaging API 設定中開啟 Allow bot to join group chats，邀 Bot 進兩人群組。
3. 先準備免費的 Cloudflare cloudflared CLI（只需初次設定時使用），以本專案的一次性 webhook 核驗群組事件，方式見下文。
4. 將 `LINE_CHANNEL_ACCESS_TOKEN` 與 `LINE_GROUP_ID` 放在 GitHub Actions repository Secrets；不要貼到聊天、程式或公開設定。
5. 日常只做 push，不需要常駐 webhook 伺服器。

初次取得 Group ID：

1. 到 [LINE Official Account Manager](https://manager.line.biz/) 建立帳號，設定 → Messaging API → 啟用；建立自己的 Provider（例如 TStockNews）。
2. 到 [LINE Developers Console](https://developers.line.biz/console/)，開啟該 Provider 的 Messaging API Channel。Basic settings 取得 Channel Secret；Messaging API 取得／簽發 Channel access token（long-lived）。
3. Messaging API 開啟 Allow bot to join group chats，將 Bot 邀進兩人群組。
4. 本機終端 A 執行 `python -B -m tstocknews.webhook`，在隱藏輸入處貼 Channel Secret。此程式只核验原始 body 的 HMAC 簽章，不輸出 Secret。
5. 終端 B 執行 `cloudflared tunnel --url http://127.0.0.1:8765`，取得臨時 `https://....trycloudflare.com` 網址。下載與使用方式見 [Cloudflare Quick Tunnels 官方說明](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)。此免費 tunnel 只供初次設定測試，不用來維持日常推播。
6. LINE Messaging API 的 Webhook URL 填 `https://....trycloudflare.com/webhook`，按 Verify，啟用 Use webhook。
7. 在正確群組傳一則文字 `TStockNews 設定`。本機會把經簽章核驗的 Group ID 存到 `data/line-group-id.txt`。此檔受 .gitignore 排除，不要公開。
8. GitHub repository → Settings → Secrets and variables → Actions → New repository secret，新增 `LINE_CHANNEL_ACCESS_TOKEN` 和 `LINE_GROUP_ID`。
9. 關閉 LINE Use webhook，終端 A／B 各按 Ctrl+C。之後 Actions 直接 push，不需要這兩個程序常駐。

若群組已有另一個官方帳號，先依 LINE 群組限制處理；不要將 Channel access token 當成 Channel Secret，兩者用途不同。

一次 request 合併前十報告與到期摘要。兩人 × 22 個交易日 × 每日一次約 44 則。發送前確認 plan/quota、實際群組人數與消耗，超額時保留報告。若 API 回傳 unlimited 或額度大於 200，回傳 `PLAN_NOT_FREE`，不繼續付費方案發送。

workflow 先持久化報告與 retry key，再嘗試 push；同日已 SENT 不再送。同 key 超過 23 小時不自動重送，回傳 `RETRY_WINDOW_EXPIRED`，需先核對群組是否已收到，避免 LINE 的 24 小時重試保護失效。

本地若已設定環境變數，可以明確執行：

```powershell
python -B -m tstocknews prepare-send
python -B -m tstocknews send
```

## 資料與研究限制

- KD 與 MACD 使用固定最近 120 個連續有效交易日，EMA 从第一個收盤起算，KD 从 50 起算；缺價不補值。當前為未還原價格，完整除權息／減資／分割調整尚未實作，報告會揭露此限制。
- 已知公司行動可在日價格欄位標記 `corporate_action_unresolved=true`，核心會排除候選或保留未知績效；目前官方 adapter 尚未自動覆蓋所有公司行動。因此輸出為原始價格變化，不能解釋為含息總報酬或實際交易收益。
- 利潤率可由最新損益資料觀察；現金流來源尚未接入，明確存為 null／SOURCE_NOT_IMPLEMENTED。這兩者不是第一版硬門檻。
- EPS 与月營收 snapshot 的 available_date 是首次取得日，不是財報期間，也不是出表日期；不聲稱歷史公告時間已重建。
- 官方最新公司範圍用于技術暖機；沒有歷史完整 universe，不能據此宣稱已做過無存活偏誤回測。
- 年度假日表排除假日；臨時休市若官方年表尚未更新，程序會因預定交易日資料缺漏而停止。可在 `data/extra_closures.json` 以 ISO 日期對應官方公告網址，明確補充，不會自行把缺資料當成休市。
- 開盤價只是價格觀察基準，沒有撮合、漲停成交或滑價模擬；不含手續費、稅與股息。
- 目前只有單一個股訊號績效，未完成市場基準配對或校準機率模型。10 日達標比率不是每檔股票的預測機率。

## 官方參考

- [LINE 群組設定](https://developers.line.biz/en/docs/messaging-api/group-chats/)
- [LINE 訊息計數](https://developers.line.biz/en/docs/messaging-api/sending-messages/)
- [LINE 台灣方案](https://tw.linebiz.com/column/LINEOA-2026-Price-Plan/)
- [GitHub 排程](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub 用量](https://docs.github.com/en/actions/concepts/billing-and-usage)
