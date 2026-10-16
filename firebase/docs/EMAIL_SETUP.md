# 讓 Firebase Functions 自動寄送簽署邀請 Email

系統會讀取 `firebase\functions\.env` 裡的 SMTP 設定。**沒有設定就不寄信**，管理台一樣會顯示每一方的簽署連結，讓你手動轉交。

## 方法 A：用 Gmail 寄（最簡單）

1. 你的 Google 帳號要先開啟「兩步驟驗證」：<https://myaccount.google.com/security>
2. 建立「應用程式密碼」：<https://myaccount.google.com/apppasswords>，名稱輸入 `MyContractApp`，會得到一組 **16 碼密碼**（只顯示一次）。
3. 用記事本打開 `firebase\functions\.env`，在最後加上以下 5 行（換成你的 Gmail 與 16 碼密碼，密碼不要有空格）：

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=你的帳號@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
SMTP_FROM=全球化合約管理系統 <你的帳號@gmail.com>
```

4. 存檔後再雙擊一次 `deploy.cmd`。腳本會**保留**這些 SMTP 設定，只更新其他項目。

Gmail 個人帳號每天約可寄 500 封。

## 方法 B：用公司信箱或寄信服務

| 服務 | SMTP_HOST | SMTP_PORT | SMTP_USERNAME / SMTP_PASSWORD |
|---|---|---|---|
| Microsoft 365 / Outlook | `smtp.office365.com` | `587` | 信箱帳號／密碼（需管理員開啟 SMTP AUTH） |
| SendGrid | `smtp.sendgrid.net` | `587` | `apikey`／你的 API Key |
| Brevo | `smtp-relay.brevo.com` | `587` | 後台顯示的登入帳號／SMTP 金鑰 |
| 使用 SSL 465 埠的主機 | 主機位址 | `465` | 帳號／密碼（系統會自動改用 SSL） |

## 怎麼確認有寄出

1. 建立一份合約，簽署方填你自己的另一個信箱，按「發送簽署邀請」。
2. 管理台每一方會顯示 **Email：已寄出**。若顯示 **寄送失敗**，連結仍可複製轉交，請檢查帳號密碼。
3. 查看錯誤原因：<https://console.cloud.google.com/logs/query?project=mycontractapp-911>，搜尋 `SMTP send failed`。

## 注意

- `.env` 含密碼，**不要上傳到 GitHub**（專案的 `.gitignore` 已排除）。
- 收件人沒收到時，請先看垃圾郵件匣。
