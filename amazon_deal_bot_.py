#!/usr/bin/env python3
"""
Halbautomatischer Amazon-Deal-Bot (kostenlos).

Liest mydealz-Feeds, behält NUR Amazon-Deals und schickt sie dir per Telegram:
Titel, Preis, Such-Link zu Amazon und einen fertigen Text zum Kopieren.
Deinen Affiliate-Link holst du dann per SiteStripe und fügst ihn ein.

Umgebungsvariablen (Windows: set NAME=wert):
  TELEGRAM_TOKEN   Token vom @BotFather
  TELEGRAM_CHAT    Deine Chat-ID
  AFFILIATE_TAG    optional, nur falls doch eine ASIN gefunden wird

Start:
  pip install requests
  python amazon_deal_bot.py --test   (nur anzeigen, nichts senden)
  python amazon_deal_bot.py          (Deals an Telegram senden)
"""
import os
import re
import sys
import time
import html
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
import requests

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT", "")
TAG = os.environ.get("AFFILIATE_TAG", "")

# --- Einstellungen ----------------------------------------------------
FEEDS = [
    "https://www.mydealz.de/rss/gruppe/preisfehler",  # Preisfehler
    "https://www.mydealz.de/rss/hot",                 # heiße Deals
]
# Zeile unter jedem Post. Leer lassen = keine Fußzeile.
# Beispiel für Kennzeichnung: FOOTER = "Anzeige / Affiliate-Link"
FOOTER = ""
MAX_PER_RUN = 5          # max. Nachrichten pro Durchlauf
COOLDOWN_HOURS = 48      # gleicher Deal frühestens nach 48h erneut
# ----------------------------------------------------------------------

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalDealBot/1.0)"}
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent.db")
ASIN_RE = re.compile(r"amazon\.[a-z.]+/(?:[^\s\"'<>]*?/)?(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.I)


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts REAL)")
    return con


def recently_sent(con, key):
    row = con.execute("SELECT ts FROM sent WHERE key=?", (key,)).fetchone()
    return bool(row) and (time.time() - row[0]) < COOLDOWN_HOURS * 3600


def mark_sent(con, key):
    con.execute("INSERT OR REPLACE INTO sent VALUES (?,?)", (key, time.time()))
    con.commit()


def read_feed(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        raw = ET.tostring(it, encoding="unicode")
        m = re.search(r'merchant name="([^"]*)"(?:\s+price="([^"]*)")?', raw)
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "guid": (it.findtext("guid") or it.findtext("link") or "").strip(),
            "merchant": html.unescape(m.group(1)) if m else "",
            "price": html.unescape(m.group(2)) if m and m.group(2) else "",
            "raw": raw,
        })
    return items


def is_amazon(item):
    if item["merchant"]:
        return "amazon" in item["merchant"].lower()
    return "amazon" in item["title"].lower()


def find_asin(item):
    m = ASIN_RE.search(html.unescape(item["raw"]))
    return m.group(1).upper() if m else None


def clean_title(t):
    t = re.sub(r"^\s*(?:\[[^\]]*\]|\([^)]*\))\s*", "", t)  # "[Preisfehler]" vorne weg
    t = re.split(r"\s(?:für|statt|nur)\s+\d|\s[-–]\s+(?:nur|statt)\s", t)[0]  # Preis steht separat
    return t.strip(" -–|/")[:110]


def search_term(t):
    """Kurzer Suchbegriff für Amazon aus dem Titel."""
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", t)
    t = re.split(r"\s(?:für|statt|nur|ab|bei)\s|\s[|/]\s|\s-\s", t)[0]
    return re.sub(r"\s+", " ", t).strip()[:80]


NUM_RE = r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)"


def to_float(num):
    return float(num.replace(".", "").replace(",", "."))


def first_number(text):
    m = re.search(NUM_RE, text or "")
    return m.group(1) if m else None


def old_price(item, new_num):
    """Alten Preis suchen: 'statt 34,68 €' im Titel, sonst <del>..</del> in der Beschreibung."""
    cands = []
    m = re.search(r"statt\s*" + NUM_RE, item["title"], re.I)
    if m:
        cands.append(m.group(1))
    for d in re.findall(r"<del>(.*?)</del>", html.unescape(item["raw"]), re.S | re.I):
        n = first_number(d)
        if n:
            cands.append(n)
    for c in cands:
        try:
            if new_num and to_float(c) > to_float(new_num):
                return c
        except ValueError:
            pass
    return None


def price_line(item):
    new = first_number(item["price"])
    old = old_price(item, new)
    new_txt = f"{new} Euro" if new else "[PREIS] Euro"
    old_txt = f"{old} Euro" if old else "[ALTER PREIS] Euro"
    return f"💶 statt {old_txt} nur {new_txt}\n", bool(old)


def build_messages(item, asin):
    title = clean_title(item["title"])
    price_txt, old_known = price_line(item)
    is_error = "preisfehler" in (item["title"] + item["raw"]).lower()
    label = "🚨 Preisfehler bei Amazon" if is_error else "🔥 Amazon-Deal"
    now = time.strftime("%d.%m. %H:%M")

    if asin and TAG:
        link = f"https://www.amazon.de/dp/{asin}?tag={TAG}"
        body = f"{label}\n\n{title}\n{price_txt}\n👉 {link}" + (f"\n\n{FOOTER}" if FOOTER else "")
        return [f"<b>Fertig zum Posten:</b>\n<pre>{html.escape(body)}</pre>"]

    search = "https://www.amazon.de/s?k=" + urllib.parse.quote_plus(search_term(item["title"]))
    template = f"{label}\n\n{title}\n{price_txt}\n👉 [DEIN LINK]" + (f"\n\n{FOOTER}" if FOOTER else "")
    info = (f"{label}\n\n<b>{html.escape(title)}</b>\n{html.escape(price_txt)}\n"
            f"🔎 Bei Amazon suchen:\n{search}\n\n"
            f"ℹ️ Deal-Seite (mydealz):\n{item['link']}\n\n"
            f"Nächste Nachricht = Text zum Kopieren. Nur [DEIN LINK] ersetzen."
            + ("" if old_known else " Alten Preis bitte selbst bei Amazon prüfen und bei [ALTER PREIS] eintragen."))
    return [info, f"<pre>{html.escape(template)}</pre>"]


def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
        timeout=30,
    )
    r.raise_for_status()


def main():
    test = "--test" in sys.argv
    if not test:
        for name, val in [("TELEGRAM_TOKEN", TG_TOKEN), ("TELEGRAM_CHAT", TG_CHAT)]:
            if not val:
                sys.exit(f"Fehlt: {name}")

    con = db()
    sent = 0
    for feed in FEEDS:
        try:
            items = read_feed(feed)
        except Exception as e:
            print(f"Feed-Fehler {feed}: {e}")
            continue
        amazon = [i for i in items if is_amazon(i)]
        print(f"{feed}: {len(items)} Einträge, davon {len(amazon)} von Amazon")

        for item in amazon:
            if test:
                print(f"  - {clean_title(item['title'])} | {price_line(item)[0].strip()}")
                continue
            if recently_sent(con, item["guid"]):
                continue
            if sent >= MAX_PER_RUN:
                break
            for msg in build_messages(item, find_asin(item)):
                send_telegram(msg)
                time.sleep(1)
            mark_sent(con, item["guid"])
            sent += 1
            time.sleep(2)
    if not test:
        print(f"{sent} Deals gesendet.")


if __name__ == "__main__":
    main()
