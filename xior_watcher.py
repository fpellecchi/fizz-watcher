"""
Xior Rotsoord (Utrecht) availability watcher - GitHub Actions only.

Deliberately never run on the user's PC: it is deployed exclusively via
.github/workflows/xior-watch.yml.

How it works
------------
The "Let's book your room" modal on the residence page asks WordPress for
Yardi availability. The same request works without a browser:

    POST https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php
    action=yardi_room_availability&property_page_id=1105
    &room_type_id=<32286 Comfy | 32287 Deluxe>&semester_id=3281

  * No rooms -> {"success": true, "data": {"total": 0, "units": []}}
  * Rooms!   -> "total" > 0 and "units" carries apartmentName, minimumRent,
                availableDate and a direct applyOnlineURL per unit.

Alerts reuse the Fizz watcher's notification stack (Telegram spam until you
reply, plus ntfy push), so both watchers behave identically on your phone.

Run:  python xior_watcher.py           (continuous)
      python xior_watcher.py --once    (single check)
"""

import datetime
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

import fizz_watcher as fw

AJAX_URL = "https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php"
RESIDENCE_URL = ("https://www.xiorstudenthousing.eu/netherlands/utrecht/"
                 "rotsoord-student-accommodation/")

# Xior Rotsoord only - the user rated it the better of the two Utrecht
# residences and asked to watch just this one.
PROPERTY_PAGE_ID = 1105
SEMESTER_ID = 3281
ROOM_TYPES = {"Comfy": 32286, "Deluxe": 32287}

INTERVAL_SECONDS = 20
MAX_RUNTIME_MINUTES = float(os.environ.get("FIZZ_MAX_RUNTIME_MINUTES", "0"))


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [xior] {msg}",
          flush=True)


_checkin_sent_for = None


def daily_checkin():
    """Once a day (~09:00 Amsterdam) confirm this watcher is alive. Same
    idea as the Fizz one: if it stops arriving, something is wrong."""
    global _checkin_sent_for
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if _checkin_sent_for == today or now.hour != 9:
        return
    _checkin_sent_for = today
    fw.notify_telegram(
        "Xior Rotsoord watcher daily check-in ✅",
        f"Still alive and watching Xior Rotsoord every {INTERVAL_SECONDS}s. "
        "No action needed.")


