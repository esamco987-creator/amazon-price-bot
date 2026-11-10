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

import requests
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
    r"Buy\s+\d+,?\s*(?:Save|get)[^\n]{0,60}",
    r"احصل\s*على\s*\d+\s*بسعر\s*\d+",
    r"اشتر[يى]?\s*\d+\s*(?:و|واحصل|احصل)[^\n]{0,50}",
    r"وفر\s*\d+\s*%[^\n]{0,60}",
    r"وفر\s*[\d.,]+\s*جنيه[^\n]{0,60}",
    r"Save\s+[^\n]{0,70}",
    r"Buy\s+\d+[^\n]{0,60}",
]
DATE_RE = re.compile(r"من\s+(\d+\s+\S+)\s+\d{4}\s+حتى\s+(\d+\s+\S+)\s+\d{4}")
DATE_EN_RE = re.compile(r"From\s+(\d+\s+\w+)\s+\d{4}\s+to\s+(\d+\s+\w+)\s+\d{4}", re.I)


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
    # data-asin="B0.." أو data-asin=\"B0..\" أو "asin":"B0.." (حتى لو الاقتباس متهرّب بـ \)
    r'asins?[\W_]{1,8}([A-Z0-9]{10})(?![A-Za-z0-9])',
    r'amzn1\.asin\.([A-Z0-9]{10})',
    r'/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})',
]
ASIN_OK = re.compile(r"B0[A-Z0-9]{8}")


def extract_asins(body):
    """كل أكواد المنتجات (ASIN) في الصفحة بترتيب ظهورها."""
    hits = []
    for p in ASIN_PATTERNS:
        for m in re.finditer(p, body, flags=re.I):
            a = m.group(1).upper()
            if ASIN_OK.fullmatch(a):
                hits.append((m.start(), a))
    hits.sort()
    found = []
    for _, a in hits:
        if a not in found:
            found.append(a)
    # كمان أي كود شكله زي ASIN (B0 + 8 حروف/أرقام) في الصفحة (للقوايم المكتوبة كمصفوفة)
    for m in re.finditer(r"(?<![A-Za-z0-9])(B0[A-Z0-9]{8})(?![A-Za-z0-9])", body):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


def endpoints(body, limit=14):
    """روابط/مسارات في كود الصفحة ممكن تكون هي اللي بتحمّل المنتجات (للتشخيص)."""
    out = []
    for m in re.finditer(r"""["'](/[A-Za-z0-9_\-./]*(?:ajax|promotion|api|asin|products|search|widget)[A-Za-z0-9_\-./?=&%:{}$]*)["']""", body, flags=re.I):
        v = m.group(1)
        if v not in out and len(v) < 140:
            out.append(v)
        if len(out) >= limit:
            break
    return out


