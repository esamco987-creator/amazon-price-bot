import os
import re
import io
import json
import html
import random
import string
import asyncio
import logging
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "telegram", "apscheduler", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("post_bot")

TOKEN = os.environ.get("POST_BOT_TOKEN", "")
try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

CHANNEL = "@PromoCodeOnlain"
MY_TAG = "mixdeals-21"
CHANNEL_LINK = "https://t.me/PromoCodeOnlain"
SEARCH_BOT_LINK = "https://t.me/Mix_Compare_bot"
BOT_LINE = []  # (قديم) السطر بقى جوه FOOTER_LINES
FOOTER_LINES = []  # الأزرار تحت البوست بتغني عن السطور
BASE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE, "allowed_users.json")
LOGO_FILE = os.path.join(BASE, "logo.png")

CANVAS = 1080
PRODUCT_BOX = 0.80
LOGO_RATIO = 0.16

OPENERS = [   # قديمة (احتياطي لو مفيش خصم ولا اسم منتج)
    "🔥 شوف العرض ده!",
    "⚡ متفوتش العرض ده!",
    "🛍️ من عروض اليوم",
    "✅ متاح حاليًا",
]


def short_name(title, words=4, limit=34):
    """اسم المنتج المختصر (أول كام كلمة) للجُمل اللي بتبدأ بيها البوستات."""
    t = re.sub(r"\s+", " ", re.sub(r"[\(\[\|\-–—،,].*$", "", title or "")).strip()
    t = " ".join(t.split()[:words])
    return t[:limit].strip()


def opener(pct=None, title="", hot=False):
    """سطر بداية البوست: بيتنوع بين الصياغات (خصم بالنسبة / عرض ممتاز على / توفير حقيقي / عروض اليوم...)."""
    name = short_name(title)
    pool = [
        "🛍️ من عروض اليوم",
        "👀 شوف العرض ده",
        "⏳ الحق العرض ده قبل ما يخلص",
        "✅ متاح حاليًا",
        "🔥 متفوتش العرض ده",
    ]
    if pct and pct >= 5:
        pool += [f"🔥 عرض بخصم {pct}%", f"💰 عرض توفير حقيقي — خصم {pct}%", f"🏷️ خصم {pct}% دلوقتي"]
        if pct >= 50 or hot:
            pool += [f"🚨 عرض ممتاز — خصم {pct}%", "⏳ الحق العرض ده قبل ما يخلص"]
    if name and pct and pct >= 20:
        pool += [f"⭐ عرض ممتاز على {name}", f"💥 عرض توفير حقيقي على {name}"]
    return random.choice(pool)


INTROS = [
    "🔔 تنبيه عرض جديد! 🔔",
    "🚀 عرض طاير النهارده! 🚀",
    "💸 وفّر فلوسك مع العرض ده! 💸",
    "🌟 اختيار اليوم من عروضنا! 🌟",
    "📢 يا جماعة عرض مهم! 📢",
    "🎁 هدية من أمازون ليكم! 🎁",
    "👀 بصّوا على السعر ده! 👀",
    "🔥 ولعت الأسعار النهارده! 🔥",
    "✨ عرض يستاهل تشوفه! ✨",
    "🛒 لو كنت محتاجه، دلوقتي وقته! 🛒",
    "⏰ العرض شغال دلوقتي! ⏰",
    "🏷️ خصم حقيقي مش كلام! 🏷️",
    "🧨 سعر ضرب الأرض! 🧨",
    "💪 عرض قوي ومضمون! 💪",
    "😍 ده لازم يتشاف! 😍",
    "🚨 عرض نار ومش هيستنى! 🚨",
    "🔥 الحق قبل ما الكمية تخلص! 🔥",
    "💥 سعر مجنون النهارده! 💥",
    "⚡ فرصة متتعوضش، الحق! ⚡",
    "🤯 مش مصدّق السعر ده! 🤯",
    "🛍️ يا جماعة ده لازم ينزل عندكم! 🛍️",
    "😱 الخصم ده نادر، شوفوه! 😱",
    "💣 عرض قنبلة على أمازون! 💣",
    "🏃 الحق العرض ده! 🏃",
    "🎯 أحسن سعر شفناه لحد دلوقتي! 🎯",
]
_last_intro = [None]


def intro():
    """جملة بداية عشوائية من القايمة (من غير ما تتكرر مرتين ورا بعض)."""
    pool = [x for x in INTROS if x != _last_intro[0]] or INTROS
    _last_intro[0] = random.choice(pool)
    return _last_intro[0]


