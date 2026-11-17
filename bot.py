import os
import re
import json
import time
import asyncio
import logging
import threading
from collections import Counter
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

import post_bot as pb  # نفس دوال قراءة أمازون الشغالة في بوت البوستات
import price_history as ph  # تاريخ الأسعار + الإحصائيات + الرسم البياني

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "telegram", "apscheduler", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("price_bot")

TOKEN = os.environ.get("BOT_TOKEN", "")
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "1800"))  # كل نص ساعة
AFFILIATE_TAG = "mixdeals-21"  # اللينك في التنبيه بيبقى بكودك. امسحه لو مش عايز: ""
MAX_ITEMS = 25  # أقصى عدد منتجات لكل شخص
FETCH_PARALLEL = 4  # عدد المنتجات اللي بتتفحص في نفس الوقت

# قناتك: البوت بيطلب من المستخدم الجديد يشترك فيها (لازم البوت يكون أدمن في القناة عشان يقدر يتأكد)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@PromoCodeOnlain")
CHANNEL_URL = "https://t.me/" + REQUIRED_CHANNEL.lstrip("@") if REQUIRED_CHANNEL else ""
try:
    OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
except ValueError:
    OWNER_ID = 0

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "tracked.json")
URL_RE = re.compile(r"https?://\S+")
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")

lock = threading.Lock()


