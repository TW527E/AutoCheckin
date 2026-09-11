# AgentRouter 自動簽到

這個工具可使用兩種方式登入 AgentRouter：

- `password`：使用 AgentRouter 帳號/密碼，自動填入登入表單。
- `github`：使用 GitHub OAuth；第一次執行時在瀏覽器完成 GitHub 登入。

設定檔位於 `~/.agentrouter-checkin/config.json`，真正的設定檔已被 `.gitignore` 忽略，不會推送到 GitHub。若使用帳密登入，設定檔含有明文密碼；macOS/Linux 建議執行 `chmod 600 ~/.agentrouter-checkin/config.json`。

## 設定帳號密碼

macOS/Linux：

```bash
./run_checkin.command --init-config
```

Windows：

```bat
run_checkin.bat --init-config
```

接著編輯 `~/.agentrouter-checkin/config.json`（Windows 對應 `%USERPROFILE%\.agentrouter-checkin\config.json`）：

```json
{
  "login_method": "password",
  "username": "your-email-or-username",
  "password": "your-password",
  "headless": true,
  "timeout": 60,
  "profile_dir": "~/.agentrouter-checkin/chromium-profile",
  "skip_if_checked_in": true
}
```

也可以參考 [config.example.json](config.example.json)。

## 第一次執行

macOS：

```bash
./run_checkin.command
```

Linux：

```bash
./run_checkin.sh --no-headless
```

Windows：

```bat
run_checkin.bat
```

啟動腳本會自動建立 Python 虛擬環境、安裝 Playwright 與 Chromium。帳密登入會自動提交；如果網站要求 CAPTCHA、2FA 或其他確認，請在可見瀏覽器中完成。

## 之後自動執行

設定 `"headless": true` 後，可排程執行：

```bash
./run_checkin.command --headless
```

Windows：

```bat
run_checkin.bat --headless
```

macOS 可用 `launchd`，Windows 可用「工作排程器」。建議每天執行一次，例如 08:00。

### Linux systemd

Linux 使用 systemd 時，請先安裝 Python 3、`python3-venv`，並確認設定檔已完成，尤其是 `login_method`、帳號密碼與 `headless: true`：

```bash
./run_checkin.sh --init-config
chmod 600 ~/.agentrouter-checkin/config.json
```

安裝目前使用者的 systemd timer（預設每天 08:00）與 Telegram 指令服務：

```bash
./install_linux_systemd.sh
```

也可以指定 systemd 行事曆時間與設定檔：

```bash
./install_linux_systemd.sh \
  --on-calendar "*-*-* 07:30:00" \
  --config "$HOME/.agentrouter-checkin/config.json"
```

查看狀態或移除服務：

```bash
./install_linux_systemd.sh --show
./install_linux_systemd.sh --remove
```

簽到服務會使用同一個 Chromium profile 與每日去重記錄。可用 `journalctl --user -u autocheckin-checkin.service` 查看簽到記錄，Telegram 指令服務則可用 `journalctl --user -u autocheckin-telegram.service` 查看。Linux 通常沒有互動式瀏覽器，因此請使用帳密登入並將密碼與 Bot Token 保存在權限為 `600` 的設定檔中；GitHub OAuth、CAPTCHA 或 2FA 需要先以非 headless 模式完成登入。

若要讓使用者登出後服務仍持續執行，安裝器會嘗試啟用 user lingering；若系統拒絕，請由管理員執行 `loginctl enable-linger "$USER"`。

### Telegram Bot

在設定檔填入 BotFather 建立的 Token，以及要接收通知的頻道或群組 Chat ID：

```json
"telegram": {
  "bot_token": "123456:replace-with-your-token",
  "chat_id": "-1001234567890",
  "admin_chat_ids": [],
  "poll_commands": true,
  "notifications": {
    "success": true,
    "error": true,
    "skipped": false
  }
}
```

Bot 必須被加入目標頻道並授予發送訊息權限。`success` 會通知實際簽到成功，`error` 會通知錯誤，`skipped` 預設關閉以避免例行的「今日已簽到」訊息。指令服務會持續接收以下指令，且設定會保存到 `telegram_state.json`：

```text
/notify_off success
/notify_on success
/notify_off error
/notify_on skipped
/notify_status
```

指令可在目標頻道或 `admin_chat_ids` 指定的管理員聊天中執行。systemd 安裝器會在 Bot Token 與 Chat ID 都已設定時啟動指令服務；若尚未設定，之後更新設定檔後重新執行安裝器即可。

## 常用選項

```text
--config PATH                指定 JSON 設定檔
--init-config                建立設定檔並結束
--login-method password      覆寫登入方式：github 或 password
--username VALUE             覆寫帳號
--password VALUE             覆寫密碼；建議使用 AGENTROUTER_PASSWORD 環境變數
--profile-dir PATH           覆寫瀏覽器登入狀態位置
--timeout 300                登入等待秒數
--headless / --no-headless   覆寫是否顯示瀏覽器
--force                      忽略今日已簽到記錄並重試
--telegram-listen            持續執行 Telegram 指令監聽服務
--no-telegram-poll           簽到執行時不輪詢 Telegram（由常駐服務負責時使用）
```

成功後，同一天再次執行預設會跳過，避免重複登入。若登入 session 過期，重新執行非 headless 模式即可。

也可不在設定檔保存密碼，改用環境變數 `AGENTROUTER_USERNAME` 與 `AGENTROUTER_PASSWORD`。環境變數的值會覆寫設定檔。