# (كلمات في اسم المنتج، إيموجي، هاشتاجات)
CATEGORIES = [
    (("سماع", "ايربود", "airpods", "هيدفون", "headphone", "earbuds"), "🎧", "#سماعات #إلكترونيات"),
    (("موبايل", "تليفون", "هاتف", "ايفون", "iphone", "جالكسي", "galaxy", "شاومي", "phone"), "📱", "#موبايلات #إلكترونيات"),
    (("لابتوب", "laptop", "ماك بوك", "macbook", "كمبيوتر"), "💻", "#لابتوب #إلكترونيات"),
    (("تلفزيون", "شاشة", "تليفزيون", "tv"), "📺", "#تلفزيونات #إلكترونيات"),
    (("ساعة", "ساعه", "watch"), "⌚", "#ساعات #إلكترونيات"),
    (("كاميرا", "camera"), "📷", "#كاميرات #إلكترونيات"),
    (("بلايستيشن", "playstation", "xbox", "جيم", "ألعاب", "العاب"), "🎮", "#ألعاب"),
    (("مكنسة", "غسالة", "ثلاجة", "مروحة", "خلاط", "قلاية", "مكواة", "سخان", "بوتاجاز", "ميكروويف", "فرن", "كبة"), "🏠", "#أجهزة_منزلية"),
    (("عطر", "برفان", "perfume"), "🌸", "#عطور"),
    (("شامبو", "كريم", "مكياج", "سيروم", "غسول", "صابون", "ماسكارا", "بشرة", "شعر"), "💄", "#عناية #جمال"),
    (("حفاض", "بامبرز", "pampers", "مولفكس", "molfix", "طفل", "أطفال"), "👶", "#أطفال"),
    (("حذاء", "جزمة", "شبشب", "كوتشي", "sneaker", "shoe"), "👟", "#أحذية #موضة"),
    (("تيشيرت", "قميص", "بنطلون", "جاكيت", "هودي", "فستان", "شنطة", "حقيبة"), "👕", "#ملابس #موضة"),
    (("قهوة", "شاي", "زيت", "بسكويت", "شوكولاتة", "نسكافيه", "تونة", "سمن"), "🛒", "#بقالة"),
]


def category(title):
    t = (title or "").lower()
    for keys, emoji, tags in CATEGORIES:
        if any(k in t for k in keys):
            return emoji, tags
    return "🛍️", "#عروض"


def features_line(title, emoji):
    """سطر المميزات: الأجزاء اللي بعد أول فاصلة في عنوان المنتج (لو موجودة)."""
    t = re.sub(r"\s+", " ", title or "").strip()
    parts = [p.strip(" .…-–") for p in re.split(r"[،,]", t)]
    parts = [p for p in parts[1:] if 2 <= len(p) <= 40]
    if not parts:
        return None
    s = "، ".join(parts[:2])
    return f"{emoji} {html.escape(s[:70])}"


def deal_post(title, price, old, link, cl=None, compact=False, footer=None):
    """شكل البوست الموحد: جملة بداية + عنوان بالخصم + مميزات + السعر + الكمية + اللينك + هاشتاجات."""
    pct = round((old - price) / old * 100) if (price and old and old > price) else None
    emoji, tags = category(title)
    name = short_name(title, words=6, limit=50)
    lines = [intro(), ""]
    if name:
        if pct and pct >= 5:
            lines.append(f"🔥 خصم {pct}% على <b>{html.escape(name)}</b>")
        else:
            lines.append(f"🔥 عرض على <b>{html.escape(name)}</b>")
    if not compact:
        f = features_line(title, emoji)
        if f:
            lines.append(f)
    if price:
        if old and old > price:
            lines.append(f"💰 <b>{fmt(price)}</b> بدل <b>{fmt(old)}</b>")
        else:
            lines.append(f"💰 السعر: <b>{fmt(price)}</b>")
    lines.append("⏳ العرض على أمازون مصر لحد نفاد الكمية")
    lines.append("")
    lines += list(cl or [])
    lines += ["🛒 اشتري من هنا:", link, "", f"{tags} #أمازون"]
    lines += list(footer or [])
    return "\n".join(lines)


CLOSERS = [
    "⏳ الكمية والسعر ممكن يتغيروا في أي وقت.",
    "👌 اطلبه قبل ما يخلص.",
    "🚚 تأكد من السعر النهائي قبل الشراء.",
    "✅ شوف التفاصيل من الرابط.",
]

UA_CHROME = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
UA_BOT = "Mozilla/5.0 (compatible; TelegramBot/1.0; +https://telegram.org)"

HEADER_STRATEGIES = [
    {"User-Agent": UA_CHROME},
    {
        "User-Agent": UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
    },
    {"User-Agent": UA_BOT},
]

AMAZON_HOSTS = ("amazon.", "amzn.", "a.co", "link.amazon")


# ---------------------------------------------------------------- users
def load_users():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_users(data):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.warning("save_users failed: %s", e)


ALLOWED = load_users()

DRAFTS = os.path.join(BASE, "drafts")
PID_RE = re.compile(r"[a-z0-9]{8}")


def new_pid():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _dpath(pid, ext):
    return os.path.join(DRAFTS, f"{pid}.{ext}")


def store_draft(pid, uid, image, post, src=None):
    os.makedirs(DRAFTS, exist_ok=True)
    with open(_dpath(pid, "json"), "w", encoding="utf-8") as f:
        json.dump(
            {"uid": uid, "post": post, "image": bool(image), "src": src or []},
            f,
            ensure_ascii=False,
        )
    if image:
        with open(_dpath(pid, "jpg"), "wb") as f:
            f.write(image)


