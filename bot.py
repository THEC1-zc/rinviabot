import os
import re
import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

from google.oauth2 import service_account
from googleapiclient.discovery import build


# -----------------------
#  LOAD ENV VARIABLES
# -----------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Rome")


# -----------------------
#  GOOGLE CALENDAR CLIENT
# -----------------------
def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def create_calendar_event(title, start_dt, end_dt, description="Evento creato da RinviaBot", location=""):
    if not GOOGLE_CALENDAR_ID:
        raise RuntimeError("GOOGLE_CALENDAR_ID mancante (controlla .env)")

    service = get_calendar_service()

    event = {
        "summary": title,
        "location": location or "",
        "description": description or "",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
    }

    created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("htmlLink", "")


# -----------------------
#  PARSER (TUO FORMATO)
# -----------------------
def _extract_datetime(text: str):
    """
    Estrae data+ora da testo.
    Supporta:
    - gg/mm/aa o gg/mm/aaaa
    - gg.mm.aa o gg.mm.aaaa
    Ora:
    - 'h 12', 'h12', 'h 12.30', 'h 12:30'
    - '12.30', '12:30'
    - 'hh.mm' (come '12.00')
    Ritorna datetime oppure None
    """
    t = text.strip()

    # data: 13/2/26, 18/09/2026, 18.09.2026
    m_date = re.search(r"\b(\d{1,2})[\/.](\d{1,2})[\/.](\d{2,4})\b", t)
    if not m_date:
        return None

    day = int(m_date.group(1))
    month = int(m_date.group(2))
    year = int(m_date.group(3))
    if year < 100:
        year += 2000

    # ora: prima prova "h 12", "h 12.30", "h 12:30"
    m_time = re.search(r"\bh\s*([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\b", t, re.IGNORECASE)
    if m_time:
        hour = int(m_time.group(1))
        minute = int(m_time.group(2) or 0)
        return datetime.datetime(year, month, day, hour, minute)

    # poi prova "12:30" o "12.30"
    m_time2 = re.search(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)\b", t)
    if m_time2:
        hour = int(m_time2.group(1))
        minute = int(m_time2.group(2))
        return datetime.datetime(year, month, day, hour, minute)

    # se manca l'ora, NON creiamo evento (per evitare eventi sbagliati)
    return None


def parse_simple_event(text: str):
    """
    Formato tuo:
    - Riga 1: titolo evento (cognome o parole/parole)
    - Riga 2: luogo (cognome / una parola)   [opzionale]
    - Righe successive: note libere
    - Data+ora presenti ovunque nel testo: "gg.mm.aaaa hh.mm" oppure "gg/mm/aa h 12" ecc.

    Supporta anche input "tutto su una riga" (come il tuo esempio Ndyae...):
    - titolo = prima riga (tutta)
    - luogo = vuoto
    - note = testo intero
    """
    t = (text or "").strip()
    if not t:
        return None

    # Estrai data+ora da tutto il testo
    start_dt = _extract_datetime(t)
    if not start_dt:
        return None

    # Durata default 1 ora (puoi cambiarla dopo)
    end_dt = start_dt + datetime.timedelta(hours=1)

    # Split righe
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]

    title = lines[0] if lines else "Evento"
    location = ""

    # luogo = seconda riga SOLO se è una singola parola (no spazi) e non contiene numeri
    if len(lines) >= 2:
        candidate = lines[1]
        if re.match(r"^[^\s]+$", candidate) and not re.search(r"\d", candidate):
            location = candidate

    description = t  # note: tutto il testo originale (come vuoi tu)

    return {
        "title": title,
        "location": location,
        "description": description,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


# -----------------------
#  TELEGRAM HANDLER
# -----------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.message.chat_id

    parsed = parse_simple_event(text)
    if not parsed:
        # Se vuoi che risponda anche quando NON riconosce evento, dimmelo.
        return

    try:
        link = create_calendar_event(
            title=f"🤖 {parsed['title']}",
            start_dt=parsed["start_dt"],
            end_dt=parsed["end_dt"],
            description=parsed.get("description", ""),
            location=parsed.get("location", ""),
        )

        msg = (
            f"📅 Evento creato!\n"
            f"• Titolo: {parsed['title']}\n"
            f"• Quando: {parsed['start_dt'].strftime('%d/%m/%Y %H:%M')} → {parsed['end_dt'].strftime('%H:%M')}"
        )
        if parsed.get("location"):
            msg += f"\n• Luogo: {parsed['location']}"
        if link:
            msg += f"\n🔗 {link}"

        await context.bot.send_message(chat_id=chat_id, text=msg)

    except Exception as e:
        print(f"[ERRORE] {repr(e)}")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Errore nella creazione dell'evento.")


# -----------------------
#  MAIN (NO ASYNC LOOP)
# -----------------------
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN mancante (controlla .env)")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("🤖 RinviaBot avviato. In ascolto... (Ctrl+C per fermare)")
    app.run_polling()


if __name__ == "__main__":
    main()