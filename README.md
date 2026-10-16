# MyContractApp 線上簽約（Manus 站 + Firebase 正式後端）

| 頁面 | 說明 |
|---|---|
| `public/index.html` + `demo.js` | 示範模式：不連資料庫，兩方／三方一次性簽署連結（localStorage） |
| `public/live.html` + `live.js` | 正式簽約管理台：匿名登入 Firebase（mycontractapp-911），依現有 Firestore 規則建立租戶／Owner／合約，呼叫正式 API 產生簽署連結並追蹤進度 |
| `public/sign.html` + `sign.js` | 正式簽署頁：呼叫 `/api/signature/invite`、`/api/signature/action` |

已發布：https://esigndemo-922o2gqm.manus.space （正式管理台 `/live.html`）

文件：
- `docs/FIREBASE_FIX.md`：Firebase 端根本原因與待部署修正（index.html 相對路徑、後端拒絕後仍可簽署）
- `docs/notes_firebase.md`：診斷筆記
- `tests/`：Playwright 端到端測試腳本與 10 輪測試結果

待辦（尚未實作）：上傳合約文件（需先啟用 Firebase Storage）、繁／簡／英翻譯、起約方選擇合約封面。
