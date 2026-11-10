"""
بيقرا صفحات عروض أمازون (زي "احصل على 3 بسعر 2" و"وفر 15% عند شراء 500 جنيه")
ويبعت منتجاتها كمسودات في قناة المسودات.
الصفحات اللي بيراقبها مكتوبة في الملف promo_pages.txt (لينك في كل سطر).
"""
import os
import re
import json
import time
import html
import logging
from urllib.parse import urlparse

from bs4 import BeautifulSoup

import post_bot as pb
import deal_card

log = logging.getLogger("promos")

BASE = os.path.dirname(os.path.abspath(__file__))
PAGES_FILE = os.path.join(BASE, "promo_pages.txt")
SENT_FILE = os.path.join(BASE, "promo_sent.json")
LOG_FILE = os.path.join(BASE, "promo_log.txt")

SCAN_EVERY = 3 * 3600      # كل قد إيه يفحص صفحات العروض
MAX_FETCH = 14             # أقصى عدد منتجات يقراها من كل صفحة في الجولة
MAX_PER_PAGE = 4           # أقصى عدد مسودات من كل صفحة في الجولة
MAX_PER_ROUND = 12         # أقصى عدد مسودات في الجولة كلها
RESEND_AFTER = 3 * 86400   # نفس المنتج في نفس العرض ما يتبعتش تاني قبل المدة دي
HOST = "www.amazon.eg"

PROMO_PATTERNS = [
    r"احصل\s*على\s*\d+\s*بسعر\s*\d+",
    r"اشتر[يى]?\s*\d+\s*(?:و|واحصل|احصل)[^\n]{0,50}",
    r"وفر\s*\d+\s*%[^\n]{0,60}",
    r"وفر\s*[\d.,]+\s*جنيه[^\n]{0,60}",
    r"Save\s+[^\n]{0,70}",
    r"Buy\s+\d+[^\n]{0,60}",
]
DATE_RE = re.compile(r"من\s+(\d+\s+\S+)\s+\d{4}\s+حتى\s+(\d+\s+\S+)\s+\d{4}")


