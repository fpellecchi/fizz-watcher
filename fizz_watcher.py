"""
THE FIZZ Utrecht availability watcher.  (Standalone project - not ASMM.)

How it works
------------
The Fizz website widget ("Currently no Single/Double Studio apartments
available") is rendered from a public booking API. Instead of scraping the
page, this script asks that API directly every few seconds
(FIZZ_INTERVAL_SECONDS, default 1):

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

Run:  python fizz_watcher.py           (checks every 1s, forever)
      python fizz_watcher.py --once    (single availability check)
      python fizz_watcher.py --test    (send a test alert to all channels)
"""

import ctypes
import datetime
import http.client
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
# Rooms have been observed to last ~20s, so detection latency is what wins or
# loses them. Tunable via FIZZ_INTERVAL_SECONDS: the PC watcher runs fast (it
# is the one racing for a room), the cloud copy stays slower since it only has
# to cover the times the PC is off - that keeps the combined request rate
# reasonable. Xior began returning 429 at ~6 requests/min, so going much below
# a couple of seconds risks a block, and a blocked watcher sees nothing.
# Measured 2026-08-03: 60 consecutive requests at 1/s drew no 429 and no
# errors (median response 655 ms), so 1s is within what Fizz tolerates.
# 0.7s is the technical floor (the request itself takes ~0.65s), but 2s is
# the setting we hold: against a ~20s room lifetime the difference is
# ~0.65s of detection, while the traffic drops from ~85 to ~30 req/min.
# Bursts get tolerated; sustained load is what gets noticed and blocked,
# and a blocked watcher sees nothing at all.
INTERVAL_SECONDS = float(os.environ.get("FIZZ_INTERVAL_SECONDS", "2"))
MAX_INTERVAL_SECONDS = 300
# keep the log readable whatever the interval is
HEARTBEAT_EVERY = max(1, int(900 / max(INTERVAL_SECONDS, 1)))

PAYLOAD = json.dumps({
    "bookingTypes": [],
    "categories": [
        {"lookupValue": "BUILDING", "tags": [{"lookupValue": BUILDING}]}
    ],
}).encode()


def log(msg):
    # millisecond precision: alert latency is measured in tens of ms now
    line = f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + f"] {msg}"
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


class RateLimited(Exception):
    """The site asked us to slow down (429). Not a failure - a throttle."""

    def __init__(self, retry_after=None):
        super().__init__(f"rate limited (retry after {retry_after or '?'}s)")
        self.retry_after = retry_after


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
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry = (e.headers or {}).get("Retry-After")
            raise RateLimited(int(retry) if str(retry or "").isdigit() else None)
        raise

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


_tg_conn = None
_tg_lock = threading.Lock()


def tg_api(token, method, params=None, timeout=10):
    """Call the Telegram API over a kept-alive TLS connection.

    A fresh HTTPS connection costs ~100 ms (handshake); reusing a warm one
    costs ~18 ms. Since the status poll hits this every few seconds, the
    connection is already open when an alert fires - so the message leaves
    almost immediately. Any error drops the connection and retries once on
    a fresh one, so a broken socket can never swallow an alert."""
    global _tg_conn
    body = urllib.parse.urlencode(params or {})
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Connection": "keep-alive"}
    with _tg_lock:
        for attempt in (1, 2):
            try:
                if _tg_conn is None:
                    _tg_conn = http.client.HTTPSConnection(
                        "api.telegram.org", timeout=timeout)
                _tg_conn.request("POST", f"/bot{token}/{method}", body, headers)
                return json.loads(_tg_conn.getresponse().read())
            except Exception:
                try:
                    if _tg_conn:
                        _tg_conn.close()
                except Exception:
                    pass
                _tg_conn = None
                if attempt == 2:
                    raise


def book_button(link):
    """A tappable BOOK NOW button under the message - one tap, instead of
    hunting for a URL inside the text."""
    if not link:
        return {}
    return {"reply_markup": json.dumps({"inline_keyboard": [[
        {"text": "🔴 BOOK NOW 🔴", "url": link}]]})}


def notify_telegram(title, body, link=None):
    cfg = discover_telegram_chat_id(load_config())
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        log("telegram not configured yet (fill fizz_config.json)")
        return
    try:
        ok = tg_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": f"🚨🚨 {title} 🚨🚨\n\n{body}",
            **book_button(link),
        }).get("ok")
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
        for u in tg_api(token, "getUpdates").get("result", []):
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            if str(chat.get("id")) == str(chat_id) and \
                    msg.get("date", 0) >= since_ts:
                return True
    except Exception as e:
        log(f"telegram ack check failed: {e}")
    return False


