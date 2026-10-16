"""MyContractApp API（Cloud Functions for Firebase, Python, asia-east1）。

端點（皆在 /api 之下）：
  GET  /health
  POST /contracts                 建立合約（自動建立租戶與 Owner 身分）        [需登入]
  POST /contracts/document        上傳合約文件（PDF/DOCX/TXT/PNG/JPG，10MB）     [需登入，建立者]
  GET  /contracts/document        下載合約文件                                   [需登入，建立者]
  POST /contracts/translate       翻譯合約內容（zh-TW / zh-CN / en）              [需登入，建立者]
  GET  /contracts/status          合約與各方簽署進度                              [需登入，建立者]
  POST /signature/invite          產生一次性簽署邀請                              [需登入，建立者]
  GET  /signature/invite          以 token 讀取邀請與合約（可帶 lang 翻譯）
  GET  /signature/document        以 token 下載合約文件
  POST /signature/action          簽署／退回修改／拒絕（含手寫簽名圖）
  POST /signature/reminders       逾期標記與催簽提醒                              [需登入]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import base64
import hashlib
import os
import secrets
import smtplib
from typing import Any

from cryptography.fernet import Fernet
from firebase_admin import auth, firestore, initialize_app, storage
from firebase_functions import https_fn, options

from documents import decode_signature, decode_upload, extract_text
from signature_service import contract_closed, issue_token, next_contract_status, token_digest, validate_action
from translation import SUPPORTED, translate_fields

initialize_app()
EMULATOR = os.getenv("FUNCTIONS_EMULATOR") == "true"
PROJECT_ID = os.getenv("GCLOUD_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or "mycontractapp-911"


class _LazyDB:
    """部署時 Firebase CLI 會載入本模組分析函式定義，當下沒有憑證；
    因此 Firestore 連線延到第一次實際使用才建立。"""
    _client = None

    def __getattr__(self, name):
        if _LazyDB._client is None:
            if EMULATOR:  # 本機模擬器：不需要 Google 憑證
                from google.auth.credentials import AnonymousCredentials
                from google.cloud import firestore as gcf
                _LazyDB._client = gcf.Client(project=PROJECT_ID, credentials=AnonymousCredentials())
            else:
                _LazyDB._client = firestore.client()
        return getattr(_LazyDB._client, name)


DB = _LazyDB()
INVITES = "signatureInvites"
KEY = base64.urlsafe_b64encode(hashlib.sha256(os.getenv("SIGNING_TOKEN_ENCRYPTION_KEY", "development-only-change-me").encode()).digest())
CIPHER = Fernet(KEY)
COVERS = {"classic-navy", "business-gold", "minimal-white", "tech-blue", "jade-green", "crimson-seal"}
MAX_PARTIES = 3


# ---------------------------------------------------------------- helpers
def response(data: Any, status: int = 200):
    return https_fn.Response(__import__("json").dumps(data, ensure_ascii=False, default=str), status=status, mimetype="application/json")


def app_base_url() -> str:
    return os.getenv("APP_BASE_URL", "https://mycontractapp-911.web.app").rstrip("/")


def require_user(req) -> dict:
    header = req.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise PermissionError("authentication_required")
    try:
        return auth.verify_id_token(header[7:].strip())
    except Exception as exc:  # noqa: BLE001
        raise PermissionError("invalid_auth_token") from exc


def bucket():
    name = os.getenv("STORAGE_BUCKET") or None
    if EMULATOR:  # 本機模擬器：不需要 Google 憑證
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage as gcs
        return gcs.Client(project=PROJECT_ID, credentials=AnonymousCredentials()).bucket(name or f"{PROJECT_ID}.firebasestorage.app")
    return storage.bucket(name)


def tenant_of(uid: str) -> str:
    return f"t-{uid}"


def contract_ref(tenant_id: str, contract_id: str):
    return DB.collection("tenants").document(tenant_id).collection("contracts").document(contract_id)


def invite_ref(tenant_id: str, contract_id: str, signer_id: str):
    return contract_ref(tenant_id, contract_id).collection(INVITES).document(signer_id)


def owned_contract(user: dict, tenant_id: str, contract_id: str):
    snap = contract_ref(tenant_id, contract_id).get()
    if not snap.exists or (snap.to_dict() or {}).get("createdBy") != user["uid"]:
        raise PermissionError("owner_required")
    return snap


def tx_get(transaction, ref) -> dict:
    """相容新舊版 google-cloud-firestore：transaction.get 可能回傳快照或產生器。"""
    result = transaction.get(ref)
    if not hasattr(result, "to_dict"):
        result = next(iter(result), None)
    return (result.to_dict() if result is not None else None) or {}


def find_invite(raw: str):
    if not raw:
        raise ValueError("token_required")
    snap = next(DB.collection_group(INVITES).where("tokenHash", "==", token_digest(raw)).limit(1).stream(), None)
    if not snap:
        raise LookupError("invite_not_found")
    return snap


def send_email(to: str, subject: str, text: str) -> str:
    host = os.getenv("SMTP_HOST")
    if not host:
        return "preview"
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM", "no-reply@example.com")
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as server:
        server.starttls()
        if os.getenv("SMTP_USERNAME"):
            server.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD", ""))
        server.send_message(message)
    return "sent"


def expiry(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else datetime.now(timezone.utc) + timedelta(days=7)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed <= now or parsed > now + timedelta(days=90):
        raise ValueError("expiration_must_be_between_now_and_90_days")
    return parsed


def iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def content_fields(contract: dict) -> dict[str, str]:
    return {
        "title": contract.get("title", ""),
        "summary": contract.get("summary", ""),
        "body": contract.get("body", ""),
        "documentText": contract.get("documentText", ""),
    }


def translated(ref, contract: dict, lang: str) -> dict[str, str]:
    """翻譯並快取到合約文件 translations.{lang}；原文變更時自動失效。"""
    fields = content_fields(contract)
    digest = hashlib.sha256(repr(sorted(fields.items())).encode()).hexdigest()[:16]
    cached = (contract.get("translations") or {}).get(lang) or {}
    if cached.get("hash") == digest:
        return cached.get("fields", {})
    result = translate_fields(fields, lang)
    ref.set({"translations": {lang: {"hash": digest, "fields": result, "at": firestore.SERVER_TIMESTAMP}}}, merge=True)
    return result


def public_contract(contract_id: str, contract: dict) -> dict:
    doc = contract.get("document") or {}
    return {
        "id": contract_id,
        "title": contract.get("title", ""),
        "summary": contract.get("summary", ""),
        "body": contract.get("body", ""),
        "documentText": contract.get("documentText", ""),
        "coverId": contract.get("coverId", "classic-navy"),
        "language": contract.get("language", "zh-TW"),
        "parties": contract.get("parties", []),
        "status": contract.get("status", "draft"),
        "createdAt": iso(contract.get("createdAt")),
        "document": {"name": doc.get("name"), "type": doc.get("type"), "size": doc.get("size")} if doc else None,
    }


def create_invite(tenant_id: str, contract_id: str, signer: dict, expires_at: datetime, title: str) -> dict:
    email = str(signer.get("email", "")).strip().lower()
    signer_id = str(signer.get("id") or email)
    if "@" not in email or not signer_id:
        raise ValueError("invalid_signer")
    token = issue_token()
    ref = invite_ref(tenant_id, contract_id, signer_id)
    role = str(signer.get("role", "signer"))[:20]
    ref.set({"signerId": signer_id, "email": email, "role": role, "tokenHash": token.digest,
             "tokenCiphertext": CIPHER.encrypt(token.raw.encode()).decode(), "status": "pending",
             "expiresAt": expires_at, "createdAt": firestore.SERVER_TIMESTAMP, "updatedAt": firestore.SERVER_TIMESTAMP,
             "reminderCount": 0})
    url = f"{app_base_url()}/sign/{token.raw}"
    delivery = send_email(email, f"簽署邀請／Signing invitation: {title}",
                          f"請使用專屬連結完成簽署／Use your private signing link:\n{url}\n\n期限／Expires: {expires_at.isoformat()}")
    ref.update({"emailStatus": delivery, "lastSentAt": firestore.SERVER_TIMESTAMP})
    result = {"signerId": signer_id, "email": email, "role": role, "emailStatus": delivery, "expiresAt": expires_at.isoformat()}
    if delivery == "preview":  # 未設定 SMTP 時回傳連結，由建立者自行轉交
        result["previewUrl"] = url
    return result


# ---------------------------------------------------------------- handlers
def h_create_contract(req, payload):
    user = require_user(req)
    uid = user["uid"]
    title = str(payload.get("title", "")).strip()[:120]
    if not title:
        raise ValueError("title_required")
    parties = payload.get("parties") or []
    if not (2 <= len(parties) <= MAX_PARTIES):
        raise ValueError("parties_must_be_2_or_3")
    clean = []
    for i, p in enumerate(parties):
        role = str(p.get("role", "")).strip()[:20] or f"簽署方{i + 1}"
        email = str(p.get("email", "")).strip().lower()
        if "@" not in email:
            raise ValueError("invalid_signer")
        clean.append({"id": f"signer-{i + 1}", "role": role, "email": email})
    cover = str(payload.get("coverId", "classic-navy"))
    if cover not in COVERS:
        cover = "classic-navy"
    lang = str(payload.get("language", "zh-TW"))
    if lang not in SUPPORTED:
        lang = "zh-TW"
    tenant_id = tenant_of(uid)
    tref = DB.collection("tenants").document(tenant_id)
    if not tref.get().exists:
        tref.set({"ownerId": uid, "name": "線上簽約工作區", "createdAt": firestore.SERVER_TIMESTAMP})
    mref = tref.collection("members").document(uid)
    if not mref.get().exists:
        mref.set({"uid": uid, "role": "Owner", "createdAt": firestore.SERVER_TIMESTAMP})
    contract_id = "c-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    contract_ref(tenant_id, contract_id).set({
        "contractId": contract_id, "title": title,
        "summary": str(payload.get("summary", "")).strip()[:2000],
        "body": str(payload.get("body", "")).strip()[:60000],
        "coverId": cover, "language": lang, "parties": clean,
        "partyEmails": {p["role"]: p["email"] for p in clean}, "partyCount": len(clean),
        "createdBy": uid, "status": "draft", "signatures": [],
        "createdAt": firestore.SERVER_TIMESTAMP, "updatedAt": firestore.SERVER_TIMESTAMP,
    })
    return response({"ok": True, "tenantId": tenant_id, "contractId": contract_id})


def h_upload_document(req, payload):
    user = require_user(req)
    tenant_id, contract_id = str(payload["tenantId"]), str(payload["contractId"])
    snap = owned_contract(user, tenant_id, contract_id)
    if (snap.to_dict() or {}).get("status") != "draft":
        raise ValueError("document_locked_after_invite")
    name, ctype, data = decode_upload(str(payload.get("fileName", "")), str(payload.get("contentType", "")), str(payload.get("dataBase64", "")))
    path = f"tenants/{tenant_id}/contracts/{contract_id}/document/{name}"
    blob = bucket().blob(path)
    blob.upload_from_string(data, content_type=ctype)
    text = extract_text(ctype, data)
    snap.reference.update({"document": {"name": name, "type": ctype, "size": len(data), "path": path,
                                        "sha256": hashlib.sha256(data).hexdigest()},
                           "documentText": text, "updatedAt": firestore.SERVER_TIMESTAMP})
    return response({"ok": True, "document": {"name": name, "type": ctype, "size": len(data)}, "textLength": len(text)})


def file_response(contract: dict):
    doc = contract.get("document") or {}
    if not doc.get("path"):
        raise LookupError("document_not_found")
    data = bucket().blob(doc["path"]).download_as_bytes()
    from urllib.parse import quote
    return https_fn.Response(data, status=200, mimetype=doc.get("type", "application/octet-stream"),
                             headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(doc.get('name', 'document'))}",
                                      "Cache-Control": "private, no-store"})


def h_owner_document(req):
    user = require_user(req)
    snap = owned_contract(user, str(req.args.get("tenantId", "")), str(req.args.get("contractId", "")))
    return file_response(snap.to_dict() or {})


def h_translate(req, payload):
    user = require_user(req)
    lang = str(payload.get("lang", ""))
    if lang not in SUPPORTED:
        raise ValueError("unsupported_language")
    snap = owned_contract(user, str(payload["tenantId"]), str(payload["contractId"]))
    return response({"ok": True, "lang": lang, "fields": translated(snap.reference, snap.to_dict() or {}, lang)})


def h_status(req):
    user = require_user(req)
    snap = owned_contract(user, str(req.args.get("tenantId", "")), str(req.args.get("contractId", "")))
    contract = snap.to_dict() or {}
    invites = []
    for inv in snap.reference.collection(INVITES).stream():
        d = inv.to_dict() or {}
        dec = d.get("decision") or {}
        invites.append({"signerId": d.get("signerId"), "role": d.get("role"), "email": d.get("email"),
                        "status": d.get("status"), "expiresAt": iso(d.get("expiresAt")), "emailStatus": d.get("emailStatus"),
                        "reminderCount": d.get("reminderCount", 0),
                        "decision": {"action": dec.get("action"), "name": dec.get("name"), "reason": dec.get("reason"), "at": iso(dec.get("at"))} if dec else None})
    invites.sort(key=lambda x: x.get("signerId") or "")
    out = public_contract(snap.id, contract)
    out.update({"pendingSigners": contract.get("pendingSigners"), "signedCount": len(contract.get("signatures") or [])})
    return response({"ok": True, "contract": out, "invites": invites})


def h_create_invites(req, payload):
    user = require_user(req)
    tenant_id, contract_id = str(payload["tenantId"]), str(payload["contractId"])
    snap = owned_contract(user, tenant_id, contract_id)
    contract = snap.to_dict() or {}
    if contract.get("status") not in (None, "draft"):
        raise ValueError("invites_already_sent")
    expires_at = expiry(payload.get("expiresAt"))
    signers = payload.get("signers") or contract.get("parties") or []
    if not (1 <= len(signers) <= MAX_PARTIES):
        raise ValueError("signers_required")
    result = [create_invite(tenant_id, contract_id, s, expires_at, contract.get("title", "合約")) for s in signers]
    snap.reference.set({"status": "invited", "pendingSigners": len(result), "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
    return response({"ok": True, "invites": result})


def h_read_invite(req):
    snap = find_invite(str(req.args.get("token", "")))
    invite = snap.to_dict() or {}
    error = validate_action(invite, "sign")
    if error:
        return response({"error": error, "status": invite.get("status")}, 410 if error == "invite_expired" else 409)
    cref = snap.reference.parent.parent
    contract = cref.get().to_dict() or {}
    if contract_closed(contract):
        return response({"error": "contract_closed", "contractStatus": contract.get("status")}, 409)
    out = public_contract(cref.id, contract)
    lang = str(req.args.get("lang", ""))
    if lang in SUPPORTED and lang != contract.get("language", "zh-TW"):
        out["translation"] = {"lang": lang, "fields": translated(cref, contract, lang)}
    return response({"ok": True, "invite": {"role": invite.get("role"), "email": invite.get("email"), "expiresAt": iso(invite.get("expiresAt"))}, "contract": out})


def h_signer_document(req):
    snap = find_invite(str(req.args.get("token", "")))
    invite = snap.to_dict() or {}
    if invite.get("status") != "pending":
        raise PermissionError("invite_already_consumed")
    return file_response(snap.reference.parent.parent.get().to_dict() or {})


def h_action(req, payload):
    action, raw = str(payload.get("action", "")), str(payload.get("token", ""))
    snap = find_invite(raw)
    name, reason = str(payload.get("name", "")).strip()[:80], str(payload.get("reason", "")).strip()[:500]
    if action == "sign" and (not name or payload.get("consent") is not True):
        raise ValueError("name_and_consent_required")
    if action in {"decline", "request_changes"} and not reason:
        raise ValueError("reason_required")
    signature_png = decode_signature(str(payload.get("signatureImage", ""))) if action == "sign" else None
    if action == "sign" and not signature_png:
        raise ValueError("signature_required")
    invite_now = snap.to_dict() or {}
    signer_id = invite_now.get("signerId", snap.id)
    sig_path = None
    if signature_png:
        cref0 = snap.reference.parent.parent
        tenant_id = cref0.parent.parent.id
        sig_path = f"tenants/{tenant_id}/contracts/{cref0.id}/signatures/{signer_id}.png"
    tx = DB.transaction()

    @firestore.transactional
    def consume(transaction):
        current = tx_get(transaction, snap.reference)
        error = validate_action(current, action)
        if error:
            raise ValueError(error)
        cref = snap.reference.parent.parent
        contract = tx_get(transaction, cref)
        if contract_closed(contract):
            raise ValueError("contract_closed")
        pending = int(contract.get("pendingSigners", 1))
        status = "signed" if action == "sign" else ("declined" if action == "decline" else "changes_requested")
        decision = {"action": action, "name": name, "reason": reason, "at": datetime.now(timezone.utc),
                    "signerId": signer_id, "role": current.get("role"), "signaturePath": sig_path}
        transaction.update(snap.reference, {"status": status, "decision": decision, "consumedAt": firestore.SERVER_TIMESTAMP, "updatedAt": firestore.SERVER_TIMESTAMP})
        update = {"status": next_contract_status(action, pending),
                  "workflowState": "needs_owner_review" if action != "sign" else "active",
                  "updatedAt": firestore.SERVER_TIMESTAMP}
        if action == "sign":
            update["pendingSigners"] = max(0, pending - 1)
            update["signatures"] = firestore.ArrayUnion([decision])
        else:
            update["closedBy"] = {"role": current.get("role"), "name": name, "reason": reason, "action": action}
        transaction.update(cref, update)
        return {"inviteStatus": status, "status": update["status"]}

    result = consume(tx)
    if signature_png and sig_path:
        bucket().blob(sig_path).upload_from_string(signature_png, content_type="image/png")
    return response({"ok": True, **result})


def h_reminders(req):
    require_user(req)
    now, sent = datetime.now(timezone.utc), 0
    for snap in DB.collection_group(INVITES).where("status", "==", "pending").stream():
        invite = snap.to_dict() or {}
        expires_at = invite.get("expiresAt")
        if not isinstance(expires_at, datetime):
            continue
        if expires_at <= now:
            snap.reference.update({"status": "expired", "updatedAt": firestore.SERVER_TIMESTAMP})
        elif expires_at - now <= timedelta(days=2):
            raw = CIPHER.decrypt(invite["tokenCiphertext"].encode()).decode()
            title = (snap.reference.parent.parent.get().to_dict() or {}).get("title", "合約")
            send_email(invite["email"], f"提醒簽署／Signing reminder: {title}", f"原專屬連結／Original private link: {app_base_url()}/sign/{raw}")
            snap.reference.update({"reminderCount": firestore.Increment(1), "lastReminderAt": firestore.SERVER_TIMESTAMP})
            sent += 1
    return response({"ok": True, "remindersSent": sent})


# ---------------------------------------------------------------- router
@https_fn.on_request(region="asia-east1", memory=options.MemoryOption.MB_512, timeout_sec=120,
                     cors=options.CorsOptions(cors_origins="*", cors_methods=["get", "post", "options"]))
def api(req: https_fn.Request):
    path = req.path.rstrip("/")
    if req.method == "OPTIONS":
        return response({"ok": True})
    if req.method == "GET" and path.endswith("/health"):
        return response({"ok": True, "service": "mycontractapp-api", "region": "asia-east1", "version": "2026-10-16"})
    try:
        payload = req.get_json(silent=True) or {}
        m = req.method
        if m == "POST" and path.endswith("/contracts/document"):
            return h_upload_document(req, payload)
        if m == "GET" and path.endswith("/contracts/document"):
            return h_owner_document(req)
        if m == "POST" and path.endswith("/contracts/translate"):
            return h_translate(req, payload)
        if m == "GET" and path.endswith("/contracts/status"):
            return h_status(req)
        if m == "POST" and path.endswith("/contracts"):
            return h_create_contract(req, payload)
        if m == "POST" and path.endswith("/signature/invite"):
            return h_create_invites(req, payload)
        if m == "GET" and path.endswith("/signature/invite"):
            return h_read_invite(req)
        if m == "GET" and path.endswith("/signature/document"):
            return h_signer_document(req)
        if m == "POST" and path.endswith("/signature/action"):
            return h_action(req, payload)
        if m == "POST" and path.endswith("/signature/reminders"):
            return h_reminders(req)
        return response({"error": "not_found"}, 404)
    except KeyError:
        return response({"error": "invalid_request"}, 400)
    except PermissionError as exc:
        code = str(exc)
        return response({"error": code}, 401 if code in {"authentication_required", "invalid_auth_token"} else 403)
    except LookupError as exc:
        return response({"error": str(exc)}, 404)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code in {"invite_already_consumed", "invite_not_pending", "contract_closed", "invites_already_sent", "document_locked_after_invite"} else (410 if code == "invite_expired" else 400)
        return response({"error": code}, status)
    except Exception as exc:  # noqa: BLE001
        return response({"error": "internal_error", "detail": str(exc)[:300]}, 500)
