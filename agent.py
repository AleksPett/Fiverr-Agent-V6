import imaplib
import email
import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

GMAIL_USER       = os.environ["GMAIL_USER"]
GMAIL_PASSWORD   = os.environ["GMAIL_PASSWORD"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL", "120"))

SYSTEM_PROMPT = (
    "Du er en profesjonell frilansassistent. "
    "Du mottar jobbbestillinger fra Fiverr og leverer ferdig, komplett arbeid. "
    "Skriv alltid på samme språk som kunden bruker. "
    "Lever aldri halvferdige svar eller skisser."
)

# Lagrer aktive ordrer i minnet: {order_id: {task, customer, delivery, history}}
pending_orders = {}

# Holder siste Telegram update_id for polling
last_update_id = 0


# ─── TELEGRAM ────────────────────────────────────────────────────────────────

def tg_request(method, payload):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.error(f"Telegram {method} feil: {e.code} - {e.read().decode()}")
        return None
    except Exception as e:
        log.error(f"Telegram {method} feil: {e}")
        return None

def send_telegram(message, reply_markup=None):
    message = message[:4000]
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_request("sendMessage", payload)
    log.info("Telegram-melding sendt.")

def send_order_to_telegram(order_id, customer, task, delivery):
    message = (
        f"NY ORDRE: {order_id}\n"
        f"Kunde: {customer}\n"
        f"Oppgave: {task[:200]}\n\n"
        f"--- LEVERANSE ---\n{delivery}\n-----------------\n\n"
        f"Svar med:\n"
        f"  ok {order_id}  →  godkjenn og marker som klar\n"
        f"  endre {order_id}: [din instruksjon]  →  be om endring\n\n"
        f"Eksempel: endre {order_id}: gjor den kortere og mer uformell"
    )
    send_telegram(message)

def get_telegram_updates():
    global last_update_id
    payload = {
        "offset": last_update_id + 1,
        "timeout": 5,
        "limit": 10
    }
    result = tg_request("getUpdates", payload)
    if not result or not result.get("ok"):
        return []
    updates = result.get("result", [])
    if updates:
        last_update_id = updates[-1]["update_id"]
    return updates


# ─── GMAIL ───────────────────────────────────────────────────────────────────

def imap_connect():
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASSWORD)
    return mail

def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw = part.get_payload(decode=True)
                if raw:
                    body += raw.decode("utf-8", errors="ignore")
    else:
        raw = msg.get_payload(decode=True)
        if raw:
            body = raw.decode("utf-8", errors="ignore")
    return body.strip()

def fetch_unseen_fiverr():
    mail = imap_connect()
    mail.select("inbox")
    _, data = mail.search(None, '(UNSEEN FROM "fiverr.com")')
    ids = data[0].split()
    results = []
    for eid in ids:
        _, raw = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(raw[0][1])
        results.append({
            "id": eid,
            "subject": msg.get("Subject", ""),
            "body": get_body(msg),
        })
        mail.store(eid, "+FLAGS", "\\Seen")
    mail.logout()
    log.info(f"Sjekket innboks: {len(results)} ny(e) Fiverr-epost(er).")
    return results


# ─── CLAUDE ──────────────────────────────────────────────────────────────────

def extract_task(subject, body):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system="Trekk ut jobbdetaljer fra en Fiverr-epost. Svar KUN med JSON, ingen annen tekst.",
        messages=[{
            "role": "user",
            "content": (
                "Analyser denne Fiverr-eposen og svar med JSON:\n"
                '{"is_order": true/false, "order_id": "id eller null", '
                '"task": "hva kunden vil ha", "customer": "navn eller null"}\n\n'
                f"SUBJECT: {subject}\nBODY: {body[:1500]}"
            )
        }]
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)

def solve_task(task, order_id, customer, history=None):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    messages = []

    # Legg til tidligere runder hvis dette er en revisjonsforespørsel
    if history:
        messages.extend(history)
    else:
        messages.append({
            "role": "user",
            "content": (
                f"Fiverr-ordre: {order_id}\n"
                f"Kunde: {customer}\n\n"
                f"Oppgave:\n{task}\n\n"
                "Lever ferdig arbeid nå."
            )
        })

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    return resp.content[0].text

def revise_delivery(order_id, instruction):
    order = pending_orders.get(order_id)
    if not order:
        send_telegram(f"Fant ikke ordre {order_id}.")
        return

    log.info(f"Reviderer ordre {order_id}: {instruction}")

    # Bygg samtalehistorikk så Claude husker konteksten
    history = order.get("history", [])
    if not history:
        history = [{
            "role": "user",
            "content": (
                f"Fiverr-ordre: {order_id}\n"
                f"Kunde: {order['customer']}\n\n"
                f"Oppgave:\n{order['task']}\n\n"
                "Lever ferdig arbeid nå."
            )
        }]
    history.append({"role": "assistant", "content": order["delivery"]})
    history.append({"role": "user", "content": f"Vennligst gjor disse endringene: {instruction}"})

    new_delivery = solve_task(order["task"], order_id, order["customer"], history=history)

    # Oppdater ordre med ny leveranse og historikk
    history.append({"role": "assistant", "content": new_delivery})
    pending_orders[order_id]["delivery"] = new_delivery
    pending_orders[order_id]["history"] = history

    message = (
        f"REVIDERT LEVERANSE: {order_id}\n\n"
        f"--- NY LEVERANSE ---\n{new_delivery}\n--------------------\n\n"
        f"Svar med:\n"
        f"  ok {order_id}  →  godkjenn\n"
        f"  endre {order_id}: [ny instruksjon]  →  revider igjen"
    )
    send_telegram(message)
    log.info(f"Ordre {order_id} revidert og sendt til Telegram.")


