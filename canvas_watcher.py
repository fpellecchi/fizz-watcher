"""
Canvas Utrecht (Greystar) availability watcher.

How it works
------------
Canvas books through Yardi RentCafe. Their applicant portal lists every
available apartment on one page:

    https://canvas-student.securerc.co.uk/onlineleasing/canvas-utrecht/floorplans.aspx

While nothing is free the page carries the sentence
"Floor plan details not available for this property", plus Canvas's own
note: "Apartments will appear on this page as soon as they become
available."

Detection is deliberately FAIL-LOUD: that sentence is treated as the only
proof of "no rooms". If it disappears - because units appeared, or because
the page changed - you get alerted. A false alarm you can dismiss beats
silence while a room slips by.

The portal rejects bare requests (403); it needs an ordinary browser header
set, which is what BROWSER_HEADERS below sends.

Run:  python canvas_watcher.py           (continuous)
      python canvas_watcher.py --once    (single check)
      python canvas_watcher.py --test    (drill alert)
"""

import datetime
import gzip
import io
import os
import re
import socket
import sys
import threading
import time
import urllib.request
import zlib

import fizz_watcher as fw

PORTAL = ("https://canvas-student.securerc.co.uk/onlineleasing/"
          "canvas-utrecht/floorplans.aspx")
INFO_URL = ("https://www.canvas-world.com/en/locations/netherlands/utrecht/"
            "canvas-utrecht")

# Present only while nothing is available.
NO_AVAILABILITY_MARKER = "floor plan details not available for this property"

INTERVAL_SECONDS = 20
MAX_RUNTIME_MINUTES = float(os.environ.get("FIZZ_MAX_RUNTIME_MINUTES", "0"))

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.canvas-world.com/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Upgrade-Insecure-Requests": "1",
}


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [canvas] {msg}",
          flush=True)


def fetch_portal():
    req = urllib.request.Request(PORTAL, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif enc == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    page = raw.decode("utf-8", errors="replace")
    low = page.lower()
    # Two sanity checks before the fail-loud availability test below. If the
    # portal serves something else entirely (maintenance page, block page,
    # redesign), treat it as an ERROR - which surfaces as the "watcher is
    # failing" warning - rather than as availability, which would fire a
    # false 3am alarm.
    if "canvas utrecht" not in low or "floor plan" not in low:
        raise RuntimeError(f"unexpected page ({len(page)} chars) - not the "
                           f"Canvas floorplans portal")
    return page


def find_units(page):
    """Best-effort extraction of unit/price details for the alert text."""
    text = re.sub(r"<[^>]+>", " ", page)
    text = re.sub(r"\s+", " ", text)
    prices = re.findall(r"€\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?\b", text)
    plans = re.findall(r"((?:Classic|Standard|Premium|Deluxe)[A-Za-z ]{0,24}"
                       r"Studio[A-Za-z\- ]{0,16})", text)
    bits = []
    if plans:
        bits.append("plans: " + ", ".join(sorted(set(plans))[:4]))
    if prices:
        bits.append("prices: " + ", ".join(sorted(set(prices))[:4]))
    return "; ".join(bits)


def check_availability(page):
    """True when the 'nothing available' sentence is gone."""
    return NO_AVAILABILITY_MARKER not in page.lower()


def alert(detail):
    title = "CANVAS UTRECHT: ROOMS AVAILABLE!"
    try:
        body = (f"The Canvas Utrecht booking portal is showing apartments"
                + (f" ({detail})" if detail else "")
                + f". Book NOW: {PORTAL}\n\nInfo: {INFO_URL}")
    except Exception as e:
        log(f"could not format alert details ({e}) - sending bare alert")
        detail, body = "", f"Apartments at Canvas Utrecht. Book NOW: {PORTAL}"

    fw.notify_push(title, body, link=PORTAL)
    # PC-only watcher: desktop alarm as a second channel.
    threading.Thread(
        target=fw.desktop_alarm,
        args=(f"CANVAS UTRECHT HAS APARTMENTS!\n\n{detail or 'See portal'}\n\n"
              f"The portal is already open. GO BOOK NOW!",
              "CANVAS UTRECHT ALERT", PORTAL),
        daemon=True).start()
    fw.telegram_spam_until_ack(title, body, link=PORTAL)


_checkin_sent_for = None


def daily_checkin():
    global _checkin_sent_for
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if _checkin_sent_for == today or now.hour != 9:
        return
    _checkin_sent_for = today
    fw.notify_telegram(
        "Canvas Utrecht watcher daily check-in ✅",
        f"Still alive, checking every {INTERVAL_SECONDS}s. No action needed.")


def main():
    once = "--once" in sys.argv
    if "--test" in sys.argv:
        log("DRILL: spamming telegram with real-alert wording until reply")
        title = "CANVAS UTRECHT: ROOMS AVAILABLE!"
        body = f"Studio available at Canvas Utrecht. Book NOW: {PORTAL}"
        fw.notify_push(title, body, link=PORTAL)
        fw.telegram_spam_until_ack(title, body, link=PORTAL)
        return 0

    if not once and not fw.HEADLESS:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lock.bind(("127.0.0.1", 51237))
        except OSError:
            log("another canvas_watcher instance is already running - exiting")
            return 2

    mode = ("single check" if once else
            f"stint of {MAX_RUNTIME_MINUTES:g} min" if MAX_RUNTIME_MINUTES
            else "continuous, forever")
    log(f"Watching Canvas Utrecht every {INTERVAL_SECONDS}s ({mode})")

    errors = 0
    checks = 0
    available = False
    was_available = False
    started = time.time()
    alert_threads = []
    last_result = "starting up"
    while True:
        try:
            page = fetch_portal()
            available = check_availability(page)
            errors = 0
            checks += 1
            daily_checkin()
            detail = find_units(page) if available else ""
            last_result = ("AVAILABLE - " + (detail or "see portal")
                           if available else "no rooms")
            if available and not was_available:
                log(f"AVAILABILITY DETECTED! {detail}")
                t = threading.Thread(target=alert, args=(detail,),
                                     daemon=False)
                t.start()
                alert_threads.append(t)
            elif was_available and not available:
                log("availability is gone again")
            was_available = available
            if checks % 45 == 1:  # heartbeat roughly every 15 min
                log(f"check #{checks}: {last_result}")
        except Exception as e:
            errors += 1
            log(f"check failed ({errors} in a row): {e}")
            if errors in (30, 180):  # ~10 min / ~1 h of continuous failures
                log("WARNING: monitor has been failing for a while")
                fw.notify_telegram(
                    "⚠️ Canvas watcher is FAILING",
                    f"{errors} checks in a row failed ({e}). It keeps "
                    "retrying, but availability could be missed.")
        fw.handle_status_requests(
            "Canvas Utrecht watcher",
            f"Checking every {INTERVAL_SECONDS}s. {checks} checks this run"
            + (f", {errors} failing" if errors else "")
            + f". Right now: {last_result}.",
            interval=45)
        if once:
            log("single check done")
            return 0 if available else 1
        if MAX_RUNTIME_MINUTES and \
                time.time() - started > MAX_RUNTIME_MINUTES * 60:
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
