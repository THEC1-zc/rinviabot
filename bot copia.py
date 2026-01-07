import os
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
    service = get_calendar_service()

    event = {
        "summary": title,
        "location": location,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": TIMEZONE,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": TIMEZONE,
        },
    }

    created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created.get("htmlLink", "")


# -----------------------
#  SIMPLE EVENT PARSER
import re

def parse_simple_event(text: str):
    """
    Supporta:
    A) Formato rigido vecchio:
       evento: Titolo | dd/mm/yyyy | hh:mm | durata_ore

    B) Formato “Fabio” multilinea (preferito):
       Riga 1: titolo (es. "Nobili avv frattasi" oppure "507 ascenzi Maurizio: ...")
       Riga 2: luogo (una parola, es. "Carlomagno") [opzionale]
       Riga 3+: note libere
       Data/ora: ovunque nel testo, es. "13/2/26 h 12" oppure "18/09/2026 h 12" oppure "18/09/2026 12:00"
       Default ora: se manca minuti -> :00
       Durata default: 1 ora
    """
    t = text.strip()
    if not t:
        return None

    # ---------- A) formato rigido
    if t.lower().startswith("evento:"):
        try:
            content = t[len("evento:"):].strip()
            parts = [p.strip() for p in content.split("|")]
            if len(parts) < 3:
                return None
            title = parts[0]
            date_str = parts[1]       # dd/mm/yyyy
            time_str = parts[2]       # hh:mm
            duration_hours = 1.0
            if len(parts) >= 4:
                try:
                    duration_hours = float(parts[3].replace(",", "."))
                except ValueError:
                    duration_hours = 1.0

            day, month, year = [int(x) for x in date_str.split("/")]
            hour, minute = [int(x) for x in time_str.split(":")]
            start_dt = datetime.datetime(year, month, day, hour, minute)
            end_dt = start_dt + datetime.timedelta(hours=duration_hours)
            return {"title": title, "location": "", "description": "", "start_dt": start_dt, "end_dt": end_dt}
        except Exception:
            return None

    # ---------- B) formato tuo (multilinea)
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        return None

    title = lines[0].strip()

    # luogo = seconda riga SOLO se è una singola “parola” (senza spazi)
    location = ""
    if len(lines) >= 2 and re.match(r"^[^\s]+$", lines[1]):
        location = lines[1]

    # description = tutto il testo originale (utile come note)
    description = t

    # --- Estrai data (gg/mm/aa o gg/mm/aaaa) e ora (h 12 / h 12.30 / 12:30)
    # accetta anche gg.mm.aaaa
    date_match = re.search(r"\b(\d{1,2})[\/.](\d{1,2})[\/.](\d{2,4})\b", t)
    if not date_match:
        return None

    d = int(date_match.group(1))
    m = int(date_match.group(2))
    y = int(date_match.group(3))
    if y < 100:
        y += 2000

    # ora: varianti
    # - "h 12" / "h12" / "h 12.30" / "h 12:30"
    # - oppure "12:30" / "12.30"
    time_match = re.search(r"\bh\s*([01]?\d|2[0-3])(?:[.:]([0-5]\d))?\b", t, re.IGNORECASE)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
    else:
        time_match2 = re.search(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)\b", t)
        if time_match2:
            hour = int(time_match2.group(1))
            minute = int(time_match2.group(2))
        else:
            # se non troviamo ora, non creiamo evento (per evitare errori)
            return None

    start_dt = datetime.datetime(y, m, d, hour, minute)
    end_dt = start_dt + datetime.timedelta(hours=1)

    return {
        "title": title,
        "location": location,
        "description": description,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


# -----------------------
#  TELEGRAM MESSAGE HANDLER
# -----------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.message.chat_id

    parsed = parse_simple_event(text)
    if not parsed:
        return

    try:
        link = create_calendar_event(
            title=parsed["title"],
            start_dt=parsed["start_dt"],
            end_dt=parsed["end_dt"],
            description=parsed.get("description", f"Testo originale:\n{text}"),
            location=parsed.get("location", ""),
        )

        msg = (
            f"📅 Evento creato!\n"
            f"• {parsed['title']}\n"
            f"• {parsed['start_dt'].strftime('%d/%m/%Y %H:%M')} → {parsed['end_dt'].strftime('%H:%M')}"
        )
        if link:
            msg += f"\n🔗 {link}"

        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        print(f"[ERRORE] {repr(e)}")
        await context.bot.send_message(
        chat_id=chat_id,
        text="⚠️ Errore nella creazione dell'evento."
    )


# -----------------------
#  MAIN
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