# ─── BEHANDLE TELEGRAM-SVAR ──────────────────────────────────────────────────

def handle_telegram_updates():
    updates = get_telegram_updates()
    for update in updates:
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # Ignorer meldinger fra andre enn deg selv
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        if not text:
            continue

        log.info(f"Telegram-melding mottatt: {text}")

        log.info(f"Telegram: {text}")

        # /ny – manuell ordre direkte fra Telegram
        if text.lower().startswith("/ny"):
            lines = text[3:].strip().split("\n")
            task = ""
            customer = "Ukjent"
            order_id = f"MANUELL-{int(time.time())}"
            for line in lines:
                if line.lower().startswith("kunde:"):
                    customer = line.split(":", 1)[1].strip()
                elif line.lower().startswith("oppgave:"):
                    task = line.split(":", 1)[1].strip()
                elif line.lower().startswith("ordre:"):
                    order_id = line.split(":", 1)[1].strip()
            if not task:
                task = text[3:].strip()
            if not task:
                send_telegram(
                    "Bruk slik:\n"
                    "/ny\n"
                    "Kunde: JohnDoe\n"
                    "Oppgave: Beskriv hva som skal lages\n\n"
                    "Eller enkelt:\n"
                    "/ny Skriv en produktbeskrivelse for..."
                )
                continue
            send_telegram(f"Behandler manuell ordre {order_id}...")
            messages = [{
                "role": "user",
                "content": f"Fiverr-ordre: {order_id}\nKunde: {customer}\n\nOppgave:\n{task}\n\nLever ferdig arbeid na."
            }]
            delivery = claude(messages=messages)
            pending_orders[order_id] = {
                "task": task,
                "customer": customer,
                "delivery": delivery,
            }
            chat_history[order_id] = []
            active_order = order_id
            save_state()
            send_telegram(
                f"NY MANUELL ORDRE: {order_id}\n"
                f"Kunde: {customer}\n\n"
                f"--- LEVERANSE ---\n{delivery}\n-----------------\n\n"
                f"ok {order_id}  →  godkjenn\n"
                f"Eller chat fritt for endringer."
            )
            continue

        

        # /status - vis alle ventende ordrer
        if text.lower() == "/status":
            if not pending_orders:
                send_telegram("Ingen aktive ordrer.")
            else:
                lines = ["AKTIVE ORDRER:"]
                for oid, o in pending_orders.items():
                    lines.append(f"  {oid} - Kunde: {o['customer']}")
                send_telegram("\n".join(lines))
            continue

        # ok <order_id>
        ok_match = re.match(r"^ok\s+(\S+)", text, re.IGNORECASE)
        if ok_match:
            order_id = ok_match.group(1)
            if order_id in pending_orders:
                del pending_orders[order_id]
                send_telegram(
                    f"Ordre {order_id} godkjent!\n\n"
                    f"Husk a ga inn pa Fiverr og levere til kunden."
                )
            else:
                send_telegram(f"Fant ikke ordre {order_id}.")
            continue

        # endre <order_id>: <instruksjon>
        endre_match = re.match(r"^endre\s+(\S+):\s*(.+)", text, re.IGNORECASE | re.DOTALL)
        if endre_match:
            order_id = endre_match.group(1)
            instruction = endre_match.group(2).strip()
            revise_delivery(order_id, instruction)
            continue

                # Ukjent kommando
        send_telegram(
            "Kjente kommandoer:\n\n"
            "  /ny <oppgave>  →  send inn manuell ordre\n"
            "  /ny\n"
            "  Kunde: Navn\n"
            "  Oppgave: Beskriv oppgaven\n\n"
            "  /status  →  vis alle aktive ordrer\n"
            "  ok <ordre-id>  →  godkjenn og fullfør\n"
            "  endre <ordre-id>: <instruksjon>  →  revider leveranse\n"

# ─── PROSESSER NY E-POST ─────────────────────────────────────────────────────

def process(mail_data):
    log.info(f"Behandler: {mail_data['subject']}")
    details = extract_task(mail_data["subject"], mail_data["body"])

    if not details.get("is_order"):
        log.info("Ikke en ny ordre, hopper over.")
        return

    task     = details.get("task", "")
    order_id = details.get("order_id", "ukjent")
    customer = details.get("customer", "Kunde")

    if not task:
        log.warning("Ingen oppgave funnet.")
        return

    log.info(f"Ordre {order_id}: sender til Claude...")
    delivery = solve_task(task, order_id, customer)

    # Lagre ordre i minnet
    pending_orders[order_id] = {
        "task": task,
        "customer": customer,
        "delivery": delivery,
        "history": []
    }

    send_order_to_telegram(order_id, customer, task, delivery)
    log.info(f"Ordre {order_id} sendt til Telegram for godkjenning.")


# ─── HOVEDLØKKE ──────────────────────────────────────────────────────────────

def main():
    log.info("Fiverr Agent v4 starter. Sjekker hvert %s sek.", CHECK_INTERVAL)
    send_telegram(
        "Fiverr Agent v4 er online!\n\n"
        "Kommandoer:\n"
        "  ok <ordre-id>  →  godkjenn\n"
        "  endre <ordre-id>: <instruksjon>  →  revider\n"
        "  /status  →  vis aktive ordrer"
    )

    while True:
        try:
            # Sjekk Telegram-svar fra deg
            handle_telegram_updates()

            # Sjekk nye Fiverr-eposter
            mails = fetch_unseen_fiverr()
            for m in mails:
                process(m)

        except Exception as e:
            log.error(f"Feil: {type(e).__name__}: {e}", exc_info=True)

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
