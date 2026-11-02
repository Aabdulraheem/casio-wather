#!/usr/bin/env python3
"""Casio Store (Bhawar) discount alert - sends Telegram/email alerts on new discounts."""

import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

# ============================ CONFIG ============================
SITE = "https://casiostore.bhawar.com"
COLLECTIONS = ["watches"]
MIN_DISCOUNT = int(os.getenv("MIN_DISCOUNT", "10"))
ONLY_IN_STOCK = True
CHECK_EVERY_MINUTES = 5
RUN_ONCE = False

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

EMAIL_ENABLED = False
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")

STATE_FILE = Path(__file__).with_name("seen_discounts.json")
# ================================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def log(msg):
    print(time.strftime("[%d-%b %H:%M:%S]"), msg, flush=True)


# ---------------------- Fetching products ----------------------
def fetch_via_json(collection):
    products = []
    for page in range(1, 20):
        url = f"{SITE}/collections/{collection}/products.json?limit=250&page={page}"
        data = json.loads(http_get(url))
        items = data.get("products", [])
        if not items:
            break
        for p in items:
            best = None
            for v in p.get("variants", []):
                try:
                    price = float(v.get("price") or 0)
                    compare = float(v.get("compare_at_price") or 0)
                except ValueError:
                    continue
                pct = round((compare - price) / compare * 100) if compare > price > 0 else 0
                avail = bool(v.get("available", True))
                cand = {"price": price, "mrp": compare or price, "pct": pct, "available": avail}
                if best is None or (cand["available"], cand["pct"]) > (best["available"], best["pct"]):
                    best = cand
            if best:
                products.append({
                    "id": str(p["id"]),
                    "title": p.get("title", "").strip(),
                    "url": f"{SITE}/products/{p['handle']}",
                    **best,
                })
        if len(items) < 250:
            break
    return products


def fetch_via_html(collection):
    products, seen = [], set()
    for page in range(1, 20):
        html = http_get(f"{SITE}/collections/{collection}?page={page}")
        new_on_page = 0
        for m in re.finditer(r'href="/(?:collections/[^/"]+/)?products/([a-z0-9\-_%]+)[^"]*"', html):
            handle = m.group(1)
            if handle in seen:
                continue
            seen.add(handle)
            new_on_page += 1
            chunk = html[m.end(): m.end() + 2500]
            text = re.sub(r"<[^>]+>", " ", chunk)
            pct_m = re.search(r"(\d+)\s*%\s*Off", chunk, re.I)
            price_m = re.search(r"Sale price[^₹]*₹\s*([\d,]+)", text, re.I)
            mrp_m = re.search(r"MRP[^₹]*₹\s*([\d,]+)", text, re.I)
            to_f = lambda x: float(x.group(1).replace(",", "")) if x else 0.0
            sold_out = bool(re.search(r"sold\s*out", chunk[:1500], re.I)) and "Add to cart" not in chunk[:1500]
            products.append({
                "id": handle,
                "title": handle.replace("-", " ").upper(),
                "url": f"{SITE}/products/{handle}",
                "price": to_f(price_m),
                "mrp": to_f(mrp_m),
                "pct": int(pct_m.group(1)) if pct_m else 0,
                "available": not sold_out,
            })
        if new_on_page == 0:
            break
    return products


def fetch_all():
    results = {}
    for col in COLLECTIONS:
        try:
            items = fetch_via_json(col)
            log(f"{col}: {len(items)} products (JSON feed)")
        except Exception as e:
            log(f"{col}: JSON feed failed ({e}); trying page scraping")
            items = fetch_via_html(col)
            log(f"{col}: {len(items)} products (HTML)")
        for p in items:
            results[p["id"]] = p
    return results


# ---------------------- Alerts ----------------------
def send_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), 3800):
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[i:i + 3800],
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(urllib.request.Request(api, data=data), timeout=30).read()
    return True


def send_email(subject, text):
    if not (EMAIL_ENABLED and EMAIL_USER and EMAIL_PASSWORD and EMAIL_TO):
        return False
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_USER, EMAIL_TO
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        s.login(EMAIL_USER, EMAIL_PASSWORD)
        s.send_message(msg)
    return True


def notify(subject, text):
    sent = False
    for fn, args in ((send_telegram, (text,)), (send_email, (subject, text))):
        try:
            sent = fn(*args) or sent
        except Exception as e:
            log(f"Alert via {fn.__name__} failed: {e}")
    if not sent:
        log("No alert channel configured/working. Message was:\n" + text)


def format_items(items):
    lines = []
    for p in sorted(items, key=lambda x: -x["pct"]):
        lines.append(f"{p['pct']}% OFF  {p['title']}\n  Rs {p['price']:,.0f} (MRP Rs {p['mrp']:,.0f})\n  {p['url']}")
    return "\n\n".join(lines)


# ---------------------- State + logic ----------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return None


def save_state(products):
    state = {pid: {"pct": p["pct"], "available": p["available"]} for pid, p in products.items()}
    STATE_FILE.write_text(json.dumps(state))


def check_once():
    products = fetch_all()
    if not products:
        log("Got 0 products - site may be down or blocking. Skipping this round.")
        return
    previous = load_state()
    deals = [p for p in products.values()
             if p["pct"] >= MIN_DISCOUNT and (p["available"] or not ONLY_IN_STOCK)]
    log(f"{len(deals)} products currently at {MIN_DISCOUNT}%+ discount")

    if previous is None:
        save_state(products)
        notify("Casio discount watcher started",
               f"Watcher is running. Right now {len(deals)} products have {MIN_DISCOUNT}%+ discount. "
               f"You will be alerted when NEW discounts appear.")
        return

    fresh = []
    for p in deals:
        old = previous.get(p["id"])
        was_deal = old and old["pct"] >= MIN_DISCOUNT and (old["available"] or not ONLY_IN_STOCK)
        if not old or not was_deal or p["pct"] >= old["pct"] + 5:
            fresh.append(p)

    if fresh:
        top = max(p["pct"] for p in fresh)
        notify(f"Casio sale alert: {len(fresh)} new deals, up to {top}% off",
               f"NEW DISCOUNTS ON CASIO STORE ({len(fresh)})\n\n" + format_items(fresh))
        log(f"Alert sent for {len(fresh)} products")
    save_state(products)


def main():
    if "--test" in sys.argv:
        notify("Casio watcher test", "Test alert: your Casio discount watcher can reach you.")
        return
    if RUN_ONCE or "--once" in sys.argv:
        check_once()
        return
    log(f"Watching {SITE} every {CHECK_EVERY_MINUTES} min. Press Ctrl+C to stop.")
    while True:
        try:
            check_once()
        except Exception as e:
            log(f"Check failed: {e}")
        time.sleep(CHECK_EVERY_MINUTES * 60)


if __name__ == "__main__":
    main()
