"""
THE FIZZ Utrecht availability watcher.  (Standalone project - not ASMM.)

How it works
------------
The Fizz website widget ("Currently no Single/Double Studio apartments
available") is rendered from a public booking API. Instead of scraping the
page, this script asks that API directly every 20 seconds:

    POST https://booking.the-fizz.com/json-interface/rs/progressiveSearch/form
    body: {"bookingTypes": [], "categories": [
            {"lookupValue": "BUILDING", "tags": [{"lookupValue": "FIZZ_UTRECHT"}]}]}

  * No rooms  -> {"status": "ERROR", messages: ["No price ranges found", ...]}
  * Rooms!    -> {"status": "OK", "priceRange": {...}, "startRanges": [...],
                  categories PROPERTYTYPE -> [("SINGLE", n), ("DOUBLE", n)]}

When rooms appear this script (forever, re-alerting on every new event):
  1. sends a Telegram message via your bot     (fizz_config.json)
  2. sends an ntfy.sh push notification        (topic below)
  3. sends an email                            (optional, fizz_config.json)
  4. opens the Fizz booking page in your browser
  5. beeps loudly until you dismiss an always-on-top alert box
  6. writes fizz_alert.json + fizz_watcher.log

Only one instance can run at a time (localhost port 51234 acts as a lock).

Run:  python fizz_watcher.py           (checks every 20s, forever)
      python fizz_watcher.py --once    (single availability check)
      python fizz_watcher.py --test    (send a test alert to all channels)
"""

import ctypes
import datetime
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "fizz_config.json")
LOG_FILE = os.path.join(BASE_DIR, "fizz_watcher.log")
ALERT_FILE = os.path.join(BASE_DIR, "fizz_alert.json")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "fizz_heartbeat.txt")

NTFY_TOPIC = "fizz-utrecht-pelle-x7k2m9"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

API_URL = "https://booking.the-fizz.com/json-interface/rs/progressiveSearch/form"
BOOK_URL = ("https://www.the-fizz.com/en/search-nl/#/"
            "searchcriteria=BUILDING:FIZZ_UTRECHT;AREA:UTRECHT;")
BUILDING = "FIZZ_UTRECHT"
INTERVAL_SECONDS = 20

PAYLOAD = json.dumps({
    "bookingTypes": [],
    "categories": [
        {"lookupValue": "BUILDING", "tags": [{"lookupValue": BUILDING}]}
    ],
}).encode()


def log(msg):
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


IS_WINDOWS = os.name == "nt"
# GitHub Actions caps a job at 6h, so the workflow runs the watcher in
# bounded stints and queues a successor. 0 = run forever (PC / Railway).
MAX_RUNTIME_MINUTES = float(os.environ.get("FIZZ_MAX_RUNTIME_MINUTES", "0"))
# In the cloud there is no desktop: no beeping, no popup, no browser.
# Telegram/push/email carry the alert instead.
HEADLESS = not IS_WINDOWS or os.environ.get("FIZZ_HEADLESS") == "1"


