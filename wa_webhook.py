import os
import hmac
import hashlib
import json
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, Request, Header
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI()

# =========================
# ENV
# =========================
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")  # must match Meta "Verifica il token"
APP_SECRET = os.getenv("META_APP_SECRET", "")         # optional (for signature validation)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # numeric id or @channelusername

# Optional: add a prefix so you can recognize messages forwarded by the webhook
TG_PREFIX = os.getenv("TG_PREFIX", "📩 WA →")


# =========================
# Helpers
# =========================
def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        return str(obj)


def _verify_signature(app_secret: str, body: bytes, x_hub_signature_256: Optional[str]) -> bool:
    """
    If APP_SECRET is set, verify X-Hub-Signature-256: "sha256=<hex>"
    If APP_SECRET is empty, we skip verification (return True).
    """
    if not app_secret:
        return True
    if not x_hub_signature_256 or not x_hub_signature_256.startswith("sha256="):
        return False

    received = x_hub_signature_256.split("=", 1)[1].strip()
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # If you haven't configured Telegram env vars yet, just do nothing.
        # Railway logs will still show payload.
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    # avoid exceptions killing the webhook
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except Exception:
        pass


def _extract_whatsapp_text(payload: Dict[str, Any]) -> str:
    """
    Extract a human-friendly text from WhatsApp webhook payload (Cloud API).
    If we can't detect a message body, return the whole payload as JSON.
    """
    try:
        entries = payload.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])

                contact_name = ""
                wa_id = ""
                if contacts:
                    contact = contacts[0]
                    wa_id = contact.get("wa_id", "") or ""
                    profile = contact.get("profile", {}) or {}
                    contact_name = profile.get("name", "") or ""

                for msg in messages:
                    mtype = msg.get("type", "")
                    from_ = msg.get("from", "") or wa_id

                    body = ""
                    if mtype == "text":
                        body = (msg.get("text", {}) or {}).get("body", "") or ""
                    elif mtype == "button":
                        body = (msg.get("button", {}) or {}).get("text", "") or ""
                    elif mtype == "interactive":
                        inter = msg.get("interactive", {}) or {}
                        itype = inter.get("type", "")
                        if itype == "button_reply":
                            body = ((inter.get("button_reply", {}) or {}).get("title", "")) or ""
                        elif itype == "list_reply":
                            body = ((inter.get("list_reply", {}) or {}).get("title", "")) or ""
                        else:
                            body = _safe_json(inter)
                    else:
                        # media/location/etc.
                        body = f"[{mtype}] " + _safe_json(msg.get(mtype, msg))

                    who = contact_name or from_ or "unknown"
                    if body.strip():
                        return f"{TG_PREFIX} {who}\n\n{body.strip()}"

        # nothing parsed
        return f"{TG_PREFIX} (unparsed)\n\n{_safe_json(payload)}"
    except Exception:
        return f"{TG_PREFIX} (parse-error)\n\n{_safe_json(payload)}"


# =========================
# Routes
# =========================
@app.get("/")
async def root():
    return {"ok": True}


@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta webhook verification:
    - hub.mode=subscribe
    - hub.verify_token must match WHATSAPP_VERIFY_TOKEN
    - hub.challenge must be echoed as plain text
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge)

    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
):
    body = await request.body()

    # Optional signature check (only if META_APP_SECRET is set)
    if not _verify_signature(APP_SECRET, body, x_hub_signature_256):
        return PlainTextResponse("Invalid signature", status_code=403)

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return PlainTextResponse("Bad Request", status_code=400)

    # Forward to Telegram (best-effort)
    text = _extract_whatsapp_text(payload)
    _telegram_send(text)

    # Always 200 to acknowledge delivery
    return JSONResponse({"status": "ok"})