def telegram_spam_until_ack(title, body, link=None):
    """Send the alert over and over until the user replies to the bot
    (or SPAM_MAX_MINUTES pass). Reply with anything - 'ok', 'seen' - to
    stop it. `link` is the booking URL quoted once acknowledged."""
    link = link or BOOK_URL
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
                            "Now GO BOOK: " + link, link=link)
            log(f"telegram spam acked by user after {n} messages")
            return
        n += 1
        text = body if n > 1 else (body + "\n\nReply anything to this bot "
                                   "to stop the repeated alerts.")
        try:
            tg_api(token, "sendMessage", {
                "chat_id": chat_id,
                "text": f"🚨 {title} (alert {n})\n\n{text}",
                **book_button(link),
            })
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


_status_seen = set()
_status_started = time.time()
_status_last_poll = 0.0


def handle_status_requests(label, detail="", interval=15):
    """Answer a 'status' message from the user, so they can verify from
    their phone that this watcher is alive. Each running watcher replies
    separately - two replies means both are healthy.

    Telegram serves concurrent getUpdates calls inconsistently, so several
    watchers polling at once can miss a message. Two defences: each watcher
    polls on its own cadence (`interval`) so they drift apart, and messages
    stay eligible until answered - a missed poll is picked up by the next
    one. Only messages sent after this process started count, so a restart
    never re-answers an old request."""
    global _status_last_poll
    now = time.time()
    if now - _status_last_poll < interval:
        return
    _status_last_poll = now
    cfg = load_config()
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return
    try:
        updates = tg_api(token, "getUpdates").get("result", [])
    except Exception as e:
        log(f"status poll failed: {e}")
        return

    for u in updates:
        uid = u.get("update_id")
        if uid in _status_seen:
            continue
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            continue
        if msg.get("date", 0) < _status_started - 5:
            _status_seen.add(uid)  # predates this run - never answer it
            continue
        if (msg.get("text") or "").strip().lower().lstrip("/") in (
                "status", "alive", "ping", "check"):
            _status_seen.add(uid)
            log(f"status request answered ({label})")
            notify_telegram(f"{label}: ALIVE ✅", detail)


def notify_push(title, body, link=None):
    # Click = tapping the notification opens the booking page directly from
    # the lock screen; Actions adds an explicit BOOK NOW button. This is the
    # fastest path there is - no app to open, no message to find.
    link = link or BOOK_URL
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=body.encode(),
            headers={"Title": title, "Priority": "urgent",
                     "Tags": "rotating_light,house",
                     "Click": link,
                     "Actions": f"view, BOOK NOW, {link}, clear=true"},
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


def desktop_alarm(text, title, link):
    """Beep + always-on-top box + open the booking page. No-op on a server.
    Every step is guarded: a desktop that will not co-operate must never
    stop the phone alerts, which are the ones that matter."""
    if HEADLESS:
        return
    try:
        webbrowser.open(link)
    except Exception as e:
        log(f"could not open browser: {e}")
    stop = threading.Event()
    try:
        threading.Thread(target=beep_forever, args=(stop,), daemon=True).start()
        MB_SYSTEMMODAL, MB_ICONEXCLAMATION, MB_SETFOREGROUND = (
            0x1000, 0x30, 0x10000)
        ctypes.windll.user32.MessageBoxW(
            0, text, title,
            MB_SYSTEMMODAL | MB_ICONEXCLAMATION | MB_SETFOREGROUND)
    except Exception as e:
        log(f"desktop alarm failed: {e}")
    finally:
        stop.set()