def load_config():
    """Config comes from environment variables when deployed (Railway etc.),
    falling back to fizz_config.json for local runs."""
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        cfg["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    return cfg


def save_config(cfg):
    """Persist discovered values locally. On a read-only/ephemeral cloud
    filesystem this is best-effort only - the env vars are the source of
    truth there."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        log(f"could not save config (fine in cloud): {e}")


def discover_telegram_chat_id(cfg):
    """If the bot token is set but chat_id is empty, find the chat id from
    the last message sent to the bot (you must have pressed START and sent
    it any message first). Saves it back into fizz_config.json."""
    token = (cfg.get("telegram_bot_token") or "").strip()
    if not token or (cfg.get("telegram_chat_id") or "").strip():
        return cfg
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as r:
            updates = json.load(r).get("result", [])
        for u in reversed(updates):
            msg = u.get("message") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                cfg["telegram_chat_id"] = str(chat["id"])
                save_config(cfg)
                log(f"telegram chat id discovered and saved "
                    f"(chat with {chat.get('first_name') or chat.get('title')})")
                return cfg
        log("telegram: token set but no chat found - open your bot in "
            "Telegram, press START and send it any message, then restart")
    except Exception as e:
        log(f"telegram chat discovery failed: {e}")
    return cfg


def check_availability():
    """Returns (available: bool, details: dict)."""
    req = urllib.request.Request(
        API_URL,
        data=PAYLOAD,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    if data.get("status") != "OK":
        return False, {}

    details = {"priceRange": data.get("priceRange"),
               "startDates": [r.get("from") for r in data.get("startRanges", [])],
               "roomTypes": {}}
    for cat in data.get("categories", []):
        if cat.get("lookupValue") == "PROPERTYTYPE":
            for tag in cat.get("tags") or []:
                details["roomTypes"][tag.get("lookupValue")] = tag.get("count")
    return True, details


def notify_telegram(title, body):
    cfg = discover_telegram_chat_id(load_config())
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        log("telegram not configured yet (fill fizz_config.json)")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": f"🚨🚨 {title} 🚨🚨\n\n{body}",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = json.load(r).get("ok")
        log("telegram alert sent" if ok else "telegram alert NOT ok")
    except Exception as e:
        log(f"telegram alert failed: {e}")


# Spam as fast as Telegram allows (~1 msg/sec per chat, enforced by their
# API): burst phase first, then slower until acknowledged.
SPAM_BURST_INTERVAL = 1.5   # seconds between messages during the burst
SPAM_BURST_COUNT = 40       # ~1 minute of near-continuous buzzing
SPAM_SLOW_INTERVAL = 10     # after the burst, until acked
SPAM_MAX_MINUTES = 30


def telegram_acked(token, chat_id, since_ts):
    """True if the user sent the bot any message after since_ts."""
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as r:
            for u in json.load(r).get("result", []):
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                if str(chat.get("id")) == str(chat_id) and \
                        msg.get("date", 0) >= since_ts:
                    return True
    except Exception as e:
        log(f"telegram ack check failed: {e}")
    return False


def telegram_spam_until_ack(title, body):
    """Send the alert over and over until the user replies to the bot
    (or SPAM_MAX_MINUTES pass). Reply with anything - 'ok', 'seen' - to
    stop it."""
    cfg = discover_telegram_chat_id(load_config())
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        log("telegram not configured yet (fill fizz_config.json)")
        return
    start = time.time()
    n = 0
    while time.time() - start < SPAM_MAX_MINUTES * 60:
        in_burst = n < SPAM_BURST_COUNT
        # ack checks cost an API call each - during the burst only check
        # every 5th message so sending stays at full speed
        if n > 0 and (not in_burst or n % 5 == 0) and \
                telegram_acked(token, chat_id, int(start)):
            notify_telegram("Acknowledged ✅",
                            "You replied - stopping the alert spam. "
                            "Now GO BOOK: " + BOOK_URL)
            log(f"telegram spam acked by user after {n} messages")
            return
        n += 1
        text = body if n > 1 else (body + "\n\nReply anything to this bot "
                                   "to stop the repeated alerts.")
        try:
            data = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": f"🚨 {title} (alert {n})\n\n{text}",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited - honor Telegram's retry delay
                try:
                    wait = json.load(e).get("parameters", {}).get("retry_after", 5)
                except Exception:
                    wait = 5
                log(f"telegram rate limit hit, backing off {wait}s")
                time.sleep(wait)
                continue
            log(f"telegram spam send failed: {e}")
        except Exception as e:
            log(f"telegram spam send failed: {e}")
        time.sleep(SPAM_BURST_INTERVAL if in_burst else SPAM_SLOW_INTERVAL)
    log(f"telegram spam gave up unacked after {n} messages")


def notify_push(title, body):
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=body.encode(),
            headers={"Title": title, "Priority": "urgent",
                     "Tags": "rotating_light,house", "Click": BOOK_URL},
        )
        urllib.request.urlopen(req, timeout=15)
        log("push notification sent")
    except Exception as e:
        log(f"push notification failed: {e}")


def notify_email(title, body):
    """Optional. Add to fizz_config.json (use a Gmail App Password from
    myaccount.google.com/apppasswords - NOT your real password):

        "email": {"smtp_host": "smtp.gmail.com", "smtp_port": 465,
                  "username": "you@gmail.com", "app_password": "xxxx",
                  "to": "you@gmail.com"}
    """
    cfg = (load_config().get("email") or {})
    if not cfg.get("username") or not cfg.get("app_password"):
        return
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = cfg["username"]
        msg["To"] = cfg.get("to", cfg["username"])
        msg.set_content(body)
        with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"),
                              int(cfg.get("smtp_port", 465)), timeout=20) as s:
            s.login(cfg["username"], cfg["app_password"].replace(" ", ""))
            s.send_message(msg)
        log("email alert sent")
    except Exception as e:
        log(f"email alert failed: {e}")


_checkin_sent_for = None


def daily_checkin(checks):
    """Once per day (~09:00 local) send a Telegram 'still alive' message.
    If it ever stops arriving, something is wrong with the watcher, the
    host, or the network - go check."""
    global _checkin_sent_for
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if _checkin_sent_for == today:
        return
    if HEADLESS:
        # Ephemeral filesystem (GitHub Actions): nothing persists between
        # job restarts, so limit check-ins to the 09:00 hour instead.
        if now.hour != 9:
            return
    else:
        if now.hour < 9:
            return
        try:
            with open(HEARTBEAT_FILE, encoding="utf-8") as f:
                if f.read().strip() == today:
                    return
        except OSError:
            pass
        try:
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(today)
        except OSError:
            return
    _checkin_sent_for = today
    notify_telegram(
        "Fizz watcher daily check-in ✅",
        f"Still alive and watching THE FIZZ Utrecht every "
        f"{INTERVAL_SECONDS}s ({checks} checks since last restart). "
        f"No action needed. If this message ever stops coming, "
        f"check the watcher on your PC.")


def beep_forever(stop_event):
    import winsound
    while not stop_event.is_set():
        winsound.Beep(1500, 400)
        winsound.Beep(1000, 400)
        time.sleep(0.3)


def alert(details):
    rooms_txt = ", ".join(f"{k} x{v}" for k, v in details["roomTypes"].items())
    pr = details.get("priceRange") or {}
    title = "FIZZ UTRECHT: ROOMS AVAILABLE!"
    body = (f"{rooms_txt or 'Rooms'} available at THE FIZZ Utrecht. "
            f"Price {pr.get('low', '?')}-{pr.get('high', '?')} {pr.get('unit', '')}. "
            f"Start dates: {', '.join(details.get('startDates') or [])}. "
            f"Book NOW: {BOOK_URL}")
    # Telegram spams repeatedly until the user replies; push/email fire once.
    threading.Thread(target=telegram_spam_until_ack, args=(title, body),
                     daemon=False).start()
    notify_push(title, body)
    notify_email(title, body)

    try:
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump({"detectedAt": datetime.datetime.now().isoformat(),
                       **details}, f, indent=2)
    except OSError as e:
        log(f"could not write alert file: {e}")

    if HEADLESS:  # cloud/server: no desktop to beep at
        return

    webbrowser.open(BOOK_URL)

    stop = threading.Event()
    threading.Thread(target=beep_forever, args=(stop,), daemon=True).start()

    price = details.get("priceRange") or {}
    text = (f"THE FIZZ UTRECHT HAS ROOMS AVAILABLE!\n\n"
            f"Room types: {rooms_txt or 'unknown'}\n"
            f"Price: {price.get('low', '?')} - {price.get('high', '?')} {price.get('unit', '')}\n"
            f"Start dates: {', '.join(details.get('startDates') or [])}\n\n"
            f"The booking page is already open in your browser. GO BOOK NOW!")
    MB_SYSTEMMODAL, MB_ICONEXCLAMATION, MB_SETFOREGROUND = 0x1000, 0x30, 0x10000
    ctypes.windll.user32.MessageBoxW(
        0, text, "FIZZ UTRECHT ALERT",
        MB_SYSTEMMODAL | MB_ICONEXCLAMATION | MB_SETFOREGROUND)
    stop.set()


def acquire_single_instance_lock():
    """Bind a localhost port as a cross-process lock. Returns the socket
    (must stay referenced) or None if another instance already runs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 51234))
        return s
    except OSError:
        return None


