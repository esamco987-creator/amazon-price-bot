"""محرك بحث عروض وخصومات (Mix_Compare_bot)
بيبحث عن المنتج في أمازون ونون وجوميا ويقارن الأسعار.
للمشتركين في القناة بس. لإضافة موقع جديد: اكتب دالة بحث وضيفها في STORES.

متغيرات البيئة:
  SEARCH_BOT_TOKEN   توكن البوت (مطلوب)
  OWNER_ID           رقمك (مطلوب)
  DRAFT_BOT_TOKEN    اختياري: بيفعّل زرار "ابعت للمسودة" ليك
  CREATORS_ID / CREATORS_SECRET   بيانات أمازون (نفس بوت العروض)
"""
import os
import re
import json
import time
import html
import random
import string
import asyncio
import logging
from urllib.parse import quote

import requests
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import deals_scanner as ds
import post_bot as pb
import draft_bot as db

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("compare_bot")

TOKEN = os.environ.get("SEARCH_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
CHANNEL = "@PromoCodeOnlain"
CHANNEL_URL = "https://t.me/PromoCodeOnlain"
MY_TAG = "mixdeals-21"

DAILY_LIMIT = 15           # عدد عمليات البحث لكل مشترك في اليوم (إنت من غير حد)
CACHE_SECONDS = 15 * 60    # نفس البحث متكرر بيرجع فوراً
STORE_TIMEOUT = 25

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE, "search_log.txt")
USAGE_FILE = os.path.join(BASE, "search_usage.json")

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
}


