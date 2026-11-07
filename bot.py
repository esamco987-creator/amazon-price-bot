import os
import re
import json
import time
import asyncio
import logging
import threading
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import post_bot as pb  # نفس دوال قراءة أمازون الشغالة في بوت البوستات

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("httpx", "httpcore", "telegram", "apscheduler", "urllib3"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("price_bot")

TOKEN = os.environ.get("BOT_TOKEN", "")
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "3600"))  # كل ساعة
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


def fetch_product(url, debug):
    """يرجع dict فيه asin/host/title/price أو None."""
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
    _, page = pb.get_page(f"https://{host}/dp/{asin}", debug)
    if not page:
        return None
    title, price, old, rating, img = pb.read_product(page)
    if not title:
        return None
    return {"asin": asin, "host": host, "title": title, "price": price}


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
    for p in list(items):
        try:
            prod = await asyncio.to_thread(fetch_by_asin, p["host"], p["asin"], [])
        except Exception as e:
            log.warning("check failed: %s", type(e).__name__)
            prod = None
        price = prod["price"] if prod else None
        if price is None:
            if always_report:
                await bot.send_message(int(chat), f"{p['title'][:50]}\nمقدرتش أجيب سعره دلوقتي.")
            await asyncio.sleep(4)
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
        await asyncio.sleep(4)


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
    app = Application.builder().token(TOKEN).build()
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
