# 專案協作與 GitHub push

- 可以視需求使用 Luna 模型的子 agent 提升任務效率。
- 若資料處理或訓練预计超過 5 分鐘，先交付啟動與進度查核方式，停止持續監控，由使用者完成後通知。
- 保留既有未提交變更；有權限問題或無法決定的事項可向使用者詢問。
- 程式變更使用 `codex/` 分支，先通過本地測試再 push、開 PR；main 透過通過 CI 的 PR 合併。禁止 force push、刪除 main，禁止憑證、群組 ID、log 或本地 data/reports 進入程式分支。
- GitHub Actions 的正式資料僅寫 `screener-data`；測試資料僅寫 `screener-tests`。兩者互不合併。
- 已完成本地暖機可用 `scripts/publish-warmup.py` 做一次性受檢查匯入；只包含兩市場同日行情、法人及暖機狀態，不包含財報回填、推薦、績效或憑證。
- 測試不得寫入正式推薦、績效、分析、發送紀錄；不得以後取得財報回填目標日。不足資料須顯示 unknown/incomplete。
- 新 clone 執行 `git config core.hooksPath .githooks` 啟用本地 push 檢查。伺服器分支保護為主要管控，hook 為額外檢查。
