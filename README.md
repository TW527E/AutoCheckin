# AgentRouter 自動簽到

這個工具可使用兩種方式登入 AgentRouter：

- `password`：使用 AgentRouter 帳號/密碼，自動填入登入表單。
- `github`：使用 GitHub OAuth；第一次執行時在瀏覽器完成 GitHub 登入。

設定檔位於程序目錄下的 `config.json`，真正的設定檔已被 `.gitignore` 忽略，不會推送到 GitHub。若使用帳密或 Telegram Bot，設定檔含有敏感資訊；macOS/Linux 建議執行 `chmod 600 ./config.json`。

## 設定帳號密碼

macOS/Linux：

```bash
./run_checkin.command --init-config
```

Windows：

```bat
run_checkin.bat --init-config
```

接著編輯程序目錄下的 `config.json`：

```json
{
  "login_method": "password",
  "username": "your-email-or-username",
  "password": "your-password",
  "headless": true,
  "browser": "chromium",
  "timeout": 60,
  "profile_dir": "~/.agentrouter-checkin/chromium-profile",
  "skip_if_checked_in": true,
  "timezone": "Asia/Taipei"
}
```

也可以參考 [config.example.json](config.example.json)。

`timezone` 使用 IANA 時區名稱；目前預設為 `Asia/Taipei`（UTC+8）。例如台灣伺服器即使系統時區是 UTC+9，簽到日期與排程仍會以 `Asia/Taipei` 計算。

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

## 使用電腦已安裝的 Google Chrome

預設 `"browser": "chromium"` 使用 Playwright 隨附的 Chromium。若要使用電腦已安裝於預設位置的穩定版 Google Chrome，將 `config.json` 的 `browser` 改成 `"chrome"`，或以命令列覆寫：

```bash
# macOS；Linux 改用 ./run_checkin.sh，Windows 改用 run_checkin.bat
./run_checkin.command --browser chrome --no-headless
./run_checkin.command --browser chrome --headless
```

`browser` 與 `headless` 是獨立選項：前者選擇瀏覽器，後者決定是否顯示視窗。命令列優先於設定檔；舊設定檔未填 `browser` 時仍使用 Chromium。Chrome 啟動失敗會報錯，不會偷偷改用 Chromium。啟動腳本首次建立環境時仍會下載 Chromium，但 `--browser chrome` 實際執行的是已安裝的 Chrome。

這會啟動由 Playwright 控制的 Chrome，不是接管已開啟的日常 Chrome 視窗。`profile_dir` 仍使用自動化專用資料夾，**不要指向日常 Chrome 的使用者資料目錄**。切換瀏覽器時建議另設 `"profile_dir": "~/.agentrouter-checkin/chrome-profile"`；新資料夾不會繼承舊登入狀態或每日去重記錄，可能需要重新登入，也可能在同一天再次執行。

### 無頭 Chrome 的 User-Agent

Google Chrome 也支援無頭模式，不是只有 Chromium 能無頭執行。現代 Chrome 的有頭與無頭模式共用瀏覽器實作，但預設 User-Agent 通常仍不同：有頭模式使用 `Chrome/<版本>`，無頭模式通常使用 `HeadlessChrome/<版本>`。實際值取決於版本與啟動設定；Chromium 也可能使用 `Chrome` 這個字樣，因此不能只靠它判斷品牌。