def note(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        # الملف ما يكبرش
        if os.path.getsize(LOG_FILE) > 200_000:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                tail = f.readlines()[-300:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except Exception:
        pass


def dump_debug(store, body):
    try:
        with open(os.path.join(BASE, f"search_debug_{store}.html"), "w", encoding="utf-8") as f:
            f.write((body or "")[:400_000])
    except Exception:
        pass


# ---------------------------------------------------------------- helpers
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")


def num(s):
    """'EGP 1,299.50' أو '١٬٢٩٩ جنيه' → 1299.5 (أول رقم في النص)."""
    if s is None:
        return None
    s = str(s).translate(AR_DIGITS)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").strip()


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[ً-ْـ]", "", s)
    s = re.sub("[أإآ]", "ا", s)
    s = s.replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def tokens(q):
    out = []
    for t in norm(q).split():
        if t.startswith("ال") and len(t) > 4:
            t = t[2:]
        out.append(t)
    return out


# كلمات عربي ↔ إنجليزي شائعة (الماركات والمنتجات) عشان "ريدمي" تطابق "Redmi"
ALIASES = {
    "ريدمي": "redmi", "شاومي": "xiaomi", "سامسونج": "samsung", "ايفون": "iphone", "ابل": "apple",
    "هواوي": "huawei", "اوبو": "oppo", "ريلمي": "realme", "انفينيكس": "infinix", "تكنو": "tecno",
    "نوكيا": "nokia", "سوني": "sony", "ال جي": "lg", "الجي": "lg", "توشيبا": "toshiba",
    "شارب": "sharp", "فريش": "fresh", "يونيون اير": "unionaire", "ايسوس": "asus", "ديل": "dell",
    "لينوفو": "lenovo", "اتش بي": "hp", "اير": "air", "بودز": "buds", "بودس": "buds",
    "سماعه": "headphone earphone earbuds", "سماعات": "headphones earphones earbuds",
    "شاحن": "charger", "كابل": "cable", "ساعه": "watch", "ذكيه": "smart", "ماوس": "mouse",
    "كيبورد": "keyboard", "لابتوب": "laptop", "تابلت": "tablet", "موبايل": "mobile phone",
    "تلفزيون": "tv television", "شاشه": "screen tv", "غساله": "washing", "ثلاجه": "refrigerator",
    "مكواه": "iron", "خلاط": "blender", "مروحه": "fan", "سخان": "heater", "بلايستيشن": "playstation",
    "اكس بوكس": "xbox", "نينتندو": "nintendo", "كاميرا": "camera", "بروجكتور": "projector",
    "باور بانك": "power bank", "ميكروويف": "microwave", "قلايه": "fryer", "هوائيه": "air",
    "كمبيوتر": "computer", "طابعه": "printer", "راوتر": "router", "جيمنج": "gaming",
    "فيتامين": "vitamin", "شامبو": "shampoo", "كريم": "cream", "عطر": "perfume", "برفان": "perfume",
    "نظاره": "glasses sunglasses", "حذاء": "shoes", "جزمه": "shoes", "شنطه": "bag", "حقيبه": "bag",
}


def score(query, title):
    toks = tokens(query)
    if not toks:
        return 0.0
    t = norm(title)
    qn = norm(query)
    hit = 0
    for tok in toks:
        alts = {tok}
        for k, v in ALIASES.items():
            if tok == k or tok == norm(k) or (" " in k and norm(k) in qn and tok in norm(k).split()):
                alts.update(v.split())
        if any(re.search(r"(?<!\w)(?:ال|وال|بال)?" + re.escape(a), t) for a in alts):
            hit += 1
    return hit / len(toks)


def fmt(v):
    if v is None:
        return "—"
    return f"{int(v):,} جنيه" if v == int(v) else f"{v:,.2f} جنيه"


def short(t, n=80):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------- stores
def search_amazon(q):
    if not ds.enabled():
        raise RuntimeError("CREATORS_ID/SECRET not set")
    body = {
        "partnerTag": ds.PARTNER_TAG,
        "keywords": q,
        "marketplace": ds.MARKETPLACE,
        "itemCount": 10,
        "resources": [
            "images.primary.large",
            "itemInfo.title",
            "offersV2.listings.price",
            "offersV2.listings.availability",
            "offersV2.listings.isBuyBoxWinner",
            "customerReviews.starRating",
        ],
    }
    headers = {
        "Authorization": f"Bearer {ds.get_token()}",
        "Content-Type": "application/json",
        "x-marketplace": ds.MARKETPLACE,
    }
    if ds._lang["on"] and ds.LANG:
        body["languagesOfPreference"] = [ds.LANG]
    r = requests.post(ds.API_URL, json=body, headers=headers, timeout=40)
    if r.status_code != 200 and "languagesOfPreference" in body and "lang" in r.text.lower():
        body.pop("languagesOfPreference", None)
        r = requests.post(ds.API_URL, json=body, headers=headers, timeout=40)
    if r.status_code != 200 and "customerReviews" in r.text:
        body["resources"] = [x for x in body["resources"] if "customerReviews" not in x]
        r = requests.post(ds.API_URL, json=body, headers=headers, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"amazon status {r.status_code}: {r.text[:150]}")
    out = []
    for it in ((r.json().get("searchResult") or {}).get("items") or []):
        try:
            listings = (it.get("offersV2") or {}).get("listings") or []
            if not listings:
                continue
            lst = next((x for x in listings if x.get("isBuyBoxWinner")), listings[0])
            av = (lst.get("availability") or {}).get("type")
            if av and av != "IN_STOCK":
                continue
            po = lst.get("price") or {}
            price = (po.get("money") or {}).get("amount")
            basis = ((po.get("savingBasis") or {}).get("money") or {}).get("amount")
            title = ((it.get("itemInfo") or {}).get("title") or {}).get("displayValue")
            if not price or not title:
                continue
            price = float(price)
            old = float(basis) if basis and float(basis) > price else None
            img = (((it.get("images") or {}).get("primary") or {}).get("large") or {}).get("url")
            rating = ((it.get("customerReviews") or {}).get("starRating") or {}).get("value")
            out.append({
                "title": title, "price": price, "old": old, "image": img,
                "rating": float(rating) if rating else None,
                "url": f"https://www.amazon.eg/dp/{it['asin']}?tag={MY_TAG}",
            })
        except Exception as e:
            log.warning("amazon parse item: %s", type(e).__name__)
    return out


def search_jumia(q):
    url = "https://www.jumia.com.eg/catalog/?q=" + quote(q)
    r = requests.get(url, headers=BROWSER, timeout=30)
    if r.status_code != 200:
        dump_debug("jumia", r.text)
        raise RuntimeError(f"jumia status {r.status_code}")
    body = r.text
    out = []
    for blk in re.findall(r"<article[^>]*class=\"[^\"]*\bprd\b[^\"]*\"[^>]*>(.*?)</article>", body, re.S):
        try:
            m = re.search(r"<a[^>]+href=\"([^\"]+)\"", blk)
            href = m.group(1) if m else None
            m = re.search(r"class=\"name\"[^>]*>(.*?)</h3>", blk, re.S) or re.search(r"data-gtm-name=\"([^\"]+)\"", blk)
            title = clean(m.group(1)) if m else None
            m = re.search(r"class=\"prc\"[^>]*>(.*?)</div>", blk, re.S)
            price = num(clean(m.group(1))) if m else None
            m = re.search(r"class=\"old\"[^>]*>(.*?)</div>", blk, re.S)
            old = num(clean(m.group(1))) if m else None
            m = re.search(r"data-src=\"(https?://[^\"]+)\"", blk) or re.search(r"<img[^>]+src=\"(https?://[^\"]+)\"", blk)
            img = m.group(1) if m else None
            m = re.search(r"class=\"stars[^\"]*\"[^>]*>\s*([\d.]+)", blk)
            rating = float(m.group(1)) if m else None
            if not (href and title and price):
                continue
            if href.startswith("/"):
                href = "https://www.jumia.com.eg" + href
            href = href.split("#")[0]
            out.append({"title": title, "price": price, "old": old if old and old > price else None,
                        "image": img, "rating": rating, "url": href})
        except Exception as e:
            log.warning("jumia parse item: %s", type(e).__name__)
    if not out:
        dump_debug("jumia", body)
    return out


def _walk(o, found):
    if isinstance(o, dict):
        if "sku" in o and "name" in o and any(k in o for k in ("sale_price", "price")):
            found.append(o)
        else:
            for v in o.values():
                _walk(v, found)
    elif isinstance(o, list):
        for v in o:
            _walk(v, found)


def search_noon(q):
    url = "https://www.noon.com/egypt-ar/search/?q=" + quote(q)
    r = requests.get(url, headers=BROWSER, timeout=30)
    body = r.text
    if r.status_code != 200:
        dump_debug("noon", body)
        raise RuntimeError(f"noon status {r.status_code}")
    m = re.search(r"<script id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", body, re.S)
    hits = []
    if m:
        try:
            _walk(json.loads(m.group(1)), hits)
        except Exception:
            pass
    out, seen = [], set()
    for h in hits:
        try:
            sku = h.get("sku")
            if not sku or sku in seen:
                continue
            seen.add(sku)
            price = num(h.get("sale_price")) or num(h.get("price"))
            old = num(h.get("price")) if h.get("sale_price") else None
            if not price:
                continue
            slug = h.get("url") or ""
            link = f"https://www.noon.com/egypt-ar/{slug}/{sku}/p/" if slug else f"https://www.noon.com/egypt-ar/{sku}/p/"
            key = h.get("image_key")
            img = f"https://f.nooncdn.com/p/{key}.jpg" if key else None
            rt = h.get("product_rating")
            rating = num(rt.get("value")) if isinstance(rt, dict) else num(rt)
            out.append({"title": h["name"], "price": price, "old": old if old and old > price else None,
                        "image": img, "rating": rating, "url": link})
        except Exception as e:
            log.warning("noon parse item: %s", type(e).__name__)
    if not out:
        dump_debug("noon", body)
    return out


# لإضافة موقع جديد: ضيف سطر هنا ودالة بحث بنفس الشكل (ترجع قايمة dict: title, price, old, image, rating, url)
STORES = [
    {"key": "amazon", "name": "أمازون", "emoji": "🟠", "fn": search_amazon, "on": True},
    # نون وجوميا بيقفلوا الاتصال من الموبايل دلوقتي. غيّر False لـ True لما نلاقي حل.
    {"key": "noon", "name": "نون", "emoji": "🟡", "fn": search_noon, "on": False},
    {"key": "jumia", "name": "جوميا", "emoji": "🟣", "fn": search_jumia, "on": False},
]
ACTIVE = [x for x in STORES if x.get("on")]


# ---------------------------------------------------------------- search + matching
def pick(query, items):
    """يرجع (أحسن نتيجة, ثقة, قايمة مرتبة). الثقة = المنتج غالباً هو المطلوب."""
    scored = [(score(query, it["title"]), it) for it in items if it.get("price")]
    if not scored:
        return None, False, []
    best = max(s for s, _ in scored)
    if best < 0.34:
        return None, False, []
    floor = max(best - 0.15, 0.34)
    band = [it for s, it in scored if s >= floor]
    top = min(band, key=lambda x: x["price"])
    ordered = [it for _, it in sorted(scored, key=lambda p: (-p[0], p[1]["price"]))]
    return top, best >= 0.6, ordered


def _one_store(store, query):
    try:
        items = store["fn"](query)
        note(f"{store['key']}: '{query}' -> {len(items)} items")
        return {"store": store, "items": items, "error": None}
    except Exception as e:
        note(f"{store['key']}: '{query}' FAILED {type(e).__name__} {str(e)[:150]}")
        return {"store": store, "items": [], "error": f"{type(e).__name__}"}


async def search_all(query):
    async def run(store):
        try:
            return await asyncio.wait_for(asyncio.to_thread(_one_store, store, query), STORE_TIMEOUT)
        except asyncio.TimeoutError:
            note(f"{store['key']}: '{query}' TIMEOUT")
            return {"store": store, "items": [], "error": "timeout"}
    res = await asyncio.gather(*[run(s) for s in ACTIVE])
    for r in res:
        r["top"], r["sure"], r["ordered"] = pick(query, r["items"])
    return res


_cache = {}


async def cached_search(query):
    key = " ".join(tokens(query))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    res = await search_all(query)
    if any(r["items"] for r in res):
        _cache[key] = (time.time(), res)
        if len(_cache) > 300:
            _cache.pop(next(iter(_cache)))
    return res


# ---------------------------------------------------------------- rendering
RANKS = ["2️⃣", "3️⃣"]


def link(url, text):
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def price_line(it):
    if it.get("old") and it["old"] > it["price"]:
        pct = round((it["old"] - it["price"]) / it["old"] * 100)
        return f"💰 <b>{fmt(it['price'])}</b> (بدل <s>{fmt(it['old'])}</s> — خصم {pct}%)"
    return f"💰 السعر الحالي: <b>{fmt(it['price'])}</b>"


def alternatives(query, res, n=2):
    """اختيارات تانية غير أفضل نتيجة (من نفس المتجر)."""
    top = res["top"]
    out, seen = [], {norm(top["title"])[:40]}
    for it in res["ordered"]:
        if it is top or score(query, it["title"]) < 0.34:
            continue
        key = norm(it["title"])[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= n:
            break
    return out


def render(query, results):
    """أمازون بس: أفضل نتيجة + اختيارين. لو متاجر تانية اتفعلت بنرجع لشكل المقارنة."""
    if len(results) != 1:
        return render_multi(query, results)
    res = results[0]
    top = res["top"]
    e = html.escape
    lines = [f"🔎 <b>{e(short(query, 60))}</b>", "", "🥇 <b>أفضل نتيجة</b>", e(short(top["title"], 90)), price_line(top)]
    if top.get("rating"):
        lines.append(f"⭐ تقييم {top['rating']:.1f}")
    if not res["sure"]:
        lines.append("⚠️ ممكن مش نفس المنتج بالظبط، راجع الاسم")
    lines += ["", "🛒 " + link(top["url"], "اشتري من أمازون")]
    alts = alternatives(query, res)
    if alts:
        lines += ["", "— — — — —", "📋 <b>اختيارات تانية:</b>"]
        for i, it in enumerate(alts):
            lines.append(f"{RANKS[i]} {link(it['url'], short(it['title'], 55))} — {fmt(it['price'])}")
    lines += ["", "📢 المزيد من العروض يومياً: " + CHANNEL_URL]
    return "\n".join(lines)


def render_multi(query, results):
    e = html.escape
    lines = [f"🔎 <b>نتيجة البحث:</b> {e(short(query, 60))}", ""]
    for r in results:
        st, top = r["store"], r["top"]
        head = f"{st['emoji']} <b>{st['name']}</b>"
        if not top:
            why = "مقدرتش أوصل للموقع دلوقتي" if r["error"] else "مفيش نتيجة مطابقة"
            lines += [head, f"— {why}", ""]
            continue
        lines += [head, e(short(top["title"], 70))]
        price = f"💰 <b>{fmt(top['price'])}</b>"
        if top["old"]:
            pct = round((top["old"] - top["price"]) / top["old"] * 100)
            price += f" (بدل <s>{fmt(top['old'])}</s> — خصم {pct}%)"
        lines.append(price)
        if top.get("rating"):
            lines.append(f"⭐ {top['rating']:.1f}")
        if not r["sure"]:
            lines.append("⚠️ ممكن مش نفس المنتج بالظبط، راجع الاسم")
        label = "🛒 اشتري من هنا:" if st["key"] == "amazon" else "🔗 صفحة المنتج:"
        lines += [f"{label} {top['url']}", ""]
    sure = sorted([r for r in results if r["top"] and r["sure"]], key=lambda r: r["top"]["price"])
    if len(sure) >= 2:
        a, b = sure[0], sure[1]
        diff = b["top"]["price"] - a["top"]["price"]
        if diff > 0:
            lines.append(f"🏆 <b>الأرخص: {a['store']['name']}</b>، أرخص من {b['store']['name']} بـ {fmt(diff)}")
        else:
            lines.append(f"🏆 السعر متساوي تقريباً بين {a['store']['name']} و{b['store']['name']}")
    elif len(sure) == 1:
        lines.append(f"النتيجة المؤكدة من {sure[0]['store']['name']} بس، والباقي ما لقيتش فيه منتج مطابق.")
    lines += ["", "📢 قناة عروض وخصومات: " + CHANNEL_URL]
    return "\n".join(lines)


def best_image(results):
    for pref in ("amazon", "noon", "jumia"):
        for r in results:
            if r["store"]["key"] == pref and r["top"] and r["top"].get("image") and r["sure"]:
                return r["top"]["image"]
    return None


def render_more(query, results):
    e = html.escape
    lines = [f"📋 <b>نتائج أكتر:</b> {e(short(query, 60))}", ""]
    for r in results:
        if len(results) > 1:
            lines.append(f"{r['store']['emoji']} <b>{r['store']['name']}</b>")
        if not r["ordered"]:
            lines += ["— مفيش نتايج", ""]
            continue
        for it in r["ordered"][:8]:
            lines.append(f"• {link(it['url'], short(it['title'], 60))}\n  {fmt(it['price'])}")
        lines.append("")
    return "\n".join(lines)[:4000]


# ---------------------------------------------------------------- votes (اقتراح المتاجر)
VOTES_FILE = os.path.join(BASE, "store_votes.json")
VOTE_STORES = ["نون", "جوميا", "كارفور", "بي تك", "رايه شوب"]


def load_votes():
    try:
        with open(VOTES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_vote(uid, name):
    d = load_votes()
    d[str(uid)] = name
    try:
        with open(VOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        note(f"save vote failed: {type(e).__name__}")


def tally():
    cnt = {}
    for name in load_votes().values():
        cnt[name] = cnt.get(name, 0) + 1
    return sorted(cnt.items(), key=lambda kv: -kv[1])


def ranking_text(limit=3):
    rows = tally()[:limit]
    if not rows:
        return ""
    return "\n".join(f"{i}) {html.escape(n)} — {c} صوت" for i, (n, c) in enumerate(rows, 1))


def vote_kb():
    rows, row = [], []
    for i, n in enumerate(VOTE_STORES):
        row.append(InlineKeyboardButton(n, callback_data=f"v:{i}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ متجر تاني", callback_data="vc")])
    return InlineKeyboardMarkup(rows)


VOTE_TEXT = "🏪 <b>أنهي متجر تحب نضيفه للبحث؟</b>\nاختار متجر واحد (وتقدر تغيّر اختيارك في أي وقت)."
VOTE_BTN = InlineKeyboardButton("🗳 صوّت على متجر جديد", callback_data="vote")


# ---------------------------------------------------------------- limits / membership
def _today():
    return time.strftime("%Y-%m-%d")


def used_today(uid):
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    return d if d.get("day") == _today() else {"day": _today()}


def bump(uid):
    d = used_today(uid)
    d[str(uid)] = d.get(str(uid), 0) + 1
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass
    return d[str(uid)]


async def is_member(bot, uid):
    if uid == OWNER_ID:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        return m.status in ("member", "administrator", "creator", "owner")
    except Exception as e:
        note(f"membership check failed: {type(e).__name__} {str(e)[:120]}")
        return None


JOIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📢 اشترك في القناة", url=CHANNEL_URL)],
    [InlineKeyboardButton("✅ اشتركت", callback_data="joined")],
])
NOT_MEMBER = "البوت ده لمشتركين قناة عروض وخصومات بس 🎁\nاشترك الأول وبعدين اضغط «✅ اشتركت»."
NOT_FOUND = "ملقتش حاجة بالاسم ده 😕\nجرّب الماركة + الموديل بس، زي: سامسونج A15."
SEARCH_FAIL = "حصلت مشكلة وأنا بدور، جرب تاني بعد شوية."
STORES_TEXT = " و".join(x["name"] for x in ACTIVE)

WELCOME = (
    "أهلاً بيك في <b>محرك بحث عروض وخصومات</b> 🔎\n\n"
    f"اكتب اسم أي منتج وأنا أجيبلك أحسن أسعاره على {STORES_TEXT} مصر، مع نسبة الخصم ولينك الشراء.\n\n"
    "مثال: <code>سماعة ريدمي بودز 5</code>\n\n"
    "🏪 بنضيف متاجر جديدة للبحث، وإنت اللي تختار!\n"
    "اضغط الزر تحت وصوّت على المتجر اللي عايزه."
)
WELCOME_KB = InlineKeyboardMarkup([[VOTE_BTN]])


# ---------------------------------------------------------------- search flow
async def gate(bot, chat_id, uid, query, bot_data):
    """بيتأكد من الاشتراك والحد اليومي. True لو يكمل البحث."""
    ok = await is_member(bot, uid)
    if ok is None:
        await bot.send_message(chat_id, "حصلت مشكلة مؤقتة في التحقق من الاشتراك، جرب بعد شوية.")
        return False
    if not ok:
        bot_data.setdefault("pending", {})[uid] = query
        await bot.send_message(chat_id, NOT_MEMBER, reply_markup=JOIN_KB)
        return False
    if uid != OWNER_ID and used_today(uid).get(str(uid), 0) >= DAILY_LIMIT:
        await bot.send_message(chat_id, f"خلّصت عمليات البحث النهارده ({DAILY_LIMIT}). نكمّل بكرة 🌙")
        return False
    return True


async def run_search(bot, chat_id, uid, query, bot_data):
    status = await bot.send_message(chat_id, f"⏳ بدور لك على «{short(query, 40)}»...")
    try:
        results = await cached_search(query)
    except Exception as e:
        note(f"search crashed: {type(e).__name__} {str(e)[:150]}")
        await status.edit_text(SEARCH_FAIL)
        return
    if uid != OWNER_ID:
        bump(uid)
    if not any(r["top"] for r in results):
        await status.edit_text(SEARCH_FAIL if all(r["error"] for r in results) else NOT_FOUND)
        return
    qid = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    store = bot_data.setdefault("res", {})
    store[qid] = (query, results)
    while len(store) > 200:
        store.pop(next(iter(store)))
    rows = []
    first = next((r for r in results if r["top"]), None)
    if first:
        rows.append([InlineKeyboardButton(f"🛒 افتح في {first['store']['name']}", url=first["top"]["url"])])
    rows.append([InlineKeyboardButton("🔍 نتائج أكتر", callback_data=f"more:{qid}"), VOTE_BTN])
    if uid == OWNER_ID and db.TOKEN:
        rows.append([InlineKeyboardButton("📢 ابعت للمسودة", callback_data=f"draft:{qid}")])
    kb = InlineKeyboardMarkup(rows)
    text = render(query, results)
    img = best_image(results)
    try:
        if img and len(text) <= 1000:
            await bot.send_photo(chat_id, photo=img, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                   disable_web_page_preview=True)
    except Exception as e:
        note(f"send result failed: {type(e).__name__} {str(e)[:120]}")
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
                               disable_web_page_preview=True)
    try:
        await status.delete()
    except Exception:
        pass


# ---------------------------------------------------------------- handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ok = await is_member(context.bot, update.effective_user.id)
    if ok:
        await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=WELCOME_KB)
    else:
        await update.message.reply_text(NOT_MEMBER, reply_markup=JOIN_KB)


