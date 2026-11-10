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
FOOTER_LINES = ["📢 للمزيد من العروض: t.me/PromoCodeOnlain", "🔎 دوّر على أي منتج: t.me/Mix_Compare_bot"]
BASE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE, "allowed_users.json")
LOGO_FILE = os.path.join(BASE, "logo.png")

CANVAS = 1080
PRODUCT_BOX = 0.80
LOGO_RATIO = 0.16

OPENERS = [
    "🔥 عرض يستاهل تلحقه!",
    "⚡ فرصة حلوة متتفوتش!",
    "💥 لقينالك عرض جامد!",
    "🛍️ عرض النهاردة مختلف!",
    "😍 ده من العروض اللي بتخلص بسرعة!",
]
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

    price_txt = first_text(
        soup,
        [
            "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
            "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
            "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
            ".priceToPay .a-offscreen",
            "span.a-price:not(.a-text-price) .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#price_inside_buybox",
        ],
    )
    price = parse_price(price_txt)

    old_txt = first_text(
        soup,
        [
            "span.a-price.a-text-price .a-offscreen",
            "span[data-a-strike='true'] .a-offscreen",
            "#listPrice",
            ".basisPrice .a-offscreen",
        ],
    )
    old = parse_price(old_txt)
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


# True = بوست مختصر تحت الكارت. False = البوست الكامل (العنوان والسعر والخصم) زي الأول.
COMPACT_POST = False


def product_card(title, price, old, raw):
    """كارت العرض لو فيه خصم، وإلا None."""
    if not raw or not (price and old and old > price):
        return None
    try:
        pct = int(round((old - price) / old * 100))
        if pct < 1:
            return None
        import deal_card
        return deal_card.make_card({"title": title, "price": price, "old": old, "pct": pct}, raw)
    except Exception as e:
        log.warning("card failed: %s", type(e).__name__)
        return None


def product_image(title, price, old, raw):
    """كارت العرض (لو فيه خصم) وإلا الصورة العادية على خلفية بيضاء."""
    if not raw:
        return None
    return product_card(title, price, old, raw) or prepare_image(raw)


def short_title(t, limit=95):
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0] + "…"


def build_post(title, price, old, rating, link, compact=False):
    if compact and price and old and old > price:
        # الكارت فيه العنوان والسعر، فالكلام هنا مختصر
        pct = round((old - price) / old * 100)
        return "\n".join([
            f"🔥 خصم {pct}% — {fmt(price)} بدل {fmt(old)}",
            "", "🛒 اشتري من هنا:", link, "",
        ] + FOOTER_LINES)
    lines = [random.choice(OPENERS), "", f"<b>{html.escape(short_title(title))}</b>", ""]
    if price:
        lines.append(f"💰 السعر: <b>{fmt(price)}</b>")
        if old:
            pct = round((old - price) / old * 100)
            lines.append(f"🏷️ كان: <s>{fmt(old)}</s> (خصم {pct}%)")
    lines += ["", "🛒 اشتري من هنا:", link, ""]
    lines += FOOTER_LINES
    return "\n".join(lines)


async def send_post(bot, chat_id, image, post, markup=None):
    if image and len(post) <= 1000:
        await bot.send_photo(chat_id, photo=image, caption=post, parse_mode=ParseMode.HTML, reply_markup=markup)
        return
    if image:
        await bot.send_photo(chat_id, photo=image)
    await bot.send_message(
        chat_id,
        post,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


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


def process_link(url):
    """Blocking. Returns (ok, data_or_message)."""
    debug = []
    final, body, asin = url, "", None
    for headers in HEADER_STRATEGIES:
        try:
            final, body = resolve(url, headers)
        except Exception as e:
            debug.append(f"فتح الرابط: {type(e).__name__}")
            continue
        asin = find_asin(final) or find_asin(url) or find_asin(body)
        if asin:
            break
    if not asin:
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
    card = product_card(title, price, old, raw)
    image = card or (prepare_image(raw) if raw else None)
    post = build_post(title, price, old, rating, link, compact=COMPACT_POST and bool(card))
    return True, {"image": image, "post": post, "price": price}


# ---------------------------------------------------------------- handlers
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if OWNER_ID == 0:
        await update.message.reply_text(f"رقمك (ID): {user.id}\nحطه في OWNER_ID وشغل البوت تاني.")
        return
    if is_allowed(user.id):
        await update.message.reply_text("أهلاً! ابعتلي لينك منتج من أمازون وأنا أجهزلك البوست 🛒")
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
    if not m:
        await update.message.reply_text("ابعتلي لينك المنتج من أمازون 🔗")
        return
    wait = await update.message.reply_text("⏳ بقرا بيانات المنتج...")
    try:
        ok, res = await asyncio.to_thread(process_link, m.group(0).rstrip(").,"))
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
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