# ---------------------------------------------------------------- storage
def load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save():
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(tracked, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        log.warning("save failed: %s", type(e).__name__)


# {chat_id: [ {asin, host, title, target, last, lowest, notified, old, image, coupon, avail, miss}, ... ]}
tracked = load()


# ---------------------------------------------------------------- amazon
def num(text):
    m = re.search(r"\d[\d,]*\.?\d*", (text or "").translate(AR_DIGITS))
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def link_for(p):
    base = f"https://{p['host']}/dp/{p['asin']}"
    return f"{base}?tag={AFFILIATE_TAG}" if AFFILIATE_TAG else base


SHORT_HOSTS = ("amzn.", "a.co", "link.amazon", "amzn.to", "amzn.eu")
_session = threading.local()
CAPTCHA_MARKS = ("api-services-support@amazon.com", "Type the characters you see", "Enter the characters you see",
                 "/errors/validateCaptcha", "اكتب الأحرف")
# أي سعر جوه أقسام المنتجات المقترحة/الإعلانات ده مش سعر المنتج
BAD_ANCESTOR_IDS = ("sims", "similar", "sp_detail", "sponsored", "anonCarousel", "purchase-sims", "bundle",
                    "percolate", "rhf", "recently", "customer-reviews", "ad-", "a-carousel")
PRICE_ROOTS = (
    "#corePriceDisplay_desktop_feature_div",
    "#corePrice_feature_div",
    "#apex_desktop",
    "#apex_offerDisplay_desktop",
    "#desktop_buybox",
    "#buybox",
    "#qualifiedBuybox",
    "#tabular-buybox",
    "#centerCol",   # احتياطي: العمود الرئيسي للمنتج (الأقسام المقترحة بتتفلتر برضه)
    "#ppd",
)
# الصفحة بتبقى حوالي 600 ألف حرف وأغلبها تقييمات ومنتجات مقترحة. بنقصّها عند أول قسم منهم قبل التحليل (أسرع بكتير)
CUT_MARKERS = (
    'id="aplus"', 'id="customerReviews"', 'id="reviewsMedley"', 'id="similarities_feature_div"',
    'id="sims-consolidated-1_feature_div"', 'id="productDescription_feature_div"',
)
MIN_CUT = 150000
COUPON_HINTS = ("coupon", "Coupon", "قسيمة", "كوبون")


def _sess():
    s = getattr(_session, "s", None)
    if s is None:
        s = requests.Session()
        _session.s = s
    return s


def get_html(url, debug, tries=3):
    """يجيب صفحة المنتج بسرعة: جلسة مفتوحة، مهلة قصيرة، ومحاولات بهيدرز مختلفة. صفحة الكابتشا بتتعامل كفشل."""
    last = ""
    for i in range(tries):
        headers = pb.HEADER_STRATEGIES[i % len(pb.HEADER_STRATEGIES)]
        try:
            r = _sess().get(url, headers=headers, timeout=(6, 15), allow_redirects=True)
            body = r.text or ""
            if r.status_code == 200 and "productTitle" in body and not any(m in body for m in CAPTCHA_MARKS[:3]):
                return r.url, body
            last = f"حالة {r.status_code}" + (" (كابتشا)" if any(m in body for m in CAPTCHA_MARKS) else "")
        except Exception as e:
            last = type(e).__name__
        debug.append(f"محاولة {i + 1}: {last}")
        if i < tries - 1:
            time.sleep(0.8 * (i + 1))
    return None, ""


def _in_bad_section(el, root):
    for a in el.parents:
        if a is root:
            break
        ident = " ".join(filter(None, [a.get("id", ""), " ".join(a.get("class", []) or [])])).lower()
        if ident and any(b.lower() in ident for b in BAD_ANCESTOR_IDS):
            return True
    return False


def read_price(soup):
    """سعر المنتج الأساسي بس (من صندوق الشراء). لو مش لاقيه يرجع None بدل ما يخمّن سعر منتج تاني."""
    for sel in PRICE_ROOTS:
        root = soup.select_one(sel)
        if not root:
            continue
        for el in root.select(".a-price:not(.a-text-price)"):
            if el.get("data-a-strike") == "true" or _in_bad_section(el, root):
                continue
            off = el.select_one(".a-offscreen")
            v = num(off.get_text(" ", strip=True)) if off else None
            if v:
                return v
    for sel in ("#priceblock_dealprice", "#priceblock_ourprice", "#price_inside_buybox"):
        el = soup.select_one(sel)
        if el:
            v = num(el.get_text(" ", strip=True))
            if v:
                return v
    return None


def page_asin(soup):
    el = soup.select_one("input#ASIN") or soup.select_one("input[name='ASIN']")
    if el and el.get("value"):
        return el["value"].strip().upper()
    link = soup.find("link", rel="canonical")
    return (pb.find_asin(link.get("href", "")) if link else None)


def slim(page):
    """يقص الصفحة عند أول قسم تقييمات/مقترحات (لو القص بعد أول 150 ألف حرف، عشان مانقصش حاجة مهمة)."""
    cut = len(page)
    for m in CUT_MARKERS:
        i = page.find(m)
        if MIN_CUT < i < cut:
            cut = i
    return page[:cut]


def extract_coupon(page, asin):
    if not any(h in page for h in COUPON_HINTS):
        return None
    try:
        import deals_scanner as scanner  # نفس كاشف الكوبونات بتاع بوت العروض
        return scanner.extract_coupon(page, asin)
    except Exception as e:
        log.warning("coupon read failed: %s", type(e).__name__)
        return None


def parse_product(page, asin):
    """يرجع (حالة, بيانات). الحالة: ok / mismatch / notitle."""
    soup = BeautifulSoup(page, "html.parser")
    shown = page_asin(soup)
    if shown and shown != asin.upper():
        return "mismatch", None  # الصفحة اتحولت لمنتج/موديل تاني: مانثقش في سعرها
    title, p2, old, rating, img = pb.read_product(page)
    if not title:
        return "notitle", None
    price = read_price(soup)
    if old and (not price or old <= price or (p2 and abs(p2 - price) / price > 0.01)):
        old = None  # السعر القديم لازم يكون بتاع نفس السعر الحالي
    return "ok", {"title": title, "price": price, "old": old, "rating": rating, "image": img}


def fetch_by_asin(host, asin, debug):
    """يرجع dict فيه asin/host/title/price/old/image/coupon. أو None لو الصفحة ماجتش."""
    _, page = get_html(f"https://{host}/dp/{asin}", debug)
    if not page:
        return None
    small = slim(page)
    st, prod = parse_product(small, asin)
    if small is not page and (st == "notitle" or (st == "ok" and prod["price"] is None)):
        st, prod = parse_product(page, asin)  # القص مكانش مناسب للصفحة دي: نحلل الصفحة كلها
        small = page
    if st == "mismatch":
        log.warning("asin mismatch for %s", asin)
        debug.append("الصفحة بتعرض منتج مختلف")
        return None
    if st != "ok":
        return None
    prod.update({"asin": asin, "host": host, "coupon": extract_coupon(small, asin) if prod["price"] else None})
    try:  # أكواد وكوبونات الصفحة بتتسجل لبوت الأكواد (من غير أي طلب زيادة)
        import coupon_store
        coupon_store.harvest(small, asin)
        if prod["coupon"]:
            coupon_store.record_clip(asin, prod["title"], prod["coupon"], prod["price"])
    except Exception:
        pass
    return prod


_prod_cache = {}  # {(host, asin): (وقت, منتج)} — منتج اتقرا من دقيقتين ما نقراهوش تاني لما حد تاني يضيفه


def get_product(host, asin, debug):
    key = (host, asin)
    hit = _prod_cache.get(key)
    if hit and time.time() - hit[0] < 120 and hit[1]:
        return hit[1]
    prod = fetch_by_asin(host, asin, debug)
    if prod:
        _prod_cache[key] = (time.time(), prod)
        if len(_prod_cache) > 200:
            for k in sorted(_prod_cache, key=lambda k: _prod_cache[k][0])[:100]:
                _prod_cache.pop(k, None)
    return prod


def fetch_product(url, debug):
    """يرجع dict فيه asin/host/title/price أو None."""
    host = urlparse(url).netloc.lower()
    asin = pb.find_asin(url)
    final = url
    if not asin or any(h in host for h in SHORT_HOSTS):
        # لينك مختصر: لازم نفكّه الأول. أما اللينك المباشر فمفيش داعي نضيع طلب
        try:
            final, body = pb.resolve(url, pb.HEADER_STRATEGIES[0])
        except Exception as e:
            debug.append(f"فتح الرابط: {type(e).__name__}")
            return None
        host = urlparse(final).netloc.lower()
        asin = pb.find_asin(final) or pb.find_asin(url) or pb.find_asin(body)
    if not asin:
        return None
    if "amazon." not in host or host.startswith("link."):
        host = "www.amazon.eg"
    return get_product(host, asin, debug)


# ---------------------------------------------------------------- telegram helpers
_img_cache = {}  # {رابط الصورة: بايتات}


def get_image(url):
    if not url:
        return None
    if url in _img_cache:
        return _img_cache[url]
    try:
        raw = pb.download_image(url)
    except Exception:
        raw = None
    if raw:
        if len(_img_cache) > 60:
            _img_cache.pop(next(iter(_img_cache)), None)
        _img_cache[url] = raw
    return raw


async def safe_answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except Exception:
        pass  # الضغطة قديمة أو النت واقع


async def send_card(bot, chat, p, text, kb):
    """بيبعت صورة المنتج + النص + الأزرار (ولو مفيش صورة، نص بس)."""
    raw = await asyncio.to_thread(get_image, p.get("image"))
    if raw:
        try:
            return await bot.send_photo(int(chat), raw, caption=text[:1000], reply_markup=kb)
        except Exception as e:
            log.warning("send photo failed: %s", type(e).__name__)
    return await bot.send_message(int(chat), text[:4000], reply_markup=kb)


def find_item(chat, asin):
    for p in tracked.get(str(chat), []):
        if p["asin"] == asin:
            return p
    return None


# ---------------------------------------------------------------- الاشتراك في القناة
_member_cache = {}
_warned = {"member": False}


async def is_member(bot, uid, force=False):
    """هل المستخدم مشترك في القناة؟ لو البوت مش أدمن فيها (مايقدرش يتأكد) بنسمح بدل ما نوقّف البوت."""
    if not REQUIRED_CHANNEL or uid == OWNER_ID:
        return True
    hit = _member_cache.get(uid)
    if hit and hit[1] and not force and time.time() - hit[0] < 600:
        return True
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
        ok = m.status in ("member", "administrator", "creator") or (m.status == "restricted" and getattr(m, "is_member", True))
        _member_cache[uid] = (time.time(), ok)
        return ok
    except Exception as e:
        if not _warned["member"]:
            _warned["member"] = True
            log.warning("membership check failed (%s). خلي البوت أدمن في القناة عشان شرط الاشتراك يشتغل", type(e).__name__)
        return True


def gate_kb():
    rows = []
    if CHANNEL_URL:
        rows.append([Btn("📢 اشترك في القناة", url=CHANNEL_URL)])
    rows.append([Btn("✅ اشتركت، كمّل", callback_data="sub")])
    return Kb(rows)


# ---------------------------------------------------------------- نصوص وأزرار الكارت
def target_text(p):
    t = p.get("target")
    if t:
        return f"هنبلغك لما السعر ينزل لـ {pb.fmt(t)} أو أقل"
    return "هنبلغك مع أي نزول في السعر"


def card_text(p):
    price = p["last"]
    lines = [f"🛒 {p['title'][:90]}", ""]
    price_line = f"💰 السعر: {pb.fmt(price)}"
    old = p.get("old")
    if old and old > price:
        price_line += f"  (كان {pb.fmt(old)} — خصم {round((old - price) / old * 100)}%)"
    lines.append(price_line)
    if p.get("coupon"):
        lines.append(f"🎟️ كوبون: {p['coupon']}")
    if p.get("avail", True) is False:
        lines.append("📦 المنتج مش متوفر دلوقتي")
    _, vtxt = ph.verdict(p["asin"], price)
    lines += ["", vtxt]
    st = ph.stats(p["asin"])
    if st and st["n"] >= 2:
        lines.append(f"📉 أقل سعر سجلته: {pb.fmt(st['min'])} | الأعلى: {pb.fmt(st['max'])}")
    lines += ["", "🛒 اشتري من هنا:", link_for(p), "", "🔔 " + target_text(p)]
    return "\n".join(lines)


def card_kb(p):
    a = p["asin"]
    return Kb([
        [Btn("📉 نزول 10%", callback_data=f"t:{a}:10"), Btn("📉 نزول 20%", callback_data=f"t:{a}:20"),
         Btn("📉 نزول 30%", callback_data=f"t:{a}:30")],
        [Btn("⭐ لأقل سعر", callback_data=f"t:{a}:low"), Btn("🔔 أي نزول", callback_data=f"t:{a}:any")],
        [Btn("📈 رسم السعر", callback_data=f"c:{a}"), Btn("🛒 اشتري من أمازون", url=link_for(p))],
        [Btn("🛑 وقف المتابعة", callback_data=f"x:{a}")],
    ])


def alert_kb(p):
    a = p["asin"]
    return Kb([
        [Btn("🛒 اشتري الآن", url=link_for(p)), Btn("📈 رسم السعر", callback_data=f"c:{a}")],
        [Btn("🛑 وقف المتابعة", callback_data=f"x:{a}")],
    ])


async def refresh_card(q, p):
    """يحدّث نص الكارت بعد تغيير التنبيه."""
    text, kb = card_text(p), card_kb(p)
    try:
        if q.message and q.message.photo:
            await q.edit_message_caption(caption=text[:1000], reply_markup=kb)
        else:
            await q.edit_message_text(text[:4000], reply_markup=kb)
    except BadRequest:
        pass  # نفس النص
    except Exception as e:
        log.warning("refresh card failed: %s", type(e).__name__)


# ---------------------------------------------------------------- commands
HELP = (
    "أهلاً بيك في بوت تتبع أسعار أمازون مصر 📉\n\n"
    "مش بس بتابعلك السعر، كمان بقولك تشتري دلوقتي ولا تستنى.\n\n"
    "ابعتلي رابط أي منتج من amazon.eg وهبدأ أتابعه، وبعدها اختار من الأزرار تحب أنبهك إمتى:\n"
    "🔔 لما ينزل 10% أو 20% أو 30%\n"
    "⭐ لما يوصل لأقل سعر سجلته\n"
    "📈 وشوف رسم بياني لسعر المنتج\n\n"
    "ولو عندك سعر معين، اكتبه بعد الرابط:\n"
    "https://www.amazon.eg/dp/XXXX 1500\n\n"
    "/list — قايمتي: المنتجات اللي بتتابعها\n"
    "/check — افحص الأسعار دلوقتي\n"
    "/remove رقم — احذف منتج"
)


def user_name(u):
    return (u.full_name if u else "") or ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ph.touch_user(update.effective_chat.id, user_name(u), (u.username or "") if u else "")
    kb = Kb([[Btn("📢 قناة عروض وخصومات", url=CHANNEL_URL)]]) if CHANNEL_URL else None
    await update.message.reply_text(HELP, reply_markup=kb)


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await core_track(context.bot, update.effective_chat.id, update.effective_user, " ".join(context.args), context.user_data)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رابط من غير /track."""
    await core_track(context.bot, update.effective_chat.id, update.effective_user, update.message.text or "", context.user_data)


async def core_track(bot, chat_id, user, text, user_data):
    uid = user.id if user else chat_id
    ph.touch_user(chat_id, user_name(user), (user.username or "") if user else "")
    m = URL_RE.search(text or "")
    if not m:
        await bot.send_message(
            chat_id,
            "ابعتلي رابط المنتج من أمازون، وممكن تكتب بعده السعر المطلوب.\n"
            "مثال: https://www.amazon.eg/dp/XXXX 1500",
        )
        return
    if not await is_member(bot, uid):
        user_data["pending"] = text
        await bot.send_message(
            chat_id,
            "عشان تستخدم البوت لازم تكون مشترك في قناة عروض وخصومات 📢\n"
            "اشترك الأول، وبعدين اضغط «✅ اشتركت، كمّل» وهكمّل متابعة المنتج.",
            reply_markup=gate_kb(),
        )
        return

    url = m.group(0).rstrip(").,")
    target = num(URL_RE.sub("", text))
    chat = str(chat_id)
    items = tracked.setdefault(chat, [])

    wait = await bot.send_message(chat_id, "ثواني، بقرا المنتج...")
    debug = []
    prod = await asyncio.to_thread(fetch_product, url, debug)

    async def clear_wait():
        try:
            await wait.delete()
        except Exception:
            pass

    if not prod:
        await clear_wait()
        await bot.send_message(
            chat_id,
            "معرفتش أقرا المنتج من الرابط ده. اتأكد إنه رابط منتج من أمازون وجرب تاني.\n" + "\n".join(debug),
        )
        return
    if prod["price"] is None:
        await clear_wait()
        await bot.send_message(chat_id, f"{prod['title'][:80]}\nالمنتج ده مفيش له سعر دلوقتي (غالبًا مش متوفر). جرب لما يتوفر.")
        return

    ph.record(prod["asin"], prod["price"])
    ph.log_search(chat_id, prod["asin"], prod["title"])

    item = next((p for p in items if p["asin"] == prod["asin"] and p["host"] == prod["host"]), None)
    if item is None:
        if len(items) >= MAX_ITEMS:
            await clear_wait()
            await bot.send_message(chat_id, f"وصلت للحد الأقصى ({MAX_ITEMS} منتج). احذف منتج الأول من /list.")
            return
        item = {
            "asin": prod["asin"], "host": prod["host"], "title": prod["title"], "target": target,
            "last": prod["price"], "lowest": prod["price"], "notified": False,
        }
        with lock:
            items.append(item)
    elif target:
        item["target"] = target
        item["notified"] = False
    item.update({
        "title": prod["title"], "last": prod["price"], "old": prod.get("old"), "image": prod.get("image"),
        "coupon": prod.get("coupon"), "avail": True, "miss": 0,
    })
    item["lowest"] = min(item.get("lowest", prod["price"]), prod["price"])
    with lock:
        save()

    await clear_wait()
    await send_card(bot, chat_id, item, card_text(item), card_kb(item))
    if item.get("target") and prod["price"] <= item["target"]:
        await bot.send_message(chat_id, "ملحوظة: السعر دلوقتي أقل من المطلوب أصلًا 👀", reply_markup=alert_kb(item))


def list_view(chat):
    items = tracked.get(str(chat), [])
    if not items:
        return "مفيش منتجات بتتابعها لسه. ابعتلي رابط منتج من أمازون وأبدأ أتابعه 📉", None
    lines = ["📋 قايمتك", ""]
    rows = []
    for i, p in enumerate(items, 1):
        want = pb.fmt(p["target"]) if p.get("target") else "أي نزول"
        gone = " (مش متوفر)" if p.get("avail", True) is False else ""
        lines.append(
            f"{i}. {p['title'][:48]}{gone}\n"
            f"   الحالي: {pb.fmt(p['last'])} | الأقل: {pb.fmt(p['lowest'])} | المطلوب: {want}"
        )
        rows.append([Btn(f"📊 تفاصيل {i}", callback_data=f"d:{p['asin']}"), Btn(f"🛑 حذف {i}", callback_data=f"x:{p['asin']}")])
    rows.append([Btn("🔄 افحص الأسعار دلوقتي", callback_data="ck")])
    return "\n\n".join([lines[0] + "\n"] + lines[2:])[:4000], Kb(rows)


async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ph.touch_user(update.effective_chat.id, user_name(update.effective_user), (update.effective_user.username or ""))
    text, kb = list_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb)


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = tracked.get(str(update.effective_chat.id), [])
    try:
        n = int(context.args[0].translate(AR_DIGITS)) - 1
        if n < 0:
            raise IndexError
        with lock:
            removed = items.pop(n)
            save()
        await update.message.reply_text(f"اتحذف: {removed['title'][:50]}")
    except (IndexError, ValueError):
        await update.message.reply_text("اكتب رقم صح من /list، مثال: /remove 1")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = str(update.effective_chat.id)
    if not tracked.get(chat):
        await update.message.reply_text("مفيش منتجات بتتابعها.")
        return
    await update.message.reply_text("بفحص الأسعار...")
    await check_all(context.bot, [chat], always_report=True)


# ---------------------------------------------------------------- الأزرار
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    chat = str(q.message.chat_id) if q.message else str(q.from_user.id)
    uid = q.from_user.id
    bot = context.bot
    parts = data.split(":")
    act = parts[0]

    if act == "sub":
        if not await is_member(bot, uid, force=True):
            await safe_answer(q, "لسه مش مشترك في القناة ❌ اشترك الأول وبعدين اضغط تاني", True)
            return
        await safe_answer(q, "تمام ✅")
        pending = context.user_data.pop("pending", None)
        if pending:
            await core_track(bot, int(chat), q.from_user, pending, context.user_data)
        else:
            await bot.send_message(int(chat), "أهلاً بيك! ابعتلي رابط أي منتج من أمازون وأنا أتابعه 📉")
        return

    if act == "ck":
        if not tracked.get(chat):
            await safe_answer(q, "مفيش منتجات بتتابعها")
            return
        await safe_answer(q, "بفحص الأسعار... ثواني")
        await check_all(bot, [chat], always_report=True)
        return

    p = find_item(chat, parts[1]) if len(parts) > 1 else None
    if p is None:
        await safe_answer(q, "المنتج ده اتحذف من قايمتك", True)
        return

    if act == "t" and len(parts) == 3:
        mode = parts[2]
        if mode in ("10", "20", "30"):
            p["target"] = round(p["last"] * (1 - int(mode) / 100))
            toast = f"تمام ✅ هنبلغك لما ينزل لـ {pb.fmt(p['target'])}"
        elif mode == "low":
            st = ph.stats(p["asin"])
            lo = st["min"] if st else None
            p["target"] = lo if lo and lo < p["last"] else round(p["last"] * 0.97)
            toast = f"تمام ✅ هنبلغك لما ينزل لـ {pb.fmt(p['target'])}"
        else:
            p["target"] = None
            toast = "تمام ✅ هنبلغك مع أي نزول في السعر"
        p["notified"] = False
        with lock:
            save()
        await safe_answer(q, toast)
        await refresh_card(q, p)
        return

    if act == "c":
        raw = await asyncio.to_thread(ph.chart, p["asin"], p["title"], p["last"])
        if not raw:
            await safe_answer(q, "لسه معنديش بيانات كفاية أرسمها. هسجّل السعر كل نص ساعة وهيبان بعد شوية 📊", True)
            return
        await safe_answer(q)
        await bot.send_photo(int(chat), raw, caption=f"📈 {p['title'][:80]}")
        return

    if act == "d":
        await safe_answer(q)
        await send_card(bot, chat, p, card_text(p), card_kb(p))
        return

    if act == "x":
        with lock:
            tracked[chat] = [i for i in tracked.get(chat, []) if i["asin"] != p["asin"]]
            save()
        await safe_answer(q, "اتوقفت متابعة المنتج ده 🛑")
        if q.message and (q.message.text or "").startswith("📋"):
            text, kb = list_view(chat)
            try:
                await q.edit_message_text(text, reply_markup=kb)
            except Exception:
                pass
        else:
            try:
                if q.message and q.message.photo:
                    await q.edit_message_caption(caption=f"🛑 اتوقفت متابعة:\n{p['title'][:80]}")
                else:
                    await q.edit_message_text(f"🛑 اتوقفت متابعة:\n{p['title'][:80]}")
            except Exception:
                pass


# ---------------------------------------------------------------- الإحصائيات (للمالك بس)
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if OWNER_ID == 0:
        await update.message.reply_text(f"رقمك (ID): {uid}\nحطه في OWNER_ID (في ملف env.sh) وشغّل البوتات تاني.")
        return
    if uid != OWNER_ID:
        return
    s = await asyncio.to_thread(ph.summary)
    if not s:
        await update.message.reply_text("معرفتش أقرا الإحصائيات دلوقتي.")
        return
    members = None
    if REQUIRED_CHANNEL:
        try:
            members = await context.bot.get_chat_member_count(REQUIRED_CHANNEL)
        except Exception:
            members = None

    n_items = sum(len(v) for v in tracked.values())
    n_users = sum(1 for v in tracked.values() if v)
    cnt = Counter()
    titles = {}
    for lst in tracked.values():
        for p in lst:
            cnt[p["asin"]] += 1
            titles[p["asin"]] = p["title"]

    lines = ["📊 إحصائيات بوت التتبع", ""]
    if members is not None:
        lines.append(f"📢 مشتركين القناة: {members:,}")
    lines += [
        f"👥 مستخدمين البوت: {s['users']:,}",
        f"   جداد النهارده: {s['users_today']} | آخر 7 أيام: {s['users_week']} | نشطين آخر 7 أيام: {s['active_week']}",
        "",
        f"🔎 منتجات اتبعتت للبحث/المتابعة: النهارده {s['searches_today']} | آخر 7 أيام {s['searches_week']}",
        f"📌 منتجات متتبعة دلوقتي: {n_items} (عند {n_users} مستخدم)",
        f"🔔 تنبيهات اتبعتت: النهارده {s['alerts_today']} | آخر 7 أيام {s['alerts_week']}",
        f"📈 منتجات عندها تاريخ أسعار: {s['products_with_history']:,}",
    ]
    if s["top_searched"]:
        lines += ["", "🔥 أكتر منتجات اتدوّر عليها (آخر 7 أيام):"]
        for i, (asin, title, n, u) in enumerate(s["top_searched"], 1):
            lines.append(f"{i}. {title[:50]} — {n} مرة ({u} شخص)")
    if cnt:
        lines += ["", "⭐ أكتر منتجات متتبعة دلوقتي:"]
        for i, (asin, n) in enumerate(cnt.most_common(5), 1):
            lines.append(f"{i}. {titles[asin][:50]} — {n} شخص")
    await update.message.reply_text("\n".join(lines)[:4000])


# ---------------------------------------------------------------- price checks
def decide(p, price):
    """يرجع (هل نبلّغ؟). ويحدّث حالة المنتج."""
    notify = False
    if p["target"]:
        if price <= p["target"]:
            if not p.get("notified") or price < p["last"]:
                notify = True
            p["notified"] = True
        else:
            p["notified"] = False
    elif price < p["last"]:
        notify = True
    p["lowest"] = min(p.get("lowest", price), price)
    return notify


async def send_alert(bot, chat, p, kind, old, price):
    title = p["title"][:80]
    _, vtxt = ph.verdict(p["asin"], price)
    if kind == "back":
        text = f"📦 رجع متوفر!\n🛒 {title}\nالسعر دلوقتي: {pb.fmt(price)}"
    elif kind == "coupon":
        text = f"🎟️ ظهر كوبون على منتج بتتابعه!\n🛒 {title}\nالكوبون: {p.get('coupon')}\nالسعر دلوقتي: {pb.fmt(price)}"
    else:
        hit = p.get("target") and price <= p["target"]
        head = "🎯 وصل للسعر اللي طلبته!" if hit else "📉 السعر نزل!"
        drop = f" (نزل {round((old - price) / old * 100)}%)" if price < old else ""
        text = f"{head}\n🛒 {title}\nكان: {pb.fmt(old)}\nدلوقتي: {pb.fmt(price)}{drop}"
        if p.get("coupon"):
            text += f"\n🎟️ كوبون: {p['coupon']}"
    if kind != "coupon":
        text += f"\n\n{vtxt}"
    text += f"\n\n🛒 اشتري من هنا:\n{link_for(p)}"
    try:
        await send_card(bot, chat, p, text, alert_kb(p))
        ph.log_event("alert", chat, p["asin"])
    except Exception as e:
        log.warning("notify failed: %s", type(e).__name__)
    await asyncio.sleep(0.05)


async def check_all(bot, chats=None, always_report=False):
    """بيفحص كل المنتجات: المنتج اللي أكتر من شخص بيتابعه بيتقري مرة واحدة، وكلها بالتوازي."""
    chats = list(chats) if chats is not None else list(tracked.keys())
    keys = {}
    for ch in chats:
        for p in tracked.get(ch, []):
            keys[(p["host"], p["asin"])] = None
    sem = asyncio.Semaphore(FETCH_PARALLEL)

    async def fetch(key):
        async with sem:
            try:
                keys[key] = await asyncio.to_thread(fetch_by_asin, key[0], key[1], [])
            except Exception as e:
                log.warning("check failed: %s", type(e).__name__)
            await asyncio.sleep(0.3)

    await asyncio.gather(*(fetch(k) for k in keys))
    for ch in chats:
        try:
            await process_chat(bot, ch, keys, always_report)
        except Exception as e:
            log.warning("chat check failed: %s", type(e).__name__)


async def process_chat(bot, chat, results, always_report):
    for p in list(tracked.get(chat, [])):
        prod = results.get((p["host"], p["asin"]))
        if prod is None:
            if always_report:
                await bot.send_message(int(chat), f"{p['title'][:50]}\nمقدرتش أجيب سعره دلوقتي.")
            continue
        price = prod["price"]
        if price is None:
            p["miss"] = p.get("miss", 0) + 1
            if p["miss"] >= 2 and p.get("avail", True):
                p["avail"] = False  # مرتين ورا بعض من غير سعر: المنتج خلص
                with lock:
                    save()
            if always_report:
                await bot.send_message(int(chat), f"{p['title'][:50]}\nمش متوفر دلوقتي.")
            continue
        was_gone = p.get("avail", True) is False
        p["miss"], p["avail"] = 0, True
        ph.record(p["asin"], price)
        old = p["last"]
        notify = decide(p, price)
        coupon = prod.get("coupon")
        new_coupon = bool(coupon) and coupon != p.get("coupon")
        p["coupon"], p["old"] = coupon, prod.get("old")
        if prod.get("image"):
            p["image"] = prod["image"]
        p["last"] = price
        with lock:
            save()
        if was_gone:
            await send_alert(bot, chat, p, "back", old, price)
        elif notify:
            await send_alert(bot, chat, p, "drop", old, price)
        elif new_coupon:
            await send_alert(bot, chat, p, "coupon", old, price)
        elif always_report:
            await bot.send_message(int(chat), f"{p['title'][:50]}\nالسعر: {pb.fmt(price)}")


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    await check_all(context.bot)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.warning("handler error: %s", type(context.error).__name__)


# ---------------------------------------------------------------- server (للاستضافة بس)
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def run_server():
    port = int(os.environ["PORT"])
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    if not TOKEN:
        print("BOT_TOKEN مش موجود. اعمل export الأول.")
        return
    if os.environ.get("PORT"):
        threading.Thread(target=run_server, daemon=True).start()
    ph.init()
    for lst in tracked.values():  # نبدأ تاريخ الأسعار من آخر سعر معروف لكل منتج متتبَّع
        for p in lst:
            ph.record(p["asin"], p.get("last"))
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)   # مستخدم بطيء ميوقّفش الباقي
        .connect_timeout(20)
        .read_timeout(40)
        .write_timeout(40)
        .build()
    )
    if app.job_queue is None:
        print("job-queue مش متثبتة. نفّذ: pip install 'python-telegram-bot[job-queue]'")
        return
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("list", list_items))
    app.add_handler(CommandHandler("remove", remove))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_prices, interval=CHECK_EVERY, first=60)
    print("Price bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