def diagnose(body):
    """معلومات تساعدنا نفهم شكل الصفحة لو مفيش منتجات."""
    low = body.lower()
    i = low.find("data-asin")
    sample = repr(body[max(0, i - 5): i + 45]) if i >= 0 else "-"
    return (
        f"len={len(body)} data-asin={low.count('data-asin')} dp={low.count('/dp/')} "
        f"asin={low.count('asin')} captcha={'captcha' in low} sample={sample}"
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
    joined = " ".join(lines[:200])
    m = DATE_RE.search(joined) or DATE_EN_RE.search(joined)
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


def parse_money(d):
    """يقرا سعر من كائن priceInfo.* (displayString)."""
    if not isinstance(d, dict):
        return None
    return pb.parse_price(d.get("displayString") or "")


def fix_url(u):
    if not u:
        return None
    return "https:" + u if u.startswith("//") else u


def load_promo(url):
    """يفتح صفحة العرض ويجيب منتجاتها من نفس الطلب اللي الصفحة بتستخدمه.
    يرجع dict: final, title, dates, products=[{asin,title,price,old,image}] أو None."""
    import requests as rq

    try:
        final, _ = pb.resolve(url, pb.HEADER_STRATEGIES[0])
    except Exception as e:
        note(f"resolve failed: {type(e).__name__}")
        return None
    sess = rq.Session()
    pid0 = promo_id(final)
    if pid0:
        final = f"https://{HOST}/-/ar/promotion/psp/{pid0}"  # النسخة العربية
    hdr = dict(pb.HEADER_STRATEGIES[1])
    hdr["Accept-Language"] = "ar-EG,ar;q=0.9,en;q=0.8"
    try:
        r = sess.get(final, headers=hdr, timeout=30)
        body = r.text or ""
    except Exception as e:
        note(f"page get failed: {type(e).__name__}")
        return None
    host = urlparse(r.url).netloc or HOST
    pid = promo_id(r.url) or promo_id(final)
    m = re.search(r'"promotionId"\s*:\s*"([A-Z0-9]+)"', body)
    pid = pid or (m.group(1) if m else None)
    mm = re.search(r'"promotionMerchantId"\s*:\s*"([A-Z0-9]+)"', body)
    ct = re.search(r"""anti-csrftoken-a2z["']\s*:\s*["']([^"']+)["']""", body)
    title, dates = promo_info(body)
    out = {"final": r.url, "title": title, "dates": dates, "products": [], "body": body}
    if not pid or not ct:
        note(f"api params missing: pid={bool(pid)} csrf={bool(ct)}")
        return out
    data = {
        "promotionId": pid, "avgCustomerReview": "0", "priceTierMin": "", "priceTierMax": "",
        "bestSeller": "0", "sortId": "[]", "applicabilityIndex": "", "productCategory": "",
        "searchKeyword": "", "redirectAsin": "", "redirectMerchantId": "",
        "productCategories": "[]", "subProductCategory": "", "selectedBrands": "[]",
        "promotionMerchantId": mm.group(1) if mm else "", "isSNSOnly": "false", "isSUSOnly": "false",
        "isCarouselPilot": "false", "isPrimeShippingEligible": "true", "isPreview": "false",
        "isFirstPageLoadRequest": "true", "recordedGroupingIds": "[]",
        "anti-csrftoken-a2z": ct.group(1),
    }
    h2 = {
        "User-Agent": hdr.get("User-Agent", ""), "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": hdr["Accept-Language"], "X-Requested-With": "XMLHttpRequest",
        "Referer": r.url, "Origin": f"https://{host}",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    try:
        pr = sess.post(f"https://{host}/promotion/psp/productInfoList", data=data, headers=h2, timeout=40)
        if pr.status_code != 200:
            note(f"productInfoList status {pr.status_code}: {pr.text[:120]!r}")
            return out
        lst = ((pr.json().get("viewModels") or {}).get("PRODUCT_INFO_LIST")) or []
    except Exception as e:
        note(f"productInfoList failed: {type(e).__name__}")
        return out
    for p in lst:
        try:
            asin = p.get("asin")
            if not asin:
                continue
            pi = p.get("priceInfo") or {}
            price = parse_money(pi.get("priceToPay"))
            old = parse_money(pi.get("basisPrice")) or parse_money(pi.get("strikeThroughPrice"))
            out["products"].append({
                "asin": asin, "title": p.get("title") or p.get("imgAlt"),
                "price": price, "old": old, "image": fix_url(p.get("imgURL")),
            })
        except Exception:
            continue
    note(f"promo api: pid={pid} products={len(out['products'])}")
    return out


def send_debug_file(path):
    """بيبعتلك نسخة من الصفحة على تيليجرام (مرة واحدة) عشان أقدر أحلل شكلها."""
    flag = path + ".sent"
    token = os.environ.get("DRAFT_BOT_TOKEN", "")
    owner = os.environ.get("OWNER_ID", "")
    if os.path.exists(flag) or not token or not owner:
        return
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": owner, "caption": "نسخة من صفحة العرض للتشخيص"},
                files={"document": ("promo_debug.html", f)},
                timeout=120,
            )
        note(f"debug file sent: status {r.status_code}")
        if r.status_code == 200:
            open(flag, "w").write("1")
    except Exception as e:
        note(f"debug file send failed: {type(e).__name__}")


def scan_once():
    """يرجع قايمة (item, post_html, image_bytes)."""
    sent = load_sent()
    results = []
    stats = []
    for url in page_urls():
        if len(results) >= MAX_PER_ROUND:
            break
        info = load_promo(url)
        if not info:
            stats.append(f"{url[:40]}: مفتحتش")
            continue
        promo, dates, prods = info["title"], info["dates"], info["products"]
        if not prods or not promo:
            stats.append(f"{url[:40]}: products={len(prods)} promo={'yes' if promo else 'no'}")
            continue
        pkey = promo_id(info["final"]) or promo
        plink = promo_link(info["final"], url)
        made = 0
        for p in prods[:MAX_FETCH]:
            if made >= MAX_PER_PAGE or len(results) >= MAX_PER_ROUND:
                break
            asin = p["asin"]
            dkey = f"{asin}|{pkey}"
            if time.time() - sent.get(dkey, 0) < RESEND_AFTER:
                continue
            title, price, old, img = p["title"], p["price"], p["old"], p["image"]
            if not title or not price:
                _, page = pb.get_page(f"https://{HOST}/dp/{asin}", [])
                if not page:
                    continue
                t2, p2, o2, _, i2 = pb.read_product(page)
                title, price, old, img = title or t2, price or p2, old or o2, img or i2
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
            results.append((item, build_post(item, promo, dates, pb.affiliate(HOST, asin), plink), image))
            sent[dkey] = time.time()
            made += 1
            time.sleep(2)
        stats.append(f"{promo[:30]}: products={len(prods)} new={made}")
    save_sent(sent)
    msg = f"promo scan: pages={len(page_urls())} new={len(results)} | " + " || ".join(stats)
    log.info(msg)
    note(msg)
    return results