def main():
    if "--test" in sys.argv:
        # Drill with real-alert wording (no "test" label) so the phone's
        # notification ranking learns these messages are important.
        log("DRILL: spamming telegram with real-alert wording until reply")
        title = "FIZZ UTRECHT: ROOMS AVAILABLE!"
        body = ("Studio available at THE FIZZ Utrecht. "
                f"Book NOW: {BOOK_URL}")
        notify_push(title, body)
        telegram_spam_until_ack(title, body)
        return 0

    once = "--once" in sys.argv
    if not once and not HEADLESS:
        lock = acquire_single_instance_lock()
        if lock is None:
            log("another fizz_watcher instance is already running - exiting")
            return 2
    if not once:
        discover_telegram_chat_id(load_config())
    if once:
        mode = "single check"
    elif MAX_RUNTIME_MINUTES:
        mode = f"stint of {MAX_RUNTIME_MINUTES:g} min"
    else:
        mode = "continuous, forever"
    log(f"Watching {BUILDING} every {INTERVAL_SECONDS}s ({mode})")
    errors = 0
    checks = 0
    available = False
    was_available = False
    known_types = set()
    started = time.time()
    alert_threads = []
    while True:
        try:
            available, details = check_availability()
            errors = 0
            checks += 1
            daily_checkin(checks)
            if available:
                new_types = set(details["roomTypes"]) - known_types
                if not was_available or new_types:
                    log(f"AVAILABILITY DETECTED! {details}")
                    t = threading.Thread(target=alert, args=(details,),
                                         daemon=False)
                    t.start()
                    alert_threads.append(t)
                known_types |= set(details["roomTypes"])
                if checks % 45 == 1:
                    log(f"check #{checks}: still available: {details['roomTypes']}")
            else:
                if was_available:
                    log("availability is gone again")
                known_types = set()
                if checks % 45 == 1:  # heartbeat roughly every 15 min
                    log(f"check #{checks}: still no availability")
            was_available = available
        except Exception as e:
            errors += 1
            log(f"check failed ({errors} in a row): {e}")
            if errors in (30, 180):  # ~10 min / ~1 h of continuous failures
                log("WARNING: monitor has been failing for a while "
                    "(network down or API changed)")
        if once:
            log("single check done")
            return 0 if available else 1
        if MAX_RUNTIME_MINUTES and \
                time.time() - started > MAX_RUNTIME_MINUTES * 60:
            # Bounded stint (GitHub Actions): let any in-flight alert spam
            # finish, then exit so the queued successor job takes over.
            for t in alert_threads:
                t.join()
            log(f"runtime limit reached after {checks} checks - handing over "
                f"to the next run")
            return 0
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        log(f"FATAL: watcher crashed: {type(e).__name__}: {e}")
        raise
