import os
import re
import json
import time
import asyncio
import logging
import threading
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import post_bot as pb  # نفس دوال قراءة أمازون الشغالة في بوت البوستات

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "telegram", "apscheduler", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("price_bot")

TOKEN = os.environ.get("BOT_TOKEN", "")
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "1800"))  # كل نص ساعة
AFFILIATE_TAG = "mixdeals-21"  # اللينك في التنبيه بيبقى بكودك. امسحه لو مش عايز: ""
MAX_ITEMS = 25  # أقصى عدد منتجات لكل شخص

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


tracked = load()  # {chat_id: [ {asin, host, title, target, last, lowest, notified}, ... ]}


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
        time.sleep(1.5 * (i + 1))
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
    return fetch_by_asin(host, asin, debug)


def fetch_by_asin(host, asin, debug):
    _, page = get_html(f"https://{host}/dp/{asin}", debug)
    if not page:
        return None
    soup = BeautifulSoup(page, "html.parser")
    shown = page_asin(soup)
    if shown and shown != asin.upper():
        # الصفحة اتحولت لمنتج/موديل تاني: مانثقش في سعرها
        log.warning("asin mismatch: wanted %s page shows %s", asin, shown)
        debug.append("الصفحة بتعرض منتج مختلف")
        return None
    title, _, _, _, _ = pb.read_product(page)
    if not title:
        return None
    return {"asin": asin, "host": host, "title": title, "price": read_price(soup)}


# ---------------------------------------------------------------- commands
HELP = (
    "أهلاً! أنا بوت تتبع أسعار أمازون 📉\n\n"
    "ابعتلي رابط المنتج وبعده السعر المطلوب (من غير /track):\n"
    "https://www.amazon.eg/dp/XXXX 1500\n"
    "هبلغك لما السعر ينزل لـ 1500 أو أقل.\n\n"
    "ولو بعت الرابط من غير سعر، هبلغك مع أي نزول في السعر.\n\n"
    "/list — المنتجات اللي بتتابعها\n"
    "/check — افحص الأسعار دلوقتي\n"
    "/remove رقم — احذف منتج"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_track(update, " ".join(context.args))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رابط من غير /track."""
    await do_track(update, update.message.text or "")


async def do_track(update: Update, text):
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text(
            "ابعتلي رابط المنتج من أمازون، وممكن تكتب بعده السعر المطلوب.\n"
            "مثال: https://www.amazon.eg/dp/XXXX 1500"
        )
        return
    url = m.group(0).rstrip(").,")
    target = num(URL_RE.sub("", text))

    chat = str(update.effective_chat.id)
    items = tracked.setdefault(chat, [])
    if len(items) >= MAX_ITEMS:
        await update.message.reply_text(f"وصلت للحد الأقصى ({MAX_ITEMS} منتج). احذف منتج الأول بـ /remove.")
        return

    await update.message.reply_text("ثواني، بقرا المنتج...")
    debug = []
    prod = await asyncio.to_thread(fetch_product, url, debug)
    if not prod:
        await update.message.reply_text(
            "معرفتش أقرا المنتج من الرابط ده. اتأكد إنه رابط منتج من أمازون وجرب تاني.\n" + "\n".join(debug)
        )
        return
    if prod["price"] is None:
        await update.message.reply_text(
            f"{prod['title'][:80]}\nالمنتج ده مفيش له سعر دلوقتي (غالبًا مش متوفر). جرب لما يتوفر."
        )
        return
    if any(p["asin"] == prod["asin"] and p["host"] == prod["host"] for p in items):
        await update.message.reply_text("المنتج ده بتتابعه بالفعل.")
        return

    item = {
        "asin": prod["asin"],
        "host": prod["host"],
        "title": prod["title"],
        "target": target,
        "last": prod["price"],
        "lowest": prod["price"],
        "notified": False,
    }
    with lock:
        items.append(item)
        save()
    when = f"لما ينزل لـ {pb.fmt(target)} أو أقل" if target else "مع أي نزول في السعر"
    await update.message.reply_text(
        f"تمام، بتابع:\n{prod['title'][:80]}\nالسعر دلوقتي: {pb.fmt(prod['price'])}\nهبلغك {when} ✅"
    )
    if target and prod["price"] <= target:
        await update.message.reply_text("ملحوظة: السعر دلوقتي أقل من المطلوب أصلًا 👀\n" + link_for(item))


async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = tracked.get(str(update.effective_chat.id), [])
    if not items:
        await update.message.reply_text("مفيش منتجات بتتابعها.")
        return
    lines = []
    for i, p in enumerate(items, 1):
        want = pb.fmt(p["target"]) if p["target"] else "أي نزول"
        lines.append(
            f"{i}. {p['title'][:50]}\n"
            f"   الحالي: {pb.fmt(p['last'])} | الأقل: {pb.fmt(p['lowest'])} | المطلوب: {want}"
        )
    await update.message.reply_text("\n\n".join(lines))


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
    await check_chat(context.bot, chat, always_report=True)


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


async def check_chat(bot, chat, always_report=False):
    items = tracked.get(chat, [])
    sem = asyncio.Semaphore(3)  # 3 منتجات في نفس الوقت على الأكتر

    async def fetch(p):
        async with sem:
            try:
                prod = await asyncio.to_thread(fetch_by_asin, p["host"], p["asin"], [])
            except Exception as e:
                log.warning("check failed: %s", type(e).__name__)
                prod = None
            await asyncio.sleep(1)
            return prod

    snapshot = list(items)
    results = await asyncio.gather(*(fetch(p) for p in snapshot))
    for p, prod in zip(snapshot, results):
        price = prod["price"] if prod else None
        if price is None:
            if always_report:
                await bot.send_message(int(chat), f"{p['title'][:50]}\nمقدرتش أجيب سعره دلوقتي.")
            continue
        old = p["last"]
        notify = decide(p, price)
        p["last"] = price
        with lock:
            save()
        if notify:
            drop = ""
            if price < old:
                drop = f" (نزل {round((old - price) / old * 100)}%)"
            try:
                await bot.send_message(
                    int(chat),
                    f"📉 السعر نزل!\n{p['title'][:80]}\n"
                    f"كان: {pb.fmt(old)}\nدلوقتي: {pb.fmt(price)}{drop}\n\n{link_for(p)}",
                )
            except Exception as e:
                log.warning("notify failed: %s", type(e).__name__)
        elif always_report:
            await bot.send_message(int(chat), f"{p['title'][:50]}\nالسعر: {pb.fmt(price)}")


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    for chat in list(tracked.keys()):
        try:
            await check_chat(context.bot, chat)
        except Exception as e:
            log.warning("chat check failed: %s", type(e).__name__)


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_prices, interval=CHECK_EVERY, first=60)
    print("Price bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
