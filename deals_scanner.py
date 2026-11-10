"""
يدوّر على منتجات عليها خصم كبير في أمازون مصر عن طريق الـ API الرسمية (Creators API)،
وبيرجّع قايمة عروض جاهزة تتبعت لقناة المسودات.
الإعدادات من متغيرات البيئة: CREATORS_ID و CREATORS_SECRET (والإصدار CREATORS_VERSION، الافتراضي 3.2).
"""
import os
import json
import time
import html
import logging
import random

import requests

import post_bot as pb
import deal_card

log = logging.getLogger("scanner")

CLIENT_ID = os.environ.get("CREATORS_ID", "")
CLIENT_SECRET = os.environ.get("CREATORS_SECRET", "")
VERSION = os.environ.get("CREATORS_VERSION", "3.2")

LANG = os.environ.get("SCAN_LANG", "ar_AE")  # لغة العناوين. اكتب en_AE للإنجليزي، أو فاضي لو مش مدعومة

MARKETPLACE = "www.amazon.eg"
PARTNER_TAG = "mixdeals-21"
API_URL = "https://creatorsapi.amazon/catalog/v1/searchItems"
TOKEN_URLS = {
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}

# True = بوست مختصر تحت الكارت. False = البوست الكامل زي الأول.
COMPACT_POST = False
MIN_DISCOUNT = 25          # أقل نسبة خصم
HOT_DISCOUNT = 50          # من النسبة دي العرض بيتعلّم "نار"
SCAN_EVERY = 20 * 60       # كل قد إيه يعيد الفحص (ثواني)
KEYWORDS_PER_ROUND = 8     # عدد كلمات البحث في كل جولة
MAX_PER_ROUND = 10         # أقصى عدد عروض بتتبعت في الجولة
RESEND_AFTER = 48 * 3600   # نفس المنتج ما يتبعتش تاني قبل المدة دي (إلا لو السعر نزل أكتر)

# كلمات البحث. عدّل فيها زي ما تحب.
KEYWORDS = [
    "هاتف", "سماعات", "ساعة ذكية", "لاب توب", "شاشة", "تابلت",
    "غسالة", "مكنسة", "قلاية هوائية", "ميكروويف", "عطر", "كاميرا",
    "ألعاب", "حذاء رياضي", "حقيبة", "مكواة", "خلاط", "مروحة",
    "شاحن", "باور بانك", "ماوس", "كيبورد", "راوتر", "طابعة",
    "تلفزيون", "ثلاجة", "مكيف", "سخان", "ماكينة قهوة", "محضر طعام",
    "ملابس رجالي", "ملابس نسائي", "ملابس أطفال", "حذاء", "نظارة", "ساعة يد",
    "مكياج", "عناية بالبشرة", "شامبو", "فيتامينات", "حفاضات", "ألعاب أطفال",
    "أدوات مطبخ", "مفروشات", "إضاءة", "عدة", "دراجة", "حقيبة ظهر",
    "سماعة بلوتوث", "سماعة رأس", "كشاف", "كاميرا مراقبة", "ميزان", "فرن كهربائي",
    "عصارة", "غلاية", "محمصة", "مجفف شعر", "ماكينة حلاقة", "فرشاة أسنان كهربائية",
    "مستحضرات تجميل", "كريم", "صبغة شعر", "مزيل عرق", "منظف", "مناديل",
    "أثاث", "مرتبة", "وسادة", "ستائر", "سجاد", "حقيبة سفر", "ساعة رياضية",
    "ملابس رياضية", "جاكيت", "شنطة", "حذاء رجالي", "حذاء نسائي", "لعبة", "ليجو",
    "كتب", "قرطاسية", "ذاكرة", "هارد ديسك", "كارت ذاكرة", "كابل", "شاشة كمبيوتر",
]

BASE = os.path.dirname(os.path.abspath(__file__))
SENT_FILE = os.path.join(BASE, "scanner_sent.json")
LOG_FILE = os.path.join(BASE, "scan_log.txt")

_token = {"value": None, "exp": 0}
_lang = {"on": True}
_deal = {"on": True}


