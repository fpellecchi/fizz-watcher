"""
Plaza Resident Services (Utrecht) new-listing watcher.

How it works
------------
Plaza's "available places" portal renders from a public JSON feed:

    GET https://plaza.newnewnew.space/portal/object/frontend/getallobjects/format/json
    -> {"result": [ {id, street, houseNumber, city, dwellingType,
                     totalRent, areaDwelling, availableFrom, urlKey, ...}, ... ]}

Unlike Fizz and Xior, Plaza usually has listings up already (29 Utrecht
studios when this was written), so "something exists" is not news. This
watcher records what is there when it starts, then alerts only when a
*new* listing id appears for the watched city.

Run:  python plaza_watcher.py           (continuous)
      python plaza_watcher.py --once    (single check, prints what is live)
      python plaza_watcher.py --test    (drill alert)
"""

import datetime
import json
import os
import socket
import sys
import threading
import time
import urllib.request

import fizz_watcher as fw

FEED_URL = ("https://plaza.newnewnew.space/portal/object/frontend/"
            "getallobjects/format/json")
LISTING_URL = "https://plaza.newnewnew.space/en/availables-places/living-place"
DETAIL_URL = LISTING_URL + "/details/{urlKey}"

CITY = "utrecht"
INTERVAL_SECONDS = 20
MAX_RUNTIME_MINUTES = float(os.environ.get("FIZZ_MAX_RUNTIME_MINUTES", "0"))
STATE_FILE = os.path.join(fw.BASE_DIR, "plaza_seen.json")


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [plaza] {msg}",
          flush=True)


_checkin_sent_for = None


def daily_checkin(live_count):
    """Once a day (~09:00 Amsterdam) confirm this watcher is alive - the
    same signal the Fizz and Xior watchers send. Without it, silence is
    ambiguous: nothing new, or quietly dead?"""
    global _checkin_sent_for
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    if _checkin_sent_for == today or now.hour != 9:
        return
    _checkin_sent_for = today
    fw.notify_telegram(
        "Plaza Utrecht watcher daily check-in ✅",
        f"Still alive, checking every {INTERVAL_SECONDS}s. "
        f"{live_count} Utrecht listing(s) currently up; you are only "
        "alerted when a NEW one appears. No action needed.")


def fetch_listings():
    """Returns the listings for the watched city, keyed by listing id."""
    req = urllib.request.Request(
        FEED_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/128.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": LISTING_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)

    out = {}
    for row in payload.get("result") or []:
        city = row.get("city")
        name = city.get("name") if isinstance(city, dict) else city
        if str(name or "").strip().lower() != CITY:
            continue
        out[str(row.get("id"))] = row
    if not out:
        raise RuntimeError("feed parsed but held no listings for " + CITY)
    return out


def describe(row):
    kind = (row.get("dwellingType") or {})
    kind = kind.get("localizedName") if isinstance(kind, dict) else kind
    where = f"{row.get('street', '?')} {row.get('houseNumber', '')}".strip()
    bits = [f"{kind or 'Home'} at {where}"]
    if row.get("areaDwelling"):
        bits.append(f"{row['areaDwelling']} m2")
    if row.get("totalRent"):
        bits.append(f"EUR {row['totalRent']}/mo")
    if row.get("availableFrom"):
        bits.append(f"from {row['availableFrom']}")
    return ", ".join(bits)


def load_seen():
    """Ids already known. Persisted so a restart is not a fresh baseline
    (which would either re-alert everything or go blind); ignored where the
    filesystem is ephemeral, in which case the first check sets the baseline."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_seen(ids):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
    except OSError as e:
        log(f"could not save state (fine in cloud): {e}")


def alert(new_rows):
    summary = " | ".join(describe(r) for r in new_rows[:5])
    if len(new_rows) > 5:
        summary += f" | +{len(new_rows) - 5} more"
    link = (DETAIL_URL.format(urlKey=new_rows[0].get("urlKey"))
            if new_rows[0].get("urlKey") else LISTING_URL)
    title = f"PLAZA UTRECHT: {len(new_rows)} NEW LISTING(S)!"
    body = f"{summary}. Apply NOW: {link}\n\nAll listings: {LISTING_URL}"
    fw.notify_push(title, body, link=link)
    fw.telegram_spam_until_ack(title, body, link=link)


def main():
    once = "--once" in sys.argv
    if "--test" in sys.argv:
        log("DRILL: spamming telegram with real-alert wording until reply")
        title = "PLAZA UTRECHT: 1 NEW LISTING!"
        body = f"Studio in Utrecht. Apply NOW: {LISTING_URL}"
        fw.notify_push(title, body, link=LISTING_URL)
        fw.telegram_spam_until_ack(title, body, link=LISTING_URL)
        return 0

    if not once and not fw.HEADLESS:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lock.bind(("127.0.0.1", 51236))
        except OSError:
            log("another plaza_watcher instance is already running - exiting")
            return 2

    mode = ("single check" if once else
            f"stint of {MAX_RUNTIME_MINUTES:g} min" if MAX_RUNTIME_MINUTES
            else "continuous, forever")
    log(f"Watching Plaza {CITY.title()} every {INTERVAL_SECONDS}s ({mode})")

    seen = load_seen()
    errors = 0
    checks = 0
    started = time.time()
    alert_threads = []
    last_result = "starting up"
    while True:
        try:
            listings = fetch_listings()
            errors = 0
            checks += 1
            if not seen:
                seen = set(listings)
                save_seen(seen)
                log(f"baseline set: {len(seen)} listing(s) already up - "
                    f"only NEW ones will alert")
                for row in list(listings.values())[:5]:
                    log(f"  already up: {describe(row)}")
            else:
                new_ids = set(listings) - seen
                if new_ids:
                    rows = [listings[i] for i in sorted(new_ids)]
                    log(f"NEW LISTING(S)! {[describe(r) for r in rows]}")
                    t = threading.Thread(target=alert, args=(rows,),
                                         daemon=False)
                    t.start()
                    alert_threads.append(t)
                    seen |= new_ids
                    save_seen(seen)
                gone = seen - set(listings)
                if gone:  # taken/expired: forget them so a relist alerts again
                    seen -= gone
                    save_seen(seen)
            last_result = f"{len(listings)} listing(s) up"
            daily_checkin(len(listings))
            if checks % 45 == 1:  # heartbeat roughly every 15 min
                log(f"check #{checks}: {last_result}, nothing new")
        except Exception as e:
            errors += 1
            log(f"check failed ({errors} in a row): {e}")
            if errors in (30, 180):  # ~10 min / ~1 h of continuous failures
                log("WARNING: monitor has been failing for a while")
                fw.notify_telegram(
                    "⚠️ Plaza watcher is FAILING",
                    f"{errors} checks in a row failed ({e}). It keeps "
                    "retrying, but new listings could be missed.")
        fw.handle_status_requests(
            "Plaza Utrecht watcher",
            f"Checking every {INTERVAL_SECONDS}s. {checks} checks this run"
            + (f", {errors} failing" if errors else "")
            + f". Right now: {last_result}.",
            interval=35)
        if once:
            log("single check done")
            return 0
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