def query_room_type(room_type_id):
    """Returns the list of available units for one room type."""
    data = urllib.parse.urlencode({
        "action": "yardi_room_availability",
        "property_page_id": PROPERTY_PAGE_ID,
        "room_type_id": room_type_id,
        "semester_id": SEMESTER_ID,
    }).encode()
    req = urllib.request.Request(
        AJAX_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/128.0 Safari/537.36",
            "Referer": RESIDENCE_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    if not payload.get("success"):
        raise RuntimeError(f"ajax returned success=false: {payload}")
    return payload.get("data", {}).get("units") or []


def check_availability():
    """Returns {room type name: [units]} for every type that has units."""
    found = {}
    for name, room_type_id in ROOM_TYPES.items():
        units = query_room_type(room_type_id)
        if units:
            found[name] = units
    return found


def describe(found):
    lines = []
    for name, units in found.items():
        rents = [u.get("minimumRent") for u in units if u.get("minimumRent")]
        dates = sorted({u.get("availableDate") for u in units
                        if u.get("availableDate")})
        lines.append(f"{name}: {len(units)} unit(s)"
                     + (f", from EUR {min(rents)}" if rents else "")
                     + (f", available {', '.join(dates[:3])}" if dates else ""))
    return " | ".join(lines)


def apply_link(found):
    for units in found.values():
        for u in units:
            if u.get("applyOnlineURL"):
                return u["applyOnlineURL"]
    return RESIDENCE_URL


def alert(found):
    # Never let formatting swallow an alert - fall back to a bare message.
    title = "XIOR ROTSOORD UTRECHT: ROOMS AVAILABLE!"
    try:
        summary = describe(found)
        link = apply_link(found)
        body = (f"{summary}. Apply NOW: {link}\n\nResidence page: "
                f"{RESIDENCE_URL}")
    except Exception as e:
        log(f"could not format alert details ({e}) - sending bare alert")
        summary, link = "Rooms available", RESIDENCE_URL
        body = f"Rooms available at Xior Rotsoord. Apply NOW: {RESIDENCE_URL}"

    fw.notify_push(title, body, link=link)
    # This watcher only runs on the PC, so use the desktop alarm too - a
    # second channel in case Telegram is unreachable.
    threading.Thread(
        target=fw.desktop_alarm,
        args=(f"XIOR ROTSOORD HAS ROOMS!\n\n{summary}\n\n"
              f"The page is already open. GO APPLY NOW!",
              "XIOR ROTSOORD ALERT", link),
        daemon=True).start()
    fw.telegram_spam_until_ack(title, body, link=link)


def main():
    once = "--once" in sys.argv
    if "--test" in sys.argv:
        log("DRILL: spamming telegram with real-alert wording until reply")
        title = "XIOR ROTSOORD UTRECHT: ROOMS AVAILABLE!"
        body = f"Studio available at Xior Rotsoord. Apply NOW: {RESIDENCE_URL}"
        fw.notify_push(title, body, link=RESIDENCE_URL)
        fw.telegram_spam_until_ack(title, body, link=RESIDENCE_URL)
        return 0

    if not once and not fw.HEADLESS:
        # Xior blocks datacenter IPs (GitHub Actions gets 403), so this
        # watcher only works from a home connection. Port differs from the
        # Fizz watcher's so the two locks never collide.
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lock.bind(("127.0.0.1", 51235))
        except OSError:
            log("another xior_watcher instance is already running - exiting")
            return 2

    mode = ("single check" if once else
            f"stint of {MAX_RUNTIME_MINUTES:g} min" if MAX_RUNTIME_MINUTES
            else "continuous, forever")
    log(f"Watching Xior Rotsoord (page {PROPERTY_PAGE_ID}) every "
        f"{INTERVAL_SECONDS}s ({mode})")

    errors = 0
    checks = 0
    found = {}          # stays defined even if the very first check fails
    known_types = set()
    started = time.time()
    alert_threads = []
    last_result = "starting up"
    while True:
        try:
            found = check_availability()
            errors = 0
            checks += 1
            daily_checkin()
            last_result = describe(found) if found else "no rooms"
            if found:
                new_types = set(found) - known_types
                if new_types:
                    log(f"AVAILABILITY DETECTED! {describe(found)}")
                    t = threading.Thread(target=alert, args=(found,),
                                         daemon=False)
                    t.start()
                    alert_threads.append(t)
                known_types |= set(found)
                if checks % 45 == 1:
                    log(f"check #{checks}: available: {describe(found)}")
            else:
                if known_types:
                    log("availability is gone again")
                known_types = set()
                if checks % 45 == 1:  # heartbeat roughly every 15 min
                    log(f"check #{checks}: still no availability")
        except Exception as e:
            errors += 1
            log(f"check failed ({errors} in a row): {e}")
            if errors in (30, 180):  # ~10 min / ~1 h of continuous failures
                log("WARNING: monitor has been failing for a while "
                    "(network down or the Xior API changed)")
                fw.notify_telegram(
                    "⚠️ Xior watcher is FAILING",
                    f"{errors} checks in a row failed ({e}). It keeps "
                    "retrying, but availability could be missed - tell "
                    "Claude to look into it.")
        # outside the try: a failed availability check must never stop the
        # watcher from answering "am I alive?". A different poll interval
        # from the Fizz watcher keeps the two from colliding on getUpdates.
        fw.handle_status_requests(
            "Xior Rotsoord watcher",
            f"Checking every {INTERVAL_SECONDS}s (cloud). "
            f"{checks} checks this run"
            + (f", {errors} failing" if errors else "")
            + f". Right now: {last_result}.",
            interval=25)
        if once:
            log("single check done")
            return 0 if found else 1
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