def alert(details):
    # Formatting must never be able to swallow an alert: if the API returns
    # a shape we did not expect, fall back to a bare message rather than
    # raising and leaving the user with silence.
    title = "FIZZ UTRECHT: ROOMS AVAILABLE!"
    try:
        rooms = (details or {}).get("roomTypes") or {}
        rooms_txt = ", ".join(f"{k} x{v}" for k, v in rooms.items())
        pr = (details or {}).get("priceRange") or {}
        dates = ", ".join((details or {}).get("startDates") or [])
        body = (f"{rooms_txt or 'Rooms'} available at THE FIZZ Utrecht. "
                f"Price {pr.get('low', '?')}-{pr.get('high', '?')} "
                f"{pr.get('unit', '')}. Start dates: {dates}. "
                f"Book NOW: {BOOK_URL}")
        popup = (f"THE FIZZ UTRECHT HAS ROOMS AVAILABLE!\n\n"
                 f"Room types: {rooms_txt or 'unknown'}\n"
                 f"Price: {pr.get('low', '?')} - {pr.get('high', '?')} "
                 f"{pr.get('unit', '')}\nStart dates: {dates}\n\n"
                 f"The booking page is already open. GO BOOK NOW!")
    except Exception as e:
        log(f"could not format alert details ({e}) - sending bare alert")
        body = f"Rooms available at THE FIZZ Utrecht. Book NOW: {BOOK_URL}"
        popup = body

    # Telegram first and over the warm connection - it is the channel the
    # user actually watches. Push and email go out on their own threads so
    # neither can delay it. Every millisecond here is a millisecond of the
    # room's lifetime, which has been measured as low as 2 seconds.
    threading.Thread(target=telegram_spam_until_ack, args=(title, body),
                     kwargs={"link": BOOK_URL}, daemon=False).start()
    threading.Thread(target=notify_push, args=(title, body),
                     kwargs={"link": BOOK_URL}, daemon=True).start()
    threading.Thread(target=notify_email, args=(title, body),
                     daemon=True).start()

    try:
        with open(ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump({"detectedAt": datetime.datetime.now().isoformat(),
                       **(details or {})}, f, indent=2)
    except Exception as e:
        log(f"could not write alert file: {e}")

    desktop_alarm(popup, "FIZZ UTRECHT ALERT", BOOK_URL)


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
    interval = INTERVAL_SECONDS
    throttled_since = None
    while True:
        cycle_start = time.time()
        try:
            available, details = check_availability()
            errors = 0
            checks += 1
            if throttled_since:
                log("rate limit cleared")
                throttled_since = None
            if interval > INTERVAL_SECONDS:  # ease back toward full speed
                interval = max(INTERVAL_SECONDS, interval * 0.8)
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
                if checks % HEARTBEAT_EVERY == 1:
                    log(f"check #{checks}: still available: {details['roomTypes']}")
            else:
                if was_available:
                    log("availability is gone again")
                known_types = set()
                if checks % HEARTBEAT_EVERY == 1:  # ~every 15 min
                    log(f"check #{checks}: still no availability")
            was_available = available
        except RateLimited as e:
            # Not a failure: Fizz is fine, we are just asking too often.
            throttled_since = throttled_since or time.time()
            interval = min(MAX_INTERVAL_SECONDS,
                           e.retry_after or max(interval * 2, INTERVAL_SECONDS * 2))
            stuck_for = time.time() - throttled_since
            log(f"rate limited - slowing to {interval:.0f}s "
                f"(throttled for {stuck_for / 60:.0f} min)")
            if 3600 <= stuck_for < 3600 + interval:  # ~1 h with no let-up
                notify_telegram(
                    "⚠️ Fizz watcher throttled for an hour",
                    "Fizz keeps answering 429 (too many requests). Still "
                    "retrying, more slowly - availability could be missed.")
        except Exception as e:
            errors += 1
            log(f"check failed ({errors} in a row): {e}")
            if errors in (HEARTBEAT_EVERY // 2, HEARTBEAT_EVERY * 4):  # ~7 min / ~1 h
                log("WARNING: monitor has been failing for a while "
                    "(network down or API changed)")
                notify_telegram(
                    "⚠️ Fizz watcher is FAILING",
                    f"{errors} checks in a row failed ({e}). It keeps "
                    "retrying, but availability could be missed - tell "
                    "Claude to look into it.")
        # outside the try: a failed availability check must never stop the
        # watcher from answering "am I alive?"
        handle_status_requests(
            "Fizz Utrecht watcher",
            f"Checking every {interval:.0f}s "
            f"({'cloud' if HEADLESS else 'your PC'}). "
            f"{checks} checks this run"
            + (f", {errors} failing" if errors else "")
            + f". Right now: {'ROOMS AVAILABLE!' if available else 'no rooms'}.",
            interval=4)
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
        # pace on the cycle, not after it: the request itself takes ~0.65s,
        # so sleeping the full interval would nearly double the real gap
        time.sleep(max(0.0, interval - (time.time() - cycle_start)))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        log(f"FATAL: watcher crashed: {type(e).__name__}: {e}")
        raise