def load_draft(pid):
    if not PID_RE.fullmatch(pid or ""):
        return None
    try:
        with open(_dpath(pid, "json"), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    img = None
    if d.get("image"):
        try:
            with open(_dpath(pid, "jpg"), "rb") as f:
                img = f.read()
        except Exception:
            img = None
    d["image"] = img
    return d


def set_src(pid, src):
    """يحفظ أماكن رسايل المسودة في قناة المسودات عشان تتمسح بعد النشر."""
    d = load_draft(pid)
    if d:
        store_draft(pid, d["uid"], d["image"], d["post"], src)


async def clear_src(bot, item):
    for chat, mid in item.get("src") or []:
        try:
            await bot.delete_message(chat, mid)
        except Exception:
            pass


def drop_draft(pid):
    if not PID_RE.fullmatch(pid or ""):
        return
    for ext in ("json", "jpg"):
        try:
            os.remove(_dpath(pid, ext))
        except OSError:
            pass


def plain(post):
    return html.unescape(re.sub(r"<[^>]+>", "", post))


def make_kb(pid):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ نشر", callback_data=f"pub:{pid}"),
                InlineKeyboardButton("✏️ تعديل", callback_data=f"ed:{pid}"),
            ],
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"cx:{pid}")],
        ]
    )


def is_allowed(uid):
    return uid == OWNER_ID or str(uid) in ALLOWED


# ---------------------------------------------------------------- helpers
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")


def parse_price(text):
    if not text:
        return None
    t = text.translate(AR_DIGITS)
    m = re.search(r"\d[\d,]*\.?\d*", t)
    if not m:
        return None
    num = m.group(0).replace(",", "")
    try:
        v = float(num)
    except ValueError:
        return None
    return v if v > 0 else None


def fmt(v):
    if v == int(v):
        return f"{int(v):,} جنيه"
    return f"{v:,.2f} جنيه"


def first_text(soup, selectors):
    for sel in selectors:
        for el in soup.select(sel):
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return ""


def find_asin(text):
    if not text:
        return None
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", text)
    return m.group(1) if m else None


def affiliate(host, asin):
    return f"https://{host}/dp/{asin}?tag={MY_TAG}"


def is_amazon_host(host):
    host = (host or "").lower()
    return any(p in host for p in AMAZON_HOSTS)


def get_page(url, debug):
    """Try several header strategies. Returns (final_url, html) of the best page."""
    best = (url, "")
    for i, headers in enumerate(HEADER_STRATEGIES, 1):
        try:
            r = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
            body = r.text or ""
            has_title = "productTitle" in body
            debug.append(f"محاولة {i}: حالة {r.status_code}، طول {len(body)}، عنوان {'✔' if has_title else '✘'}")
            log.info("fetch try %s status=%s len=%s title=%s", i, r.status_code, len(body), has_title)
            if len(body) > len(best[1]) or (r.url and not best[1]):
                best = (r.url, body)
            if r.status_code == 200 and has_title:
                return r.url, body
        except Exception as e:
            debug.append(f"محاولة {i}: خطأ {type(e).__name__}")
            log.warning("fetch try %s error: %s", i, e)
    return best


def clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    pat = r"\s*[:：|\-–]?\s*(?:amazon\.[a-z.]+|أمازون)"
    m = re.search(pat, t, flags=re.I)
    if m:
        before = t[: m.start()].strip(" :：|-–")
        t = before if before else re.sub(pat, " ", t, flags=re.I)
    t = re.sub(r"amazon", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" :：|-–")


# أقسام الصفحة اللي فيها أسعار منتجات تانية (مقترحات/إعلانات) ومينفعش نقرا منها سعر المنتج
_BAD_SECTIONS = ("sims", "similar", "sp_detail", "sponsored", "anonCarousel", "bundle", "percolate", "rhf",
                 "recently", "customer-reviews", "a-carousel")
_PRICE_ROOTS = (
    "#corePriceDisplay_desktop_feature_div", "#corePrice_feature_div", "#apex_desktop",
    "#apex_offerDisplay_desktop", "#desktop_buybox", "#buybox", "#qualifiedBuybox", "#tabular-buybox",
    "#centerCol", "#ppd",
)


def _price_roots(soup):
    for sel in _PRICE_ROOTS:
        root = soup.select_one(sel)
        if root is not None:
            yield root


def _bad_section(el, root):
    for a in el.parents:
        if a is root:
            break
        ident = " ".join(filter(None, [a.get("id", ""), " ".join(a.get("class", []) or [])])).lower()
        if ident and any(b.lower() in ident for b in _BAD_SECTIONS):
            return True
    return False


def scoped_price(soup, strike):
    """سعر المنتج الأساسي (strike=False) أو سعر القائمة المشطوب (strike=True) من صندوق الشراء بس."""
    for root in _price_roots(soup):
        if strike:
            els = root.select("span.a-price.a-text-price, span[data-a-strike='true'], .basisPrice .a-price")
        else:
            els = root.select(".a-price:not(.a-text-price)")
        for el in els:
            if _bad_section(el, root):
                continue
            if not strike and el.get("data-a-strike") == "true":
                continue
            off = el.select_one(".a-offscreen")
            v = parse_price(off.get_text(" ", strip=True)) if off else None
            if v:
                return v
    return None