def note(msg):
    """سطر في ملف scan_log.txt عشان نعرف الفحص عمل إيه."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def enabled():
    return bool(CLIENT_ID and CLIENT_SECRET)


# ---------------------------------------------------------------- auth
def get_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    url = TOKEN_URLS.get(VERSION, TOKEN_URLS["3.2"])
    r = requests.post(
        url,
        json={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": "creatorsapi::default",
        },
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"token status {r.status_code}: {r.text[:150]}")
    d = r.json()
    _token["value"] = d["access_token"]
    _token["exp"] = time.time() + int(d.get("expires_in", 3600))
    return _token["value"]


# ---------------------------------------------------------------- api
def search(keyword, page=1):
    body = {
        "partnerTag": PARTNER_TAG,
        "keywords": keyword,
        "marketplace": MARKETPLACE,
        "itemCount": 10,
        "itemPage": page,
        "minSavingPercent": MIN_DISCOUNT,
        "resources": [
            "images.primary.large",
            "itemInfo.title",
            "offersV2.listings.price",
            "offersV2.listings.availability",
            "offersV2.listings.isBuyBoxWinner",
        ],
    }
    if _deal["on"]:
        body["resources"].append("offersV2.listings.dealDetails")
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "x-marketplace": MARKETPLACE,
    }
    if _lang["on"] and LANG:
        body["languagesOfPreference"] = [LANG]
    r = requests.post(API_URL, json=body, headers=headers, timeout=40)
    if r.status_code != 200 and "languagesOfPreference" in body and "lang" in r.text.lower():
        # اللغة مش مدعومة: كمّل من غيرها
        log.warning("language %s not accepted, continuing without it: %s", LANG, r.text[:150])
        _lang["on"] = False
        body.pop("languagesOfPreference", None)
        r = requests.post(API_URL, json=body, headers=headers, timeout=40)
    if r.status_code != 200 and _deal["on"] and "dealDetails" in r.text:
        log.warning("dealDetails not accepted, continuing without it: %s", r.text[:150])
        _deal["on"] = False
        body["resources"] = [x for x in body["resources"] if "dealDetails" not in x]
        r = requests.post(API_URL, json=body, headers=headers, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"search status {r.status_code}: {r.text[:200]}")
    return r.json()


def parse_items(data):
    """يرجع قايمة dict: asin, title, price, old, pct, image."""
    out = []
    items = ((data or {}).get("searchResult") or {}).get("items") or []
    for it in items:
        try:
            listings = (it.get("offersV2") or {}).get("listings") or []
            if not listings:
                continue
            lst = next((x for x in listings if x.get("isBuyBoxWinner")), listings[0])
            av = (lst.get("availability") or {}).get("type")
            if av and av != "IN_STOCK":
                continue
            price_obj = lst.get("price") or {}
            price = (price_obj.get("money") or {}).get("amount")
            basis = ((price_obj.get("savingBasis") or {}).get("money") or {}).get("amount")
            pct = (price_obj.get("savings") or {}).get("percentage")
            if not price or not basis or not pct:
                continue
            title = ((it.get("itemInfo") or {}).get("title") or {}).get("displayValue")
            if not title or float(basis) <= float(price):
                continue
            image = (((it.get("images") or {}).get("primary") or {}).get("large") or {}).get("url")
            badge = ((lst.get("dealDetails") or {}).get("badge")) or ""
            out.append(
                {
                    "badge": badge,
                    "asin": it["asin"],
                    "title": title,
                    "price": float(price),
                    "old": float(basis),
                    "pct": int(pct),
                    "image": image,
                }
            )
        except Exception as e:
            log.warning("parse item failed: %s", type(e).__name__)
    return out


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
    d = {k: v for k, v in d.items() if now - v["ts"] < 7 * 86400}
    try:
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception as e:
        log.warning("save_sent failed: %s", type(e).__name__)


def is_new(sent, item):
    prev = sent.get(item["asin"])
    if not prev:
        return True
    if time.time() - prev["ts"] >= RESEND_AFTER:
        return True
    return item["price"] < prev["price"] * 0.95


# ---------------------------------------------------------------- post
def build_deal_post(item, compact=False):
    hot = item["pct"] >= HOT_DISCOUNT
    head = f"🚨 خصم {item['pct']}% — عرض ممتاز ما يتفوتش!" if hot else f"🔥 خصم {item['pct']}%"
    link = pb.affiliate(MARKETPLACE, item["asin"])
    if compact:
        # الكارت فيه العنوان والسعر، فالكلام هنا مختصر
        return "\n".join(
            [
                head,
                f"{pb.fmt(item['price'])} بدل {pb.fmt(item['old'])}",
                "",
                "🛒 اشتري من هنا:",
                link,
                "",
                "⏳ السعر والكمية ممكن يتغيروا في أي وقت.",
                "",
            ] + pb.FOOTER_LINES
        )
    return "\n".join(
        [
            head,
            "",
            f"<b>{html.escape(pb.short_title(item['title']))}</b>",
            "",
            f"💰 السعر: <b>{pb.fmt(item['price'])}</b>",
            f"🏷️ كان: <s>{pb.fmt(item['old'])}</s> (خصم {item['pct']}%)",
            "",
            "🛒 اشتري من هنا:",
            link,
            "",
            "⏳ السعر والكمية ممكن يتغيروا في أي وقت.",
            "",
        ] + pb.FOOTER_LINES
    )


def scan_once():
    """جولة فحص واحدة. يرجع قايمة (item, post_html, image_bytes)."""
    sent = load_sent()
    pool = random.sample(KEYWORDS, len(KEYWORDS))
    found = {}
    used = raw = skipped = 0
    errors = []
    # لو الدفعة الأولى مجبتش حاجة جديدة، كمّل بدفعات تانية (لحد 4 دفعات)
    for batch in range(4):
        kws = pool[batch * KEYWORDS_PER_ROUND:(batch + 1) * KEYWORDS_PER_ROUND]
        for kw in kws:
            used += 1
            try:
                data = search(kw, random.choice([1, 1, 2, 3]))
            except Exception as e:
                errors.append(f"{kw}: {str(e)[:120]}")
                log.warning("search '%s' failed: %s", kw, e)
                time.sleep(3)
                continue
            for it in parse_items(data):
                raw += 1
                if it["pct"] >= MIN_DISCOUNT and is_new(sent, it):
                    found.setdefault(it["asin"], it)
                else:
                    skipped += 1
            time.sleep(2)
        if len(found) >= max(3, MAX_PER_ROUND // 2):
            break

    best = sorted(found.values(), key=lambda x: x["pct"], reverse=True)[:MAX_PER_ROUND]
    results = []
    for it in best:
        image = None
        card = False
        if it["image"]:
            raw_img = pb.download_image(it["image"])
            if raw_img:
                image = deal_card.make_card(it, raw_img)
                card = bool(image)
                if not image:
                    image = pb.prepare_image(raw_img)
        results.append((it, build_deal_post(it, compact=COMPACT_POST and card), image))
        sent[it["asin"]] = {"price": it["price"], "ts": time.time()}
    save_sent(sent)
    msg = f"scan: keywords={used} with_discount={raw} already_sent={skipped} new={len(results)} errors={len(errors)}"
    if errors:
        msg += " | " + " || ".join(errors[:3])
    log.info(msg)
    note(msg)
    return results
