import os
import re
import json
import datetime
import tempfile
import requests
from fastapi import FastAPI, Request, Response

from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()

@app.get("/")
def health():
    return {"ok": True}


PREFIX = "🤖"

# ====== ENV ======
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WA_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Rome")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # consigliato su Railway

# ====== helper: credenziali Google ======
def get_calendar_service():
    sa_path = SERVICE_ACCOUNT_FILE
    if GOOGLE_SERVICE_ACCOUNT_JSON and sa_path == "service-account.json":
        tmp_path = os.path.join(tempfile.gettempdir(), "service-account.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(GOOGLE_SERVICE_ACCOUNT_JSON)
        sa_path = tmp_path

    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)

def create_calendar_event(service, title, start_dt, end_dt, description, location=""):
    event = {
        "summary": title,
        "location": location or "",
        "description": description or "",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }
    created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("htmlLink", "")

# ====== parser data/ora dal testo (uguale logica del tuo bot) ======
def _extract_datetime(text: str):
    m_date = re.search(r"\b(\d{1,2})[\/.](\d{1,2})[\/.](\d{2,4})\b", text)
    if not m_date:
        return None
    d, m, y = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
    if y < 100:
        y += 2000

    m_time_h = re.search(r"\b[hH]\s*([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\b", text)
    if m_time_h:
        hh = int(m_time_h.group(1))
        mm = int(m_time_h.group(2) or 0)
        return datetime.datetime(y, m, d, hh, mm)

    m_time_ore = re.search(r"\bore\s*([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\b", text, re.IGNORECASE)
    if m_time_ore:
        hh = int(m_time_ore.group(1))
        mm = int(m_time_ore.group(2) or 0)
        return datetime.datetime(y, m, d, hh, mm)

    m_time2 = re.search(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)\b", text)
    if m_time2:
        hh = int(m_time2.group(1))
        mm = int(m_time2.group(2))
        return datetime.datetime(y, m, d, hh, mm)

    return None

def parse_event(text: str):
    t = (text or "").strip()
    if not t:
        return None
    start_dt = _extract_datetime(t)
    if not start_dt:
        return None
    end_dt = start_dt + datetime.timedelta(hours=1)

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    title = lines[0] if lines else "Evento"
    location = ""
    if len(lines) >= 2:
        candidate = lines[1]
        if re.match(r"^[^\s]+$", candidate) and not re.search(r"\d", candidate):
            location = candidate

    return {
        "title": title,
        "location": location,
        "description": t,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }

# ====== WhatsApp: invio risposta ======
def wa_send_text(to_number: str, text: str):
    if not (WA_TOKEN and PHONE_NUMBER_ID):
        return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }
    requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)

# ====== 1) verifica webhook (GET) ======
@app.get("/webhook")
def verify_webhook(request: Request):
    """Webhook verification (GET) required by Meta/WhatsApp Cloud API."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token and token == VERIFY_TOKEN and challenge is not None:
        return Response(content=str(challenge), media_type="text/plain", status_code=200)
    return Response(content="Forbidden", media_type="text/plain", status_code=403)

# ====== 2) ricezione messaggi (POST) ======
@app.post("/webhook")
async def incoming(request: Request):
    data = await request.json()

    # struttura: entry -> changes -> value -> messages[]
    try:
        changes = data["entry"][0]["changes"][0]["value"]
        messages = changes.get("messages", [])
        if not messages:
            return {"ok": True}

        msg = messages[0]
        from_number = msg.get("from", "")
        text = msg.get("text", {}).get("body", "")

        parsed = parse_event(text)
        if not parsed:
            wa_send_text(from_number, "Non ho trovato una data/ora evento nel messaggio.")
            return {"ok": True}

        if not GOOGLE_CALENDAR_ID:
            wa_send_text(from_number, "Errore: GOOGLE_CALENDAR_ID non configurato.")
            return {"ok": True}

        service = get_calendar_service()
        link = create_calendar_event(
            service=service,
            title=f"{PREFIX} {parsed['title']}",
            start_dt=parsed["start_dt"],
            end_dt=parsed["end_dt"],
            description=parsed["description"],
            location=parsed.get("location", ""),
        )

        wa_send_text(from_number, f"Evento creato ✅\n{link}".strip())
        return {"ok": True}

    except Exception as e:
        # evita loop: non rispondere sempre in errore
        return {"ok": True, "error": repr(e)}
