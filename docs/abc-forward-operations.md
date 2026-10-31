# A／B／C前瞻影子驗證操作

本流程驗證正式v8的替代用法。A直接使用正式v8推薦建構；B與C保留相同EPS、每月營收、C5、法人、量能與資料品質條件，使用同一完整有效候選池及每日檔數。B沿用法人排序；C先依原KD／MACD門檻的0至2分排序，再沿用原排名。三版都是研究名單，不寫入正式推薦或績效。

EPS使用同一份已觀測財報的當期／去年同期累計基本EPS，保留scope、來源雜湊與真實觀測日期。較寬的B／C池也必須取得必要比較；不得只取得A已入選股的資料，或將未知技術值當成零分。A逐檔順序與共用正式建構核對，B／C相同池與相同檔數核對。

## 啟用條件

`.github/workflows/abc-shadow.yml`目前是程式變更，**尚未表示線上監測已啟用**。需先完成本地測試、推送codex分支、PR通過CI並合併main。工作流程只從main讀取程式，沒有LINE憑證或發送步驟。

啟用後，main的「Daily Taiwan stock screener」成功完成才會觸發影子驗證。工作流程確認上游來源為同一儲存庫的main，不執行PR／fork程式。自動觸發日期使用上游工作建立時間換算台北日期，不依上游任意輸入改寫；capture再核對實際來源日期及時間。跨日延遲完成不能回填成前一日已觀測。

流程將正式`screener-data`的`data/`還原至`preview-source/data`作輸入，僅還原`screener-tests`中的`test-runs/abc-forward-v1`續跑資料。新資料只經`scripts/persist-tests.sh`寫入`screener-tests`，不合併到正式分支。相同影子工作不並行，既有日快照不得改寫。若其他測試同時更新`screener-tests`導致一般push被拒絕，工作應失敗並重新讀取最新狀態再續跑；不得force push。

## 每日順序

```powershell
python -X utf8 -B -m tstocknews.abc_forward register --run-dir test-runs/abc-forward-v1
python -X utf8 -B -m tstocknews.abc_forward capture --run-dir test-runs/abc-forward-v1 --source-dir preview-source/data --date YYYY-MM-DD
python -X utf8 -B -m tstocknews.abc_forward score --run-dir test-runs/abc-forward-v1
```

`YYYY-MM-DD`須替換為當天台北日期。capture只在當日21:07之後、當日結束前接收日期與時效合格的來源。週末回報`NON_TRADING_DAY`，不製造空手績效；其他非交易日依來源與交易日曆核對。既有封存資料的register與score可以重跑，不能藉重跑register重設研究起點或把今天財報寫成歷史觀測。

本地使用的`preview-source/data`應來自實際當日正式資料快照。不要將研究run或歷史報告當成來源資料夾，也不要複製憑證、群組ID或本地log到run。若較寬候選池需補充財報，新增觀測及原始來源證據只保存於隔離run，取得時間必須真實保留。

## 手動重跑

合併main後，可從GitHub Actions選擇「ABC forward shadow validation」的Run workflow；分支選main，date留白表示當日。若需指定日期，也只能指定當天並符合capture時間界線。例如使用已設定GitHub CLI的終端：

```powershell
gh workflow run abc-shadow.yml --ref main
```

手動重跑仍檢查同日、來源新鮮度與不可改寫快照。capture失敗後應閱讀錯誤，待同日來源問題解決再重試；午夜後不回填未成功封存的前一日。GitHub工作中register成功後，即使capture或score失敗，已形成的隔離狀態仍會嘗試保存，便於保留阻斷證據。

## 評估與阻斷

主要比較為C對A的整套策略改善，以及C對B的技術排名額外貢獻。主要持有60交易日，60／100bps成本；訊號筆數、不同選股日期、原本有訊號／空手日期、年度與集中度都須列出。股票紀錄與重疊60日窗口不當獨立樣本。

最低360個連續新決策日及其他正式門檻由登記protocol與score管控；不足時維持證據不足。這是專案保守研究設定，並非保證樣本足夠。研究期間不能因短期表現改參數、重選期間或變更檢驗門檻。

價格缺漏、無法成交、股息、除權息、停牌與下市須保留可核對的覆蓋狀態。公司行動來源覆蓋為`UNKNOWN`時，完整NAV與有效性判斷應保持阻斷，直到經覆核的ledger提供證據。**目前不能將這個工作流程解讀為已實作或已取得完整公司行動來源。** 不得將缺價補成零、直接刪除受影響股票，或用未核對前收盤冒充可成交價。

完成每日capture只代表當日研究資料已保存；完整策略有效性需後續標籤成熟、資金與公司行動檢查及統計條件通過，不會自動升版。

## 固定檢查點與事件證據

登記檔為`docs/abc-forward-protocol-v1.json`，含實際登記時間、跨平台LF正規化程式雜湊及固定規則。主檢查點只使用最早360個決策日全部成熟的結果；C−A、C−B只在60bps計算兩項Bonferroni校正區間，100bps與B−A只作診斷。追加日期不擴大主檢查窗口；後續研究更動需新登記，不每天更換區間尋找通過。

登記閘門要求C−A平均60日增量至少0.5百分點、C−B正增量，兩項校正下界正、100bps差值與資金報酬正，且相對兩參考版的完整資金總報酬改善、最大回撤不劣。即使條件通過也只標為`RESEARCH_PASS_REQUIRES_REVIEW`，不自動升版或投資。

公司行動證據可在每日快照後附加，保留實際覆核時間，不修改原始名單與行情。事件清單須明確聲明該日兩市場覆蓋完整，附來源URL，列股利／股數變更與已證實停牌；不能把查不到資料當作完整無事件。

```powershell
python -X utf8 -B -m tstocknews.abc_forward attest-actions --run-dir test-runs/abc-forward-v1 --file reviewed-actions.json
python -X utf8 -B -m tstocknews.abc_forward score --run-dir test-runs/abc-forward-v1
```

覆核JSON欄位為`date`、`coverage: VERIFIED`、`markets: [twse,tpex]`、`source_urls`、實際`reviewed_at`、`corporate_actions`與`suspensions`兩個明確清單。事件需`date,market,symbol,verified:true,source_url,share_multiplier,cash_per_old_share`；現金正值還須已核實的`cash_payment_date`，付款前只列應收款。停牌需`market,symbol,source_url`。同日多份不同覆核不自動挑選，會阻斷並要求另行明確覆核版本。