使用正式 Chrome 或顯示視窗都不保證不被判定為自動化；網站也可能參考其他瀏覽器特徵與操作行為。本工具不偽造 User-Agent，也不繞過 CAPTCHA；需要驗證時請使用有頭模式手動完成。可參考 [Chrome Headless 說明](https://developer.chrome.com/docs/chromium/new-headless) 與 [Chromium User-Agent 實作](https://chromium.googlesource.com/chromium/src/+/main/components/embedder_support/user_agent_utils.cc)。

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

Linux 使用 systemd 時，請先安裝 Python 3、`python3-venv`，並確認設定檔已完成，尤其是 `login_method`、帳號密碼、`headless: true` 與 `timezone: "Asia/Taipei"`：

```bash
./run_checkin.sh --init-config
chmod 600 ./config.json
```

新版安裝器將 systemd timer（預設每天 08:00，使用設定檔中的時區）與 Telegram 指令服務安裝至 `/etc/systemd/system`，以系統層級的 `systemctl` 管理，不使用 `--user`。安裝需要 root 權限：

```bash
sudo ./install_linux_systemd.sh
```

`--run-as USER` 可指定服務的執行身分；未指定時使用 `SUDO_USER`，直接以 root 執行且沒有 `SUDO_USER` 時則使用目前的 root 身分。**建議以非 root 使用者執行無頭瀏覽器**：請從原本執行簽到的帳號使用 `sudo` 安裝，或明確指定原帳號（將 `originaluser` 換成實際帳號）：

```bash
sudo ./install_linux_systemd.sh --run-as originaluser
```

服務會明確設定所選使用者的 `User`、`Group` 與 `HOME`，讓 `~` 指向該使用者的家目錄。選對原帳號並沿用原設定檔與 `profile_dir`，才能保留原本的自動化專用瀏覽器 profile、登入狀態與每日去重記錄；不要改用日常 Chrome 的使用者資料目錄。請確認該帳號可讀取設定檔，並可存取程序目錄及寫入 profile／狀態檔。

系統服務不會繼承舊 user service 或登入 shell 的環境變數；原本只透過環境變數提供的帳密不會自動帶入。請確認所選設定檔含有需要的登入憑證與 Bot Token，並以 `600` 權限保護、由執行帳號持有。Linux 通常沒有互動式瀏覽器，建議使用帳密登入；GitHub OAuth、CAPTCHA 或 2FA 需要先以原帳號、相同的專用 profile 在非 headless 模式完成登入。

也可以指定 systemd 行事曆時間與設定檔（預設時區仍為 `Asia/Taipei`）：

```bash
sudo ./install_linux_systemd.sh \
  --run-as originaluser \
  --on-calendar "*-*-* 07:30:00" \
  --config "$PWD/config.json"
```

#### 從舊版使用者服務遷移

安裝器會檢查**所選執行使用者**的 `~/.config/systemd/user`，並公告偵測到的舊版 AutoCheckin units。遷移時會停止舊 timer、簽到與 Telegram 服務，移除已知的 unit 檔及啟用用的符號連結，再安裝系統層級服務。

- 不會刪除設定檔、瀏覽器 profile 或狀態資料，也不會變更 lingering 設定或無關服務。
- 若舊服務清理不完整，會中止遷移，避免新舊服務重複執行。特殊或無法辨識的 overrides 可能需要手動清理後再重新安裝。
- 不會自動移除其他使用者的舊服務；請依原安裝帳號選擇 `--run-as`，並另行確認其他帳號沒有重複排程。

#### 查看狀態與移除

`--show` 只讀取系統層級狀態，不需要 `sudo`；`--remove` 需要 `sudo`，且**只移除系統層級 units**，不會清理舊版使用者 units：

```bash
./install_linux_systemd.sh --show
sudo ./install_linux_systemd.sh --remove
```

直接查看狀態與記錄時也使用系統層級指令，不加 `--user`（若沒有讀取系統 journal 的權限，請加上 `sudo`）：

```bash
systemctl status autocheckin-checkin.timer autocheckin-checkin.service autocheckin-telegram.service
journalctl -u autocheckin-checkin.service
journalctl -u autocheckin-telegram.service
```

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

Bot 必須被加入目標頻道並授予發送訊息權限。`success` 會通知實際簽到成功，訊息以 `✅` 開頭；`error` 會通知錯誤，訊息以 `❌` 開頭。例行的「今日已簽到」跳過不會發送通知。Telegram 指令服務啟動時會自動註冊指令，因此在聊天輸入 `/` 就能看到指令與中文說明：

```text
/checkin   立即執行簽到
/test      發送測試通知
/toggle    顯示選單並切換通知
/status    以選單查看通知狀態
/help      顯示使用說明
```

輸入 `/toggle` 或 `/status` 後，Bot 會用附圖的垂直按鈕格式顯示現有通知類型與「全部通知」；按鈕前的 `✅` 表示開啟，`❌` 表示關閉，點擊即可切換狀態。

`/test` 會立刻發送一則測試訊息，用來確認通知管道正常；它不受通知開關影響，全部關閉時仍會送達，並附上目前開啟的通知類型。若在管理員聊天執行，測試訊息會同時送到設定的目標頻道，可一併確認頻道權限。

`/checkin` 可從聊天直接觸發簽到，不必等待排程；`/checkin force` 會忽略今日已簽到紀錄強制重新登入。簽到在指令服務的背景執行緒中進行，因此 Bot 仍可回應其他指令，結果照一般簽到通知回報（含餘額）。預設會沿用每日去重：若今日已簽到，Bot 會直接回覆「今日已經簽到」而不重新登入。

指令需要有一個程序正在輪詢 Telegram：常駐的 `--telegram-listen` 指令服務（建議，systemd 安裝器會一併安裝）或每次簽到執行開始時的短暫輪詢。因此 `/checkin` 要在指令服務運作時才有作用。另外請避免在排程簽到正在執行的同時觸發 `/checkin`：兩次簽到會搶用同一個 `profile_dir`，後啟動的那次會因瀏覽器設定檔被佔用而失敗並發出錯誤通知。

簽到成功後，程式會在通知中附上當下餘額（例如 `餘額：$25.00`）。餘額是用登入後的瀏覽器 session 直接讀取網站本身使用的 `/api/user/self`，並依照網站的 `display_in_currency` 與 `quota_per_unit` 換算，因此與網頁上顯示的金額一致。若餘額讀取失敗（例如網站改版或 session 失效），只會省略這一行並在終端機印出警告，不影響簽到結果與其他通知內容。

Telegram 請求只會送往 `api.telegram.org`：Bot Token 會先檢查字元集並做 percent-encoding，URL 的協定與主機在送出前再次檢查，DNS 解析結果若有私網、環回或 link-local 位址就整筆拒絕，連線固定使用已驗證的位址（避免 DNS rebinding），並且不跟隨離開 Telegram 主機的重新導向。若你的環境需要 proxy（環境變數或系統設定），請求會交給 proxy 解析與轉送，位址檢查由 proxy 負責，因此本機 proxy（例如 `127.0.0.1`）仍可正常運作。

指令可在目標頻道或 `admin_chat_ids` 指定的管理員聊天中執行，設定會保存到 `telegram_state.json`。若要在私人聊天操作，請把自己的 Telegram User ID 加入 `admin_chat_ids`。systemd 安裝器會在 Bot Token 與 Chat ID 都已設定時啟動指令服務；若尚未設定，之後更新設定檔後重新執行安裝器即可。

## 常用選項

```text
--config PATH                指定 JSON 設定檔
--init-config                建立設定檔並結束
--login-method password      覆寫登入方式：github 或 password
--username VALUE             覆寫帳號
--password VALUE             覆寫密碼；建議使用 AGENTROUTER_PASSWORD 環境變數
--browser chrome             選擇 chrome（已安裝）或 chromium（預設）
--profile-dir PATH           覆寫瀏覽器登入狀態位置
--timeout 300                登入等待秒數
--headless / --no-headless   覆寫是否顯示瀏覽器
--force                      忽略今日已簽到記錄並重試
--telegram-listen            持續執行 Telegram 指令監聽服務
--no-telegram-poll           簽到執行時不輪詢 Telegram（由常駐服務負責時使用）
```

成功後，同一天再次執行預設會跳過，避免重複登入。若登入 session 過期，重新執行非 headless 模式即可。

也可不在設定檔保存密碼，改用環境變數 `AGENTROUTER_USERNAME` 與 `AGENTROUTER_PASSWORD`。環境變數的值會覆寫設定檔。
