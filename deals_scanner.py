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
import re
import math

import requests
from bs4 import BeautifulSoup

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
MIN_DISCOUNT = 40          # أقل نسبة خصم (عروض أقل من كده مش بتتبعت)
MAX_DISCOUNT = 80          # أكتر من كده غالباً سعر قايمة مزيف
HOT_DISCOUNT = 55          # من النسبة دي العرض بيتعلّم "ممتاز"
MIN_PRICE = 120            # أقل سعر للمنتج (نشيل المنتجات الرخيصة اللي مالهاش لازمة)
MIN_RATING = 4.0           # أقل تقييم (لو المنتج عليه تقييم)
MIN_REVIEWS = 15           # أقل عدد تقييمات (لو العدد معروف)
SCAN_EVERY = 5 * 60        # كل قد إيه يعيد الفحص (ثواني)
KEYWORDS_PER_ROUND = 6     # عدد كلمات البحث في الدفعة
MAX_BATCHES = 2            # أقصى عدد دفعات في الجولة (يعني 12 طلب بحث على الأكتر)
MAX_PER_ROUND = 1          # أقصى عدد عروض بتتبعت في الجولة (أحسن واحد كل 5 دقايق)
HOURLY_CAP = 12            # أقصى عدد عروض في الساعة
DAILY_CAP = 150            # أقصى عدد عروض في اليوم
DAILY_REQUESTS = 6000      # أقصى طلبات API في اليوم (حد أمازون 8640)
RATE_PAUSE = 10 * 60       # لو أمازون قالت "بطّأ" (429)، نوقف الفحص المدة دي
MIN_SCORE = 95             # أقل "درجة" للعرض عشان يتبعت (الأضعف مش بيتبعت حتى لو عدّى الفلاتر)
NO_DATA_MIN_PCT = 50       # لو التقييم مش معروف: لازم خصم أكبر
NO_DATA_MIN_PRICE = 200    # ... وسعر أعلى
JUNK_WORDS = (             # منتجات هامشية (إكسسوارات رخيصة / حافظات / لزق...)
    "جراب", "كفر", "حافظة", "حافظه", "واقي شاشة", "لاصق شاشة", "سكرين", "ملصق", "استيكر", "ستيكر",
    "حلق", "خاتم", "اسورة", "إسورة", "سلسلة", "دبوس", "شريط لاصق", "bracelet", "sticker", "screen protector",
    "case for", "phone case", "cover for", "keychain", "ميدالية", "عينة", "sample", "تجريبي",
)
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
    # ماركات ومنتجات مطلوبة
    "سامسونج", "شاومي", "ايفون", "هواوي", "اوبو", "ريلمي", "انفينيكس", "لينوفو", "اتش بي", "ديل",
    "سوني", "ال جي", "توشيبا", "فريش", "شارب", "بلايستيشن", "اكس بوكس", "ايرفريشنر", "مكنسة روبوت",
    "ايربودز", "ساعة ابل", "ريدمي", "ماك بوك", "ايباد", "كاميرا اكشن", "بروجكتور", "سماعة جيمنج",
    "كرسي مكتب", "مكتب", "ديب فريزر", "بوتجاز", "دفاية", "سخان مياه", "طقم حلل", "طقم سفرة", "مشاية",
    "اوزان", "مضرب", "شنطة لاب توب", "حفاضات", "حليب اطفال", "كريم اساس", "روج", "ماسكارا", "برفان",
    "بنطلون", "تيشيرت", "فستان", "قميص", "جزمة", "شبشب", "كوتشي", "ساعة كاسيو", "نظارة شمس",
    "قهوة", "شاي", "زيت", "منظفات", "مساحيق غسيل", "مكملات غذائية", "بروتين", "فيتامين د",
]

BASE = os.path.dirname(os.path.abspath(__file__))
SENT_FILE = os.path.join(BASE, "scanner_sent.json")
LOG_FILE = os.path.join(BASE, "scan_log.txt")

