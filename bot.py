import os
import re
import time
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
CHECK_EVERY = 3600  # كل ساعة

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
}

PRICE_SELECTORS = [
    "#corePrice_feature_div span.a-offscreen",
    "span.a-price span.a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#price_inside_buybox",
]

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")

tracked = {}


def parse_price(text):
    text = text.translate(AR_DIGITS).replace(",", "")
    digits = re.sub(r"[^\d.]", "", text)
    try:
        return float(digits)
    except ValueError:
        return None


def get_product(url):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            logging.warning("Request failed: %s", e)
            time.sleep(2)
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        title_tag = soup.select_one("#productTitle")
        if not title_tag:
            page_title = soup.title.get_text(strip=True) if soup.title else None
            logging.warning(
                "No product title (status %s, page: %s)", r.status_code, page_title
            )
            time.sleep(3)
            continue

        title = title_tag.get_text(strip=True)
        for sel in PRICE_SELECTORS:
            tag = soup.select_one(sel)
            if tag:
                price = parse_price(tag.get_text())
                if price:
                    return title, price
        logging.warning("No price found for: %s", title[:40])
        return title, None
    return None, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلا! أنا بوت تتبع أسعار أمازون.\n\n"
        "/track الرابط السعر_المطلوب\n"
        "مثال:\n/track https://www.amazon.eg/dp/XXXX 1500\n\n"
        "/list لعرض المنتجات\n"
        "/remove رقم لحذف منتج"
    )


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("اكتب كده:\n/track الرابط السعر_المطلوب")
        return
    url = context.args[0]
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("السعر لازم يكون رقم.")
        return

    await update.message.reply_text("ثواني، بقرا المنتج...")
    title, price = await asyncio.to_thread(get_product, url)
    if title is None or price is None:
        await update.message.reply_text(
            "معرفتش أقرا السعر. ممكن المنتج مش متوفر دلوقتي، أو أمازون منعت الطلب. جرب بعد شوية."
        )
        return

    chat_id = update.effective_chat.id
    tracked.setdefault(chat_id, []).append(
        {"url": url, "title": title, "target": target, "last": price}
    )
    await update.message.reply_text(
        f"تمام، بتابع:\n{title[:80]}\nالسعر دلوقتي: {price}\nهبلغك لما ينزل لـ {target} أو أقل."
    )


async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = tracked.get(update.effective_chat.id, [])
    if not items:
        await update.message.reply_text("مفيش منتجات بتتابعها.")
        return
    lines = [
        f"{i}. {p['title'][:50]}\n   الحالي: {p['last']} | المطلوب: {p['target']}"
        for i, p in enumerate(items, 1)
    ]
    await update.message.reply_text("\n\n".join(lines))


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = tracked.get(update.effective_chat.id, [])
    try:
        n = int(context.args[0]) - 1
        removed = items.pop(n)
        await update.message.reply_text(f"اتحذف: {removed['title'][:50]}")
    except (IndexError, ValueError):
        await update.message.reply_text("اكتب رقم صح، مثال: /remove 1")


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, items in tracked.items():
        for p in items:
            title, price = await asyncio.to_thread(get_product, p["url"])
            if price is None:
                continue
            p["last"] = price
            if price <= p["target"]:
                await context.bot.send_message(
                    chat_id,
                    f"السعر نزل!\n{p['title'][:80]}\nالسعر: {price}\n{p['url']}",
                )
            await asyncio.sleep(3)


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    threading.Thread(target=run_server, daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("list", list_items))
    app.add_handler(CommandHandler("remove", remove))
    app.job_queue.run_repeating(check_prices, interval=CHECK_EVERY, first=60)
    app.run_polling()


if __name__ == "__main__":
    main()