def read_product(page):
    soup = BeautifulSoup(page, "html.parser")

    title = first_text(soup, ["#productTitle", "span#productTitle", "h1#title span"])
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"]
    if not title and soup.title and soup.title.string:
        title = soup.title.string
    title = clean_title(title)

    price = scoped_price(soup, strike=False)
    if not price:
        price = parse_price(first_text(soup, ["#priceblock_ourprice", "#priceblock_dealprice", "#price_inside_buybox"]))

    old = scoped_price(soup, strike=True)
    if not old:
        old = parse_price(first_text(soup, ["#listPrice"]))
    if price and not old:
        # أمازون كاتبة نسبة الخصم بس: نحسب السعر القديم منها
        for root in _price_roots(soup):
            el = root.select_one(".savingPercentage, .savingsPercentage")
            m = re.search(r"(\d{1,2})\s*%", (el.get_text(" ") if el else "").translate(AR_DIGITS))
            if m and 3 <= int(m.group(1)) <= 90:
                old = round(price / (1 - int(m.group(1)) / 100), 2)
                break
    if price and old and old <= price:
        old = None

    rating = ""
    el = soup.select_one("#acrPopover")
    if el and el.get("title"):
        rating = el["title"]
    if not rating:
        rating = first_text(soup, ["span.a-icon-alt"])
    m = re.search(r"\d[.,٫]?\d*", (rating or "").translate(AR_DIGITS))
    rating = m.group(0).replace(",", ".") if m else ""

    img = ""
    tag = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
    if tag:
        dyn = tag.get("data-a-dynamic-image")
        if dyn:
            try:
                d = json.loads(html.unescape(dyn))
                img = max(d.items(), key=lambda kv: kv[1][0] * kv[1][1])[0]
            except Exception:
                img = ""
        if not img:
            img = tag.get("data-old-hires") or tag.get("src") or ""
    if not img:
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            img = og["content"]
    if not img:
        for sel in ("#imgTagWrapperId img", "#main-image", "#ebooksImgBlkFront", "img[data-old-hires]"):
            t = soup.select_one(sel)
            if t:
                img = t.get("data-old-hires") or t.get("src") or ""
                if img.startswith("http"):
                    break
                img = ""
    if not img:
        for pat in (r'"hiRes"\s*:\s*"(https:[^"]+)"', r'"large"\s*:\s*"(https:[^"]+)"'):
            m = re.search(pat, page)
            if m:
                img = m.group(1).replace("\\/", "/")
                break
    if not img:
        for m in re.finditer(r"https://m\.media-amazon\.com/images/I/[A-Za-z0-9%+_-]{8,}[^\"'\s\\<>]*?\.(?:jpg|jpeg|png)", page):
            if "sprite" not in m.group(0) and "pixel" not in m.group(0):
                img = m.group(0)
                break

    return title, price, old, rating, img


def full_size(url):
    return re.sub(r"\._[^/]*?_\.", ".", url) if url else url


def fetch_bytes(url):
    for headers in ({"User-Agent": UA_CHROME}, {"User-Agent": UA_DESKTOP}):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except Exception:
            pass
    return None


def download_image(url):
    if not url:
        return None
    data = fetch_bytes(full_size(url))
    if not data:
        data = fetch_bytes(url)
    return data