def note(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def page_urls():
    try:
        with open(PAGES_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f]
    except Exception:
        return []
    return [l for l in lines if l.startswith("http")]


def enabled():
    return bool(page_urls())


# ---------------------------------------------------------------- dedup
def load_sent():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_sent(d):
    now = time.time()
    d = {k: v for k, v in d.items() if now - v < 14 * 86400}
    try:
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


# ---------------------------------------------------------------- fetch/parse
def fetch(url):
    """يفتح صفحة العرض (ويمشي ورا اللينكات المختصرة). يرجع (final_url, html) أو (None, '')."""
    best = (None, "")
    for headers in pb.HEADER_STRATEGIES:
        try:
            final, body = pb.resolve(url, headers)
        except Exception as e:
            log.warning("promo fetch failed: %s", type(e).__name__)
            continue
        if len(body) > len(best[1]):
            best = (final, body)
        if re.search(r'data-asin="[A-Z0-9]{10}"', body) or "/dp/" in body:
            return final, body
    return best


ASIN_PATTERNS = [
    r'data-asin=["\']([A-Z0-9]{10})["\']',
    r'amzn1\.asin\.([A-Z0-9]{10})',
    r'/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})',
    r'["\']asin["\']\s*[:=]\s*["\']([A-Z0-9]{10})["\']',
    r'\basin=([A-Z0-9]{10})',
]


def extract_asins(body):
    """كل أكواد المنتجات (ASIN) في الصفحة بترتيب ظهورها."""
    hits = []
    for p in ASIN_PATTERNS:
        for m in re.finditer(p, body, flags=re.I):
            a = m.group(1).upper()
            if re.fullmatch(r"[A-Z0-9]{10}", a) and re.search(r"\d", a):
                hits.append((m.start(), a))
    hits.sort()
    found = []
    for _, a in hits:
        if a not in found:
            found.append(a)
    return found


def diagnose(body):
    """معلومات تساعدنا نفهم شكل الصفحة لو مفيش منتجات."""
    low = body.lower()
    return (
        f"len={len(body)} data-asin={low.count('data-asin')} dp={low.count('/dp/')} "
        f"asin={low.count('asin')} captcha={'captcha' in low} "
        f"robot={'robot' in low} json={low.count('application/json')} next={'__next' in low}"
    )


def promo_info(body):
    """يرجع (عنوان العرض، تواريخ أو None)."""
    soup = BeautifulSoup(body, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    title = None
    for l in lines[:120]:
        if len(l) > 120:
            continue
        for p in PROMO_PATTERNS:
            m = re.search(p, l, flags=re.I)
            if m:
                title = m.group(0).strip(" .:-")
                break
        if title:
            break
    if not title:
        h = soup.find(["h1", "h2"])
        if h:
            t = re.sub(r"\s+", " ", h.get_text(" ")).strip()
            if 3 < len(t) < 100:
                title = t
    dates = None
    m = DATE_RE.search(" ".join(lines[:200]))
    if m:
        dates = f"من {m.group(1)} لحد {m.group(2)}"
    return title, dates


def promo_id(final):
    m = re.search(r"/promotion/psp/([A-Za-z0-9]+)", final or "")
    return m.group(1) if m else None


def promo_link(final, original):
    pid = promo_id(final) or promo_id(original)
    if pid:
        return f"https://{HOST}/promotion/psp/{pid}?tag={pb.MY_TAG}"
    return None


# ---------------------------------------------------------------- post
def build_post(item, promo, dates, link, plink):
    lines = [f"🎁 {html.escape(promo)}"]
    if dates:
        lines.append(f"📅 {html.escape(dates)}")
    lines += ["", f"<b>{html.escape(pb.short_title(item['title']))}</b>", ""]
    lines.append(f"💰 السعر: <b>{pb.fmt(item['price'])}</b>")
    if item.get("old") and item["old"] > item["price"]:
        lines.append(f"🏷️ كان: <s>{pb.fmt(item['old'])}</s> (خصم {item['pct']}%)")
    lines += ["", "ℹ️ شروط العرض بتتطبق في عربة التسوق. راجعها في الصفحة.", "", "🛒 اشتري من هنا:", link]
    if plink:
        lines += ["", "🎁 كل منتجات العرض:", plink]
    lines += ["", "📢 قناة عروض وخصومات", "للمزيد من العروض يوميًا:", pb.CHANNEL_LINK]
    return "\n".join(lines)


def scan_once():
    """يرجع قايمة (item, post_html, image_bytes)."""
    sent = load_sent()
    results = []
    stats = []
    for url in page_urls():
        if len(results) >= MAX_PER_ROUND:
            break
        final, body = fetch(url)
        if not body:
            stats.append(f"{url[:40]}: مفتحتش")
            continue
        asins = extract_asins(body)
        promo, dates = promo_info(body)
        if not asins or not promo:
            stats.append(f"{url[:40]}: asins={len(asins)} promo={'yes' if promo else 'no'} {diagnose(body)}")
            try:
                with open(os.path.join(BASE, "promo_debug.html"), "w", encoding="utf-8") as f:
                    f.write(body)
            except Exception:
                pass
            continue
        pkey = promo_id(final) or promo
        plink = promo_link(final, url)
        made = 0
        for asin in asins[:MAX_FETCH]:
            if made >= MAX_PER_PAGE or len(results) >= MAX_PER_ROUND:
                break
            dkey = f"{asin}|{pkey}"
            if time.time() - sent.get(dkey, 0) < RESEND_AFTER:
                continue
            dbg = []
            _, page = pb.get_page(f"https://{HOST}/dp/{asin}", dbg)
            if not page:
                continue
            title, price, old, _, img = pb.read_product(page)
            if not title or not price:
                continue
            item = {"asin": asin, "title": title, "price": price, "old": old,
                    "pct": 0, "image": img, "badge": promo}
            if old and old > price:
                item["pct"] = int(round((old - price) / old * 100))
            raw = pb.download_image(img) if img else None
            image = deal_card.make_card(item, raw) if raw else None
            if not image and raw:
                image = pb.prepare_image(raw)
            link = pb.affiliate(HOST, asin)
            results.append((item, build_post(item, promo, dates, link, plink), image))
            sent[dkey] = time.time()
            made += 1
            time.sleep(3)
        stats.append(f"{promo[:30]}: asins={len(asins)} new={made}")
    save_sent(sent)
    msg = f"promo scan: pages={len(page_urls())} new={len(results)} | " + " || ".join(stats)
    log.info(msg)
    note(msg)
    return results