async def cmd_votes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    rows = tally()
    if not rows:
        await update.message.reply_text("لسه مفيش أصوات.")
        return
    total = sum(c for _, c in rows)
    txt = f"🗳 <b>أصوات المتاجر ({total})</b>\n\n" + "\n".join(
        f"{i}) {html.escape(n)} — {c}" for i, (n, c) in enumerate(rows, 1))
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
    query = re.sub(r"\s+", " ", (msg.text or "")).strip()
    if context.user_data.pop("await_store", False):
        name = query[:40]
        if len(name) >= 2:
            save_vote(uid, name)
            await msg.reply_text(f"✅ تمام، سجلت اقتراحك: «{name}». شكراً لمشاركتك!")
        return
    if len(query) < 2:
        return
    if len(query) > 100:
        await msg.reply_text("اكتب اسم المنتج في سطر قصير (أقل من 100 حرف).")
        return
    if await gate(context.bot, msg.chat_id, uid, query, context.bot_data):
        await run_search(context.bot, msg.chat_id, uid, query, context.bot_data)


def make_draft(item):
    """Blocking: يبني صورة وبوست جاهزين للنشر من نتيجة أمازون."""
    raw = pb.download_image(item.get("image"))
    image = pb.product_image(item["title"], item["price"], item.get("old"), raw)
    post = pb.build_post(item["title"], item["price"], item.get("old"), item.get("rating"), item["url"])
    return image, post


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    action, _, arg = data.partition(":")
    uid = q.from_user.id
    chat_id = q.message.chat_id if q.message else uid

    if action == "vote":
        await q.answer()
        await context.bot.send_message(chat_id, VOTE_TEXT, parse_mode=ParseMode.HTML, reply_markup=vote_kb())
        return
    if action == "v":
        try:
            name = VOTE_STORES[int(arg)]
        except Exception:
            await q.answer()
            return
        save_vote(uid, name)
        await q.answer("✅ صوتك اتسجل")
        txt = f"✅ صوتك اتسجل لـ «{html.escape(name)}»"
        rk = ranking_text()
        if rk:
            txt += "\n\n🏆 <b>الأكتر طلباً دلوقتي:</b>\n" + rk
        await context.bot.send_message(chat_id, txt, parse_mode=ParseMode.HTML)
        return
    if action == "vc":
        context.user_data["await_store"] = True
        await q.answer()
        await context.bot.send_message(chat_id, "✍️ اكتب اسم المتجر اللي عايزه يتضاف (رسالة واحدة).")
        return
    if action == "joined":
        ok = await is_member(context.bot, uid)
        if not ok:
            await q.answer("لسه مش لاقيك مشترك في القناة. اشترك الأول 🙂", show_alert=True)
            return
        await q.answer("✅ تمام")
        pending = (context.bot_data.get("pending") or {}).pop(uid, None)
        if pending and await gate(context.bot, chat_id, uid, pending, context.bot_data):
            await run_search(context.bot, chat_id, uid, pending, context.bot_data)
        else:
            await context.bot.send_message(chat_id, "أهلاً بيك! اكتب اسم المنتج اللي بتدور عليه 🔎")
        return

    ent = (context.bot_data.get("res") or {}).get(arg)
    if not ent:
        await q.answer("البحث ده انتهى، ابعت اسم المنتج تاني.", show_alert=True)
        return
    query, results = ent
    if action == "more":
        await q.answer()
        await context.bot.send_message(chat_id, render_more(query, results), parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        return
    if action == "draft":
        if uid != OWNER_ID or not db.TOKEN:
            await q.answer("الزرار ده لصاحب القناة بس.", show_alert=True)
            return
        am = next((r for r in results if r["store"]["key"] == "amazon" and r["top"]), None)
        if not am:
            await q.answer("مفيش نتيجة أمازون أعمل منها بوست.", show_alert=True)
            return
        await q.answer("بجهز البوست...")
        try:
            image, post = await asyncio.to_thread(make_draft, am["top"])
            pid = db.new_pid()
            db.store_draft(pid, image, post)
            dbot = Bot(db.TOKEN)
            if image and len(post) <= 1000:
                m = await dbot.send_photo(OWNER_ID, photo=image, caption=post, parse_mode=ParseMode.HTML,
                                          reply_markup=db.make_kb(pid))
            else:
                m = await dbot.send_message(OWNER_ID, post, parse_mode=ParseMode.HTML,
                                            reply_markup=db.make_kb(pid), disable_web_page_preview=True)
            db.set_src(pid, [[OWNER_ID, m.message_id]])
            await context.bot.send_message(chat_id, "✅ البوست اتبعت في شات بوت المسودات (Mix Draft Bot) بأزرار نشر وتعديل.")
        except Exception as e:
            note(f"draft failed: {type(e).__name__} {str(e)[:150]}")
            await context.bot.send_message(
                chat_id, "معرفتش أبعت البوست. افتح بوت المسودات (Mix Draft Bot) وابعتله /start الأول وجرب تاني.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning("handler error: %s %s", type(context.error).__name__, str(context.error)[:120])


def main():
    if not TOKEN:
        raise SystemExit("SEARCH_BOT_TOKEN مش موجود")
    if not OWNER_ID:
        raise SystemExit("OWNER_ID مش موجود")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("votes", cmd_votes))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("compare bot running")
    app.run_polling()


if __name__ == "__main__":
    main()