def prepare_image(data):
    try:
        im = Image.open(io.BytesIO(data))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg
        im = im.convert("RGB")
        box = int(CANVAS * PRODUCT_BOX)
        im.thumbnail((box, box), Image.LANCZOS)
        canvas = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
        canvas.paste(im, ((CANVAS - im.width) // 2, (CANVAS - im.height) // 2))
        if os.path.exists(LOGO_FILE):
            logo = Image.open(LOGO_FILE).convert("RGBA")
            w = int(CANVAS * LOGO_RATIO)
            logo = logo.resize((w, int(w * logo.height / logo.width)), Image.LANCZOS)
            pad = 28
            canvas.paste(logo, (CANVAS - logo.width - pad, CANVAS - logo.height - pad), logo)
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        log.warning("prepare_image failed: %s", e)
        return None


def coupon_final(price, coupon):
    """من نص الكوبون ("وفّر 7.65 جنيه" أو "وفّر 5%") يحسب السعر بعد الكوبون. بيرجع None لو مقدرش."""
    try:
        if not price or not coupon:
            return None
        m = re.search(r"(\d+(?:[.,]\d+)?)", coupon)
        if not m:
            return None
        v = float(m.group(1).replace(",", "."))
        if "%" in coupon or "٪" in coupon:
            final = price * (1 - v / 100.0)
        else:
            final = price - v
        if v <= 0 or final <= 0 or final >= price:
            return None
        return round(final, 2)
    except Exception:
        return None


def coupon_block(price, coupon):
    """سطور الكوبون في البوست: الكوبون + السعر بعد الكوبون (لو اتحسب)."""
    if not coupon:
        return []
    lines = [f"🎟 <b>كوبون إضافي:</b> {html.escape(coupon)}", "(فعّل الكوبون من صفحة المنتج قبل الشراء)"]
    final = coupon_final(price, coupon)
    if final:
        lines += [f"✅ <b>السعر بعد الكوبون: {fmt(final)}</b>"]
    return lines + [""]


# True = بوست مختصر تحت الكارت. False = البوست الكامل (العنوان والسعر والخصم) زي الأول.
COMPACT_POST = False


def product_card(title, price, old, raw, extra=None):
    """كارت المنتج: اسم + سعر (+ خصم وسعر قديم وكوبون لو موجودين). None لو مفيش صورة أو سعر."""
    if not raw or not price or not title:
        return None
    try:
        import deal_card
        pct = 0
        if old and old > price:
            pct = int(round((old - price) / old * 100))
        if pct < 1:
            pct, old = 0, None
        item = {"title": title, "price": price, "old": old, "pct": pct}
        if not pct:
            item["badge"] = "متاح حاليًا"
        item.update({k: v for k, v in (extra or {}).items() if v})
        return deal_card.make_card(item, raw)
    except Exception as e:
        log.warning("card failed: %s", type(e).__name__)
        return None


def product_image(title, price, old, raw, extra=None):
    """كارت العرض (لو فيه خصم) وإلا الصورة العادية على خلفية بيضاء."""
    if not raw:
        return None
    return product_card(title, price, old, raw, extra) or prepare_image(raw)


def short_title(t, limit=95):
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0] + "…"


def build_post(title, price, old, rating, link, compact=False, coupon=None):
    return deal_post(title, price, old, link, coupon_block(price, coupon), compact, FOOTER_LINES)


SHOW_COUPON_BUTTON = False  # زرار "أكواد الخصم" تحت البوستات (غيّرها لـ True لو عايزه يرجع)
_names = {}


async def _bot_username(key, token):
    if _names.get(key):
        return _names[key]
    if not token:
        return None
    try:
        from telegram import Bot
        async with Bot(token) as b:
            me = await b.get_me()
        _names[key] = me.username
        return me.username
    except Exception as e:
        log.warning("username lookup failed (%s): %s", key, type(e).__name__)
        return None


async def channel_kb():
    """أزرار تحت كل بوست في القناة: تتبع السعر + البحث، وتحتهم أكواد الخصم."""
    row = []
    tracker = await _bot_username("tracker", os.environ.get("BOT_TOKEN", ""))
    if tracker:
        row.append(InlineKeyboardButton("📉 تتبع سعر أي منتج", url=f"https://t.me/{tracker}"))
    row.append(InlineKeyboardButton("🔎 ابحث عن أي منتج", url="https://t.me/Mix_Compare_bot"))
    rows = [row]
    coupons = (await _bot_username("coupons", os.environ.get("COUPON_BOT_TOKEN", ""))) if SHOW_COUPON_BUTTON else None
    if coupons:
        rows.append([InlineKeyboardButton("🎟️ أكواد الخصم", url=f"https://t.me/{coupons}")])
    return InlineKeyboardMarkup(rows)


async def send_post(bot, chat_id, image, post, markup=None):
    post = post.rstrip()
    if markup is None and chat_id == CHANNEL:
        markup = await channel_kb()
    if image and len(post) <= 1000:
        await bot.send_photo(chat_id, photo=image, caption=post, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        if image:
            await bot.send_photo(chat_id, photo=image)
        await bot.send_message(
            chat_id,
            post,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    if chat_id == CHANNEL:
        try:
            import repost_store
            repost_store.record(image, post)     # لبوت إعادة النشر
        except Exception as e:
            log.warning("repost record skipped: %s", type(e).__name__)


# ---------------------------------------------------------------- product pipeline
def resolve(url, headers):
    """Follow redirects by hand; tolerate non-http redirect targets."""
    cur = url
    body = ""
    for _ in range(10):
        r = requests.get(cur, headers=headers, timeout=25, allow_redirects=False)
        body = r.text or ""
        loc = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            nxt = urljoin(cur, loc)
            if not nxt.lower().startswith(("http://", "https://")):
                m = re.search(r"https?://[^\s\"']+", unquote(loc))
                if not m:
                    return cur, body
                nxt = m.group(0)
            cur = nxt
            continue
        break
    return cur, body


_TRACK = {
    "linkcode", "linkid", "ref", "ref_", "ascsubtag", "btn_type", "btn_ref", "srctok", "creative", "creativeasin",
    "camp", "_encoding", "content-id", "pd_rd_r", "pd_rd_w", "pd_rd_wg", "pf_rd_p", "pf_rd_r", "pf_rd_s", "pf_rd_t",
    "pf_rd_i", "pf_rd_m", "psc", "qid", "sr", "crid", "sprefix", "th", "language", "social_share", "gclid", "fbclid",
}
PROMO_RE = re.compile(r"/promotion/psp/([A-Za-z0-9]+)")
AMZ_URL_RE = re.compile(r"https?://(?:www\.)?amazon\.eg/[^\s\"'<>\\]+")


def _clean_amazon_url(final):
    """بيشيل بارامترات التتبع من لينك أمازون (بحث/قسم/عروض) ويحط كودك."""
    try:
        base, _, query = final.partition("?")
        query = query.split("#", 1)[0].split(";", 1)[0]
        keep = [p for p in query.split("&") if p and p.split("=", 1)[0].lower() not in _TRACK
                and p.split("=", 1)[0].lower() != "tag"]
        keep.append(f"tag={MY_TAG}")
        return base + "?" + "&".join(keep)
    except Exception:
        return final


def process_page_link(final, body, original, debug, title_hint=None):
    """لينك مش منتج واحد (صفحة عروض مجمّعة "وفّر 30% على ..." أو بحث/قسم): كارت + بوست باللينك بكودك."""
    import deal_card
    host = "www.amazon.eg"
    pm = PROMO_RE.search(final) or PROMO_RE.search(original) or PROMO_RE.search(body.replace("\\/", "/"))
    if pm:
        link = f"https://{host}/promotion/psp/{pm.group(1)}?tag={MY_TAG}"
        title = dates = None
        imgs = []
        try:
            import promo_scanner as promos
            info = promos.load_promo(link)
            if info:
                title, dates = info.get("title"), info.get("dates")
                if title and promos._junk_title(title):
                    title = None
                for p in (info.get("products") or [])[:8]:
                    if len(imgs) >= 4:
                        break
                    raw = download_image(p["image"]) if p.get("image") else None
                    if raw:
                        imgs.append(raw)
        except Exception as e:
            debug.append(f"قراءة صفحة العرض: {type(e).__name__}")
        dbg_html = (info or {}).get("body") if "info" in locals() and info else None
        title = (title_hint or "").strip() or title or "عرض خاص على أمازون"
        try:
            image = deal_card.make_promo_card(title, imgs, dates) if imgs else deal_card.make_text_promo_card(title, dates)
        except Exception:
            image = None
        lines = [f"🎁 <b>{html.escape(title)}</b>"]
        if dates:
            lines.append(f"📅 {html.escape(dates)}")
        lines += ["", "ℹ️ شروط العرض بتتطبق في عربة التسوق. راجعها في الصفحة.", "",
                  "🛒 افتح العرض من هنا:", link, ""] + FOOTER_LINES
        return True, {"image": image, "post": "\n".join(lines).rstrip(), "price": None, "title": title, "old": None,
                      "rating": "", "link": link, "asin": None, "host": host, "coupon": None, "debug_html": dbg_html}
    # صفحة أمازون عامة (بحث/قسم/عروض اليوم)
    if "amazon." in urlparse(final).netloc.lower():
        link = _clean_amazon_url(final)
        title = ""
        try:
            _, page = get_page(final, debug)
            if page:
                soup = BeautifulSoup(page, "html.parser")
                og = soup.find("meta", property="og:title")
                title = clean_title((og.get("content") if og and og.get("content") else "") or
                                    (soup.title.string if soup.title and soup.title.string else ""))
        except Exception:
            title = ""
        title = re.sub(r"^[\W_]*eg\s*[:：]\s*", "", title or "", flags=re.I).strip()
        title = (title_hint or "").strip() or title
        head = f"🔥 <b>{html.escape(title)}</b>" if title else "🔥 <b>عروض على أمازون</b>"
        lines = [head, "", "🛒 افتح العرض من هنا:", link, ""] + FOOTER_LINES
        return True, {"image": None, "post": "\n".join(lines).rstrip(), "price": None, "title": title, "old": None,
                      "rating": "", "link": link, "asin": None, "host": host, "coupon": None}
    return None


def process_link(url, title_hint=None):
    """Blocking. Returns (ok, data_or_message). title_hint: عنوان تكتبه إنت لصفحات العروض المجمّعة."""
    debug = []
    final, body, asin = url, "", None
    pages = []
    for headers in HEADER_STRATEGIES:
        try:
            final, body = resolve(url, headers)
        except Exception as e:
            debug.append(f"فتح الرابط: {type(e).__name__}")
            continue
        if "amazon." not in urlparse(final).netloc.lower():
            # لينك مختصر مش بيحوّل مباشرة: نلاقي لينك أمازون جوه الصفحة
            m = AMZ_URL_RE.search(body.replace("\\/", "/"))
            if m:
                final = m.group(0)
        pages.append((final, body))
        if PROMO_RE.search(final) or PROMO_RE.search(url):
            asin = find_asin(final) or find_asin(url)     # صفحة عرض: ASIN جوه الصفحة بتاع منتج عشوائي مش المقصود
        else:
            asin = find_asin(final) or find_asin(url) or find_asin(body)
        if asin:
            break
    if not asin:
        for f2, b2 in pages:
            res = process_page_link(f2, b2, url, debug, title_hint)
            if res:
                return res
        return False, "معرفتش أوصل لصفحة المنتج من الرابط ده.\n" + "\n".join(debug)

    host = urlparse(final).netloc.lower()
    if "amazon." not in host or host.startswith("link."):
        host = "www.amazon.eg"

    target = f"https://{host}/dp/{asin}"
    final_url, page = get_page(target, debug)
    if not page:
        return False, "أمازون مردتش على البوت.\n" + "\n".join(debug)

    title, price, old, rating, img = read_product(page)
    log.info("parsed title=%s price=%s old=%s rating=%s img=%s", bool(title), price, old, rating, bool(img))
    if not title:
        return False, "الصفحة اتفتحت بس معرفتش أقرا اسم المنتج.\n" + "\n".join(debug)

    link = affiliate(host, asin)
    raw = download_image(img)
    try:
        import deals_scanner
        coupon = deals_scanner.extract_coupon(page, asin)
    except Exception:
        coupon = None
    card = product_card(title, price, old, raw, {"rating": rating, "coupon": coupon})
    image = card or (prepare_image(raw) if raw else None)
    post = build_post(title, price, old, rating, link, compact=COMPACT_POST and bool(card), coupon=coupon)
    return True, {"image": image, "post": post, "price": price, "title": title, "old": old, "rating": rating,
                  "link": link, "asin": asin, "host": host, "coupon": coupon}


# ---------------------------------------------------------------- handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID == 0:
        await update.message.reply_text(f"رقمك (ID): {user.id}\nحطه في OWNER_ID وشغل البوت تاني.")
        return
    if is_allowed(user.id):
        await update.message.reply_text("أهلاً! ابعتلي لينك منتج من أمازون وأنا أجهزلك البوست 🛒\n📸 أو ابعت لقطة شاشة للمنتج ومعاها اللينك، وأنا أحط عليها مستطيلات السعر والكوبونات وشريط القناة.")
        return
    await update.message.reply_text("طلبك اتبعت للمسؤول، استنى الموافقة ✋")
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ قبول", callback_data=f"ap:{user.id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"dn:{user.id}"),
        ]]
    )
    name = user.full_name or "مستخدم"
    uname = f"@{user.username}" if user.username else "بدون يوزر"
    try:
        await context.bot.send_message(
            OWNER_ID, f"طلب استخدام جديد:\n{name} ({uname})\nID: {user.id}", reply_markup=kb
        )
    except Exception as e:
        log.warning("notify owner failed: %s", e)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not ALLOWED:
        await update.message.reply_text("مفيش مستخدمين مضافين.")
        return
    await update.message.reply_text("\n".join(f"{n} — {i}" for i, n in ALLOWED.items()))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("اكتب: /remove ID")
        return
    if ALLOWED.pop(context.args[0], None) is not None:
        save_users(ALLOWED)
        await update.message.reply_text("تم الحذف ✅")
    else:
        await update.message.reply_text("الرقم ده مش في القائمة.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("مش مسموحلك تستخدم البوت. ابعت /start عشان تطلب الإذن.")
        return
    editing = context.bot_data.setdefault("editing", {})
    if user.id in editing:
        pid = editing.pop(user.id)
        txt = (update.message.text or "").strip()
        item = load_draft(pid)
        if not item:
            await update.message.reply_text("البوست ده انتهى.")
            return
        if txt in ("إلغاء", "الغاء"):
            await update.message.reply_text("اتلغى التعديل. البوست القديم لسه موجود.")
            return
        post = html.escape(txt)
        store_draft(pid, item["uid"], item["image"], post, item.get("src"))
        await send_post(context.bot, update.effective_chat.id, item["image"], post, make_kb(pid))
        return
    m = re.search(r"https?://\S+", update.message.text or "")
    pend = context.bot_data.setdefault("pending_shot", {})
    if m and user.id in pend:
        raw = pend.pop(user.id)
        await _finish_shot(update, context, raw, m.group(0).rstrip(").,"))
        return
    if not m:
        await update.message.reply_text("ابعتلي لينك المنتج من أمازون 🔗")
        return
    wait = await update.message.reply_text("⏳ بقرا بيانات المنتج...")
    hint = re.sub(r"\s+", " ", (update.message.text or "").replace(m.group(0), " ")).strip()[:90]   # عنوان لصفحات العروض
    try:
        ok, res = await asyncio.to_thread(process_link, m.group(0).rstrip(").,"), hint or None)
    except Exception as e:
        log.exception("process_link crashed")
        ok, res = False, f"حصل خطأ غير متوقع: {type(e).__name__}"
    try:
        await wait.delete()
    except Exception:
        pass
    if not ok:
        await update.message.reply_text("❌ " + res)
        return
    pid = new_pid()
    store_draft(pid, user.id, res["image"], res["post"])
    await send_post(context.bot, update.effective_chat.id, res["image"], res["post"], make_kb(pid))
    dbg = res.get("debug_html")
    if dbg and user.id == OWNER_ID and not context.bot_data.get("promo_dbg_sent"):
        context.bot_data["promo_dbg_sent"] = True      # مرة واحدة: صفحة العرض عشان نظبط قراءة العنوان
        try:
            await context.bot.send_document(OWNER_ID, io.BytesIO(dbg.encode("utf-8", "ignore")),
                                            filename="promo_page.html", caption="نسخة من صفحة العرض (للتحليل)")
        except Exception as e:
            log.warning("debug page send failed: %s", type(e).__name__)


def _pct_of(price, old):
    if price and old and old > price:
        return int(round((old - price) / old * 100))
    return None


def _make_shot_draft(raw, url):
    """Blocking. لقطة الشاشة + لينك المنتج -> (صورة، بوست). لو صفحة المنتج مقرتش بيطلع بوست بسيط باللينك والفوتر."""
    import shot_card
    ok, res = process_link(url)
    if ok:
        price, old = res["price"], res["old"]
        img, info = shot_card.make_shot(raw, price=price, pct=_pct_of(price, old))
        # الكوبون من اللقطة نفسها (صفحة أمازون من غير تسجيل دخول غالبًا مبتورّيش الكوبون)
        coupon = info.get("coupon") or res.get("coupon")
        post = build_post(res["title"], price, old, res["rating"], res["link"], compact=bool(_pct_of(price, old)),
                          coupon=coupon)
        return True, {"image": img, "post": post, "info": info}
    asin = find_asin(url)
    if not asin:
        return False, res
    host = urlparse(url).netloc.lower()
    host = host if ("amazon." in host and not host.startswith("link.")) else "www.amazon.eg"
    img, info = shot_card.make_shot(raw)
    post = "\n".join([intro(), "", "🛒 اشتري من هنا:", affiliate(host, asin), ""] + FOOTER_LINES)
    return True, {"image": img, "post": post, "info": info}


async def _finish_shot(update, context, raw, url):
    user = update.effective_user
    wait = await update.effective_message.reply_text("⏳ بجهز اللقطة...")
    try:
        ok, res = await asyncio.to_thread(_make_shot_draft, raw, url)
    except Exception as e:
        log.exception("shot failed")
        ok, res = False, f"حصل خطأ غير متوقع: {type(e).__name__}"
    try:
        await wait.delete()
    except Exception:
        pass
    if not ok:
        await update.effective_message.reply_text("❌ " + str(res))
        return
    pid = new_pid()
    store_draft(pid, user.id, res["image"], res["post"])
    await send_post(context.bot, update.effective_chat.id, res["image"], res["post"], make_kb(pid))
    info = res["info"]
    if not info.get("price") and not info.get("coupons"):
        await update.effective_message.reply_text(
            "ℹ️ مقدرتش أحدد مكان السعر والكوبونات في اللقطة فنزلت من غير مستطيلات "
            "(لو مش مثبّت tesseract على Termux اكتب: pkg install tesseract)."
        )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لقطة شاشة لصفحة المنتج: لو مكتوب معاها لينك بيجهّز المسودة، وإلا بيستنى اللينك."""
    user = update.effective_user
    msg = update.effective_message
    if not is_allowed(user.id):
        await msg.reply_text("مش مسموحلك تستخدم البوت. ابعت /start عشان تطلب الإذن.")
        return
    try:
        if msg.photo:
            f = await msg.photo[-1].get_file()
        else:
            f = await msg.document.get_file()
        raw = bytes(await f.download_as_bytearray())
    except Exception as e:
        await msg.reply_text(f"معرفتش أنزّل الصورة: {type(e).__name__}")
        return
    m = re.search(r"https?://\S+", msg.caption or "")
    if m:
        await _finish_shot(update, context, raw, m.group(0).rstrip(").,"))
        return
    context.bot_data.setdefault("pending_shot", {})[user.id] = raw
    await msg.reply_text("📸 استلمت اللقطة. ابعتلي دلوقتي لينك المنتج من أمازون.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, _, val = (q.data or "").partition(":")
    uid = q.from_user.id

    if action in ("ap", "dn"):
        if uid != OWNER_ID:
            return
        if action == "ap":
            ALLOWED[val] = q.message.text.split("\n")[1] if q.message and q.message.text else val
            save_users(ALLOWED)
            await q.edit_message_text(f"تم قبول المستخدم {val} ✅")
            try:
                await context.bot.send_message(int(val), "اتقبلت ✅ ابعتلي لينك منتج أمازون.")
            except Exception:
                pass
        else:
            await q.edit_message_text(f"تم رفض المستخدم {val} ❌")
            try:
                await context.bot.send_message(int(val), "للأسف الطلب اترفض.")
            except Exception:
                pass
        return

    async def say(text, **kw):
        try:
            await context.bot.send_message(uid, text, **kw)
        except Exception as e:
            log.warning("say failed: %s", type(e).__name__)

    item = load_draft(val)
    if not item:
        await say("البوست ده انتهى، ابعت اللينك تاني.")
        return
    if uid != item["uid"] and uid != OWNER_ID:
        return

    if action == "cx":
        await clear_src(context.bot, item)
        drop_draft(val)
        await say("اتلغى ❌")
        return

    if action == "ed":
        context.bot_data.setdefault("editing", {})[uid] = val
        await say(plain(item["post"]), disable_web_page_preview=True)
        await say("✏️ انسخ النص اللي فوق، عدّل عليه، وابعته هنا.\nعشان تلغي التعديل ابعت: إلغاء")
        return

    if action == "pub":
        try:
            await send_post(context.bot, CHANNEL, item["image"], item["post"])
        except Exception as e:
            log.exception("publish failed")
            await say(f"معرفتش أنشر: {type(e).__name__}\nتأكد إن البوت أدمن في القناة.")
            return
        await clear_src(context.bot, item)
        drop_draft(val)
        await say("اتنشر في القناة ✅")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning("handler error: %s", type(context.error).__name__)


def main():
    if not TOKEN:
        print("POST_BOT_TOKEN مش موجود. اعمل export الأول.")
        return
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .connect_timeout(20)
        .read_timeout(40)
        .write_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
