"""對 Firebase 模擬器執行完整 API 流程測試（一次跑完，不重複）。
用法：python tests/api_flow_test.py http://127.0.0.1:5055
"""
import base64
import io
import sys
from datetime import datetime, timedelta, timezone

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5055").rstrip("/")
API = BASE + "/api"
AUTH = "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:signUp?key=fake-api-key"
PNG = "data:image/png;base64," + base64.b64encode(
    base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")).decode()
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def docx_b64():
    from docx import Document
    d = Document()
    d.add_paragraph("第一條　合作範圍：乙方提供軟體服務。")
    d.add_paragraph("第二條　保密義務：雙方應保密三年。")
    buf = io.BytesIO()
    d.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


tok = requests.post(AUTH, json={"returnSecureToken": True}).json()["idToken"]
H = {"Authorization": "Bearer " + tok}
exp = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

# ---- 三方合約：上傳、翻譯、封面、簽署、拒絕後鎖定
r = requests.post(API + "/contracts", headers=H, json={
    "title": "全球服務合作合約", "summary": "雙方就軟體服務合作達成協議。", "body": "第一條　合作範圍。\n第二條　服務費用。",
    "language": "zh-TW", "coverId": "business-gold",
    "parties": [{"role": "甲方", "email": "a@example.com"}, {"role": "乙方", "email": "b@example.com"}, {"role": "丙方", "email": "c@example.com"}]})
check("建立三方合約", r.status_code == 200, r.text)
t, c = r.json()["tenantId"], r.json()["contractId"]
check("未登入不可建立合約", requests.post(API + "/contracts", json={"title": "x"}).status_code == 401)
r = requests.post(API + "/contracts/document", headers=H, json={"tenantId": t, "contractId": c, "fileName": "服務合約.docx",
    "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "dataBase64": docx_b64()})
check("上傳 Word 文件並擷取文字", r.status_code == 200 and r.json().get("textLength", 0) > 10, r.text)
r = requests.post(API + "/contracts/document", headers=H, json={"tenantId": t, "contractId": c, "fileName": "a.exe", "contentType": "application/x-msdownload", "dataBase64": "AAAA"})
check("拒絕不支援的檔案類型", r.status_code == 400 and r.json()["error"] == "unsupported_file_type", r.text)
r = requests.get(API + f"/contracts/document?tenantId={t}&contractId={c}", headers=H)
check("建立者可下載文件", r.status_code == 200 and r.content[:2] == b"PK", r.status_code)
r = requests.post(API + "/contracts/translate", headers=H, json={"tenantId": t, "contractId": c, "lang": "zh-CN"})
check("翻譯為簡體（含文件文字）", r.status_code == 200 and "协议" in r.json()["fields"]["summary"] and "保密义务" in r.json()["fields"]["documentText"], r.text[:200])
r = requests.post(API + "/contracts/translate", headers=H, json={"tenantId": t, "contractId": c, "lang": "en"})
check("翻譯為英文（模擬器以假翻譯代替 Cloud Translation）", r.status_code == 200 and r.json()["fields"]["title"].startswith("[en]"), r.text[:200])
st = requests.get(API + f"/contracts/status?tenantId={t}&contractId={c}", headers=H).json()
check("封面選擇已保存", st["contract"]["coverId"] == "business-gold", st["contract"].get("coverId"))

r = requests.post(API + "/signature/invite", headers=H, json={"tenantId": t, "contractId": c, "expiresAt": exp})
check("發送三方簽署邀請", r.status_code == 200 and len(r.json()["invites"]) == 3, r.text[:200])
links = [v["previewUrl"] for v in r.json()["invites"]]
check("簽署連結格式 /sign/<token>", all("/sign/" in u for u in links), links[0])
toks = [u.split("/sign/")[1] for u in links]
check("不可重複發送邀請", requests.post(API + "/signature/invite", headers=H, json={"tenantId": t, "contractId": c, "expiresAt": exp}).status_code == 409)
r = requests.post(API + "/contracts/document", headers=H, json={"tenantId": t, "contractId": c, "fileName": "x.txt", "contentType": "text/plain", "dataBase64": base64.b64encode(b"x").decode()})
check("發出邀請後文件鎖定", r.status_code == 409, r.text)

r = requests.get(API + "/signature/invite", params={"token": toks[0], "lang": "en"})
check("簽署人讀取合約（英文）", r.status_code == 200 and r.json()["contract"]["translation"]["lang"] == "en" and r.json()["contract"]["coverId"] == "business-gold", r.text[:200])
check("簽署人可開啟文件", requests.get(API + "/signature/document", params={"token": toks[0]}).status_code == 200)
r = requests.post(API + "/signature/action", json={"token": toks[0], "action": "sign", "name": "王小明", "consent": True})
check("沒有手寫簽名不能簽", r.status_code == 400 and r.json()["error"] == "signature_required", r.text)
r = requests.post(API + "/signature/action", json={"token": toks[0], "action": "sign", "name": "王小明", "consent": True, "signatureImage": PNG})
check("甲方簽署", r.status_code == 200 and r.json()["status"] == "partially_signed", r.text)
r = requests.post(API + "/signature/action", json={"token": toks[0], "action": "sign", "name": "王小明", "consent": True, "signatureImage": PNG})
check("連結用過即失效", r.status_code == 409 and r.json()["error"] == "invite_already_consumed", r.text)
check("用過的連結無法再讀取", requests.get(API + "/signature/invite", params={"token": toks[0]}).status_code == 409)
r = requests.post(API + "/signature/action", json={"token": toks[1], "action": "decline", "name": "李大華"})
check("拒絕必須填原因", r.status_code == 400 and r.json()["error"] == "reason_required", r.text)
r = requests.post(API + "/signature/action", json={"token": toks[1], "action": "decline", "name": "李大華", "reason": "價格不同意"})
check("乙方拒絕", r.status_code == 200 and r.json()["status"] == "declined", r.text)
r = requests.post(API + "/signature/action", json={"token": toks[2], "action": "sign", "name": "陳丙", "consent": True, "signatureImage": PNG})
check("拒絕後丙方不能再簽（修正後端漏洞）", r.status_code == 409 and r.json()["error"] == "contract_closed", r.text)
st = requests.get(API + f"/contracts/status?tenantId={t}&contractId={c}", headers=H).json()
check("合約維持已被拒絕", st["contract"]["status"] == "declined", st["contract"]["status"])
check("各方狀態正確", [v["status"] for v in st["invites"]] == ["signed", "declined", "pending"], [v["status"] for v in st["invites"]])

tok2 = requests.post(AUTH, json={"returnSecureToken": True}).json()["idToken"]
check("其他使用者看不到這份合約", requests.get(API + f"/contracts/status?tenantId={t}&contractId={c}", headers={"Authorization": "Bearer " + tok2}).status_code == 403)

# ---- 兩方合約：全數簽署
r = requests.post(API + "/contracts", headers=H, json={"title": "Service Agreement", "summary": "Both parties agree to the terms.", "language": "en", "coverId": "tech-blue",
    "parties": [{"role": "Party A", "email": "x@example.com"}, {"role": "Party B", "email": "y@example.com"}]})
t2, c2 = r.json()["tenantId"], r.json()["contractId"]
inv = requests.post(API + "/signature/invite", headers=H, json={"tenantId": t2, "contractId": c2, "expiresAt": exp}).json()["invites"]
r = requests.get(API + "/signature/invite", params={"token": inv[0]["previewUrl"].split("/sign/")[1], "lang": "zh-CN"})
check("英文合約翻成簡體", r.status_code == 200 and "translation" in r.json()["contract"], r.text[:200])
out = [requests.post(API + "/signature/action", json={"token": v["previewUrl"].split("/sign/")[1], "action": "sign", "name": f"Signer {i}", "consent": True, "signatureImage": PNG}).json() for i, v in enumerate(inv)]
check("兩方全數簽署", out[-1].get("status") == "fully_signed", out)
st = requests.get(API + f"/contracts/status?tenantId={t2}&contractId={c2}", headers=H).json()
check("簽名圖路徑已記錄", all(v["status"] == "signed" for v in st["invites"]) and st["contract"]["signedCount"] == 2)

ok = sum(1 for _, p in results if p)
print(f"\n結果：{ok}/{len(results)} 通過")
sys.exit(0 if ok == len(results) else 1)