_token = {"value": None, "exp": 0}
_lang = {"on": True}
_deal = {"on": True}
_rev = {"on": True}
_state = {"pause_until": 0}
USAGE_FILE = os.path.join(BASE, "scan_usage.json")
COUPON_DEBUG = os.path.join(BASE, "coupon_debug.txt")


class RateLimited(Exception):
    pass


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
def count_request():
    """بيعدّ طلبات الـAPI في اليوم (عشان منعدّيش الحد)."""
    day = time.strftime("%Y-%m-%d")
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if d.get("day") != day:
        d = {"day": day, "n": 0}
    d["n"] += 1
    try:
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass
    return d["n"]


def requests_today():
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d["n"] if d.get("day") == time.strftime("%Y-%m-%d") else 0
    except Exception:
        return 0


def search(keyword, page=1):
    count_request()
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
    if _rev["on"]:
        body["resources"] += ["customerReviews.count", "customerReviews.starRating"]
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "x-marketplace": MARKETPLACE,
    }
    if _lang["on"] and LANG:
        body["languagesOfPreference"] = [LANG]

    def post():
        r = requests.post(API_URL, json=body, headers=headers, timeout=40)
        if r.status_code == 429:
            raise RateLimited(r.text[:120])
        return r

    r = post()
    if r.status_code != 200 and "languagesOfPreference" in body and "lang" in r.text.lower():
        # اللغة مش مدعومة: كمّل من غيرها
        log.warning("language %s not accepted, continuing without it: %s", LANG, r.text[:150])
        _lang["on"] = False
        body.pop("languagesOfPreference", None)
        r = post()
    if r.status_code == 400 and _rev["on"]:
        # بيانات التقييم مش مدعومة: كمّل من غيرها
        log.warning("customerReviews not accepted, continuing without it: %s", r.text[:150])
        _rev["on"] = False
        body["resources"] = [x for x in body["resources"] if "customerReviews" not in x]
        r = post()
    if r.status_code == 400 and _deal["on"]:
        log.warning("dealDetails not accepted, continuing without it: %s", r.text[:150])
        _deal["on"] = False
        body["resources"] = [x for x in body["resources"] if "dealDetails" not in x]
        r = post()
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
            cr = it.get("customerReviews") or {}
            rating = cr.get("starRating")
            if isinstance(rating, dict):
                rating = rating.get("value")
            try:
                rating = float(rating) if rating is not None else None
            except Exception:
                rating = None
            try:
                reviews = int(cr.get("count")) if cr.get("count") is not None else None
            except Exception:
                reviews = None
            out.append(
                {
                    "rating": rating,
                    "reviews": reviews,
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


# ---------------------------------------------------------------- quality
def is_junk(it):
    t = (it.get("title") or "").lower()
    return any(w.lower() in t for w in JUNK_WORDS)


def is_good(it):
    """عرض يستاهل: خصم حقيقي، سعر محترم، تقييم كويس وعدد تقييمات يطمّن، ومش منتج هامشي."""
    if not (MIN_DISCOUNT <= it["pct"] <= MAX_DISCOUNT):
        return False
    if it["price"] < MIN_PRICE:
        return False
    if is_junk(it):
        return False
    rating, reviews = it.get("rating"), it.get("reviews")
    if rating is None and reviews is None:
        # مفيش أي بيانات تقييم: مانقدرش نتأكد، فنطلب عرض أقوى بكتير
        return it["pct"] >= NO_DATA_MIN_PCT and it["price"] >= NO_DATA_MIN_PRICE
    if rating is not None and rating < MIN_RATING:
        return False
    if reviews is not None and reviews < MIN_REVIEWS:
        return False
    return True


def deal_score(it):
    """ترتيب العروض: خصم أكبر + سعر أعلى + تقييم أحسن."""
    rating = it.get("rating")
    quality = (rating / 5.0) if rating else 0.8
    reviews = it.get("reviews") or 0
    trust = 1.0 + min(math.log10(reviews + 1), 3) / 6  # لحد +50%
    bonus = 1.15 if it.get("badge") else 1.0
    return it["pct"] * math.log10(it["price"] + 10) * quality * trust * bonus


def quota_counts(sent):
    """بيعدّ العروض اللي النسخة دي بعتتها (علامة q) في آخر ساعة وآخر 24 ساعة."""
    now = time.time()
    mine = [v for v in sent.values() if v.get("q")]
    hour = sum(1 for v in mine if now - v["ts"] < 3600)
    day = sum(1 for v in mine if now - v["ts"] < 86400)
    return hour, day


def quota_left(sent):
    """كام عرض لسه مسموح نبعته (حسب حد الساعة وحد اليوم)."""
    hour, day = quota_counts(sent)
    return min(HOURLY_CAP - hour, DAILY_CAP - day)


# ---------------------------------------------------------------- coupons
_COUPON_PATTERNS = [
    r"(?:وفر|وفّر|وفِّر)\s*(?:ج\.?\s?م\.?\s*)?(\d+(?:[.,]\d+)?)\s*(?:%|٪|جنيه|ج\.?\s?م)?[^.\n]{0,30}?(?:القسيمة|قسيمة|كوبون)",
    r"(?:طبّق|تطبيق|طبق|استخدم)\s*(?:ال)?(?:قسيمة|كوبون)\s*(?:بقيمة|خصم)?\s*(?:EGP\s*|ج\.?\s?م\.?\s*)?(\d+(?:[.,]\d+)?)\s*(?:%|٪|جنيه|ج\.?\s?م)?",
    r"Save\s*(?:EGP\s*)?(\d+(?:[.,]\d+)?)\s*(?:%|EGP)?[^.\n]{0,30}?coupon",
    r"Apply\s*(?:EGP\s*)?(\d+(?:[.,]\d+)?)\s*(?:%|EGP)?\s*coupon",
]
_NUM_UNIT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(%|٪|جنيه|ج\.?\s?م|EGP)?")


def _fmt_coupon(val, whole):
    val = val.replace(",", ".")
    if val.endswith(".0"):
        val = val[:-2]
    if "%" in whole or "٪" in whole:
        return f"وفّر {val}%"
    return f"وفّر {val} جنيه"


def extract_coupon(page, asin=""):
    """بيدوّر في صفحة المنتج على كوبون (قسيمة) تحت السعر. بيرجع نص أو None."""
    try:
        soup = BeautifulSoup(page or "", "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        box = soup.select_one("#centerCol") or soup.select_one("#ppd") or soup.select_one("#dp-container") or soup
        # 1) عناصر الكوبون نفسها
        texts = []
        for el in box.select("[id*=coupon], [class*=coupon], [id*=Coupon], [class*=Coupon], #vpcButton"):
            tx = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if tx and tx not in texts:
                texts.append(tx)
        joined = " | ".join(texts)[:600]
        for tx in texts:
            if re.search(r"قسيم|كوبون|coupon", tx, re.I):
                m = _NUM_UNIT.search(tx)
                if m:
                    return _fmt_coupon(m.group(1), m.group(0) + tx[:60])
        # 2) نص منطقة السعر
        text = re.sub(r"\s+", " ", box.get_text(" "))
        for pat in _COUPON_PATTERNS:
            m = re.search(pat, text, re.I)
            if m:
                return _fmt_coupon(m.group(1), m.group(0))
        if joined or re.search(r"قسيمة|coupon", text, re.I):
            # شفنا كلمة كوبون بس مفهمناش الرقم: نسجل عينة عشان نظبطها
            try:
                with open(COUPON_DEBUG, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%F %T')} {asin} :: {joined[:300]}\n")
            except Exception:
                pass
    except Exception as e:
        log.warning("coupon parse failed: %s", type(e).__name__)
    return None


def fetch_coupon(asin):
    try:
        _, page = pb.get_page(f"https://{MARKETPLACE}/dp/{asin}", [])
        return extract_coupon(page, asin)
    except Exception as e:
        log.warning("coupon fetch failed: %s", type(e).__name__)
        return None


# ---------------------------------------------------------------- post
def footer_lines():
    """الفوتر من post_bot لو موجود، وإلا الفوتر القديم (عشان البوت ما يقعش لو الملفات مش متطابقة)."""
    f = getattr(pb, "FOOTER_LINES", None)
    if f:
        return list(f)
    return ["📢 قناة عروض وخصومات", "للمزيد من العروض يوميًا:", pb.CHANNEL_LINK] + list(getattr(pb, "BOT_LINE", []))


def coupon_lines(item):
    c = item.get("coupon")
    if not c:
        return []
    return [f"🎟 <b>كوبون إضافي:</b> {html.escape(c)}", "(فعّل القسيمة من صفحة المنتج قبل الشراء)", ""]


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
            ] + coupon_lines(item) + [
                "🛒 اشتري من هنا:",
                link,
                "",
            ] + footer_lines()
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
        ] + coupon_lines(item) + [
            "🛒 اشتري من هنا:",
            link,
            "",
        ] + footer_lines()
    )


def scan_once():
    """جولة فحص واحدة. يرجع قايمة (item, post_html, image_bytes)."""
    now = time.time()
    if now < _state["pause_until"]:
        note(f"scan paused for {int(_state['pause_until'] - now)}s (rate limit)")
        return []
    used_today = requests_today()
    if used_today >= DAILY_REQUESTS:
        note(f"scan skipped: daily API requests {used_today} reached {DAILY_REQUESTS}")
        return []
    sent = load_sent()
    left = quota_left(sent)
    if left <= 0:
        h, d = quota_counts(sent)
        note(f"scan skipped: cap reached (last hour {h}/{HOURLY_CAP}, last 24h {d}/{DAILY_CAP})")
        return []

    pool = random.sample(KEYWORDS, len(KEYWORDS))
    found = {}
    used = raw = skipped = lowq = 0
    errors = []
    stop = False
    for batch in range(MAX_BATCHES):
        kws = pool[batch * KEYWORDS_PER_ROUND:(batch + 1) * KEYWORDS_PER_ROUND]
        for kw in kws:
            used += 1
            try:
                data = search(kw, random.choice([1, 1, 1, 2, 2, 3, 4, 5]))
            except RateLimited as e:
                _state["pause_until"] = time.time() + RATE_PAUSE
                note(f"RATE LIMITED (429) -> pause {RATE_PAUSE // 60} min: {e}")
                stop = True
                break
            except Exception as e:
                errors.append(f"{kw}: {str(e)[:120]}")
                log.warning("search '%s' failed: %s", kw, e)
                time.sleep(3)
                continue
            for it in parse_items(data):
                raw += 1
                if not is_good(it):
                    lowq += 1
                elif is_new(sent, it):
                    found.setdefault(it["asin"], it)
                else:
                    skipped += 1
            time.sleep(2)
        if stop or len(found) >= max(4, MAX_PER_ROUND * 2):
            break

    ranked = sorted(found.values(), key=deal_score, reverse=True)
    weak = sum(1 for x in ranked if deal_score(x) < MIN_SCORE)
    ranked = [x for x in ranked if deal_score(x) >= MIN_SCORE]
    best = ranked[:min(MAX_PER_ROUND, left)]
    results = []
    coupons = 0
    for it in best:
        it["coupon"] = fetch_coupon(it["asin"])
        coupons += 1 if it["coupon"] else 0
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
        sent[it["asin"]] = {"price": it["price"], "ts": time.time(), "q": 1}
    save_sent(sent)
    msg = (f"scan: keywords={used} items={raw} low_quality={lowq} already_sent={skipped} "
           f"good_new={len(found)} weak_score={weak} sent={len(results)} coupons={coupons} api_today={requests_today()} errors={len(errors)}")
    if errors:
        msg += " | " + " || ".join(errors[:3])
    log.info(msg)
    note(msg)
    return results
