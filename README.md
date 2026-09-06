# AgentRouter 自動簽到

這個工具透過重新登入 AgentRouter 觸發簽到，登入方式使用 GitHub OAuth。每次執行會先清除 AgentRouter 的登入 session，再使用保存的 GitHub 瀏覽器 session 完成 OAuth；它不儲存 GitHub 密碼或 Token。

為避免重複刷登入，成功後同一天再次執行會自動跳過。只有在確認需要重試時才使用 `--force`。

## 第一次執行

### macOS

在 Terminal 執行：

```bash
cd /Users/tw527e/Documents/ChatGPT/AutoCheckin
chmod +x run_checkin.command
./run_checkin.command
```

瀏覽器會開啟，請完成 GitHub 登入、2FA 或授權。成功後，登入狀態會保存到 `~/.agentrouter-checkin/chromium-profile`。

### Windows

先安裝 Python 3.10+，然後在命令提示字元執行：

```bat
cd C:\path\to\AutoCheckin
run_checkin.bat
```

## 之後自動執行

登入狀態已存在後，可使用無頭模式：

```bash
./run_checkin.command --headless
```

Windows：

```bat
run_checkin.bat --headless
```

macOS 可在「行事曆與排程」或 `launchd` 每天執行 `run_checkin.command --headless`；Windows 可在「工作排程器」每天執行 `run_checkin.bat --headless`。建議安排在每天第一次使用前，例如 08:00。

## 常用選項

```text
--timeout 300       將首次互動登入等待時間改為 300 秒
--profile-dir PATH  指定登入狀態保存位置
--headless          不顯示瀏覽器，適合排程
--force             忽略今日已成功簽到的記錄並重試
```

如果 GitHub session 過期，重新執行不帶 `--headless` 的命令，在瀏覽器中重新登入即可。
