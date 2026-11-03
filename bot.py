import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")

logging.basicConfig(level=logging.INFO)

products = {}

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة منتج", callback_data="add")],
        [InlineKeyboardButton("📦 منتجاتي", callback_data="list")],
        [InlineKeyboardButton("ℹ️ المساعدة", callback_data="help")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 أهلاً بك في بوت متابعة أسعار أمازون مصر\n\n"
        "تابع منتجاتك واعرف لما السعر يوصل لهدفك.",
        reply_markup=menu()
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "add":
        context.user_data["step"] = "link"
        await q.message.reply_text("📎 ابعت رابط المنتج من Amazon.eg")

    elif q.data == "list":
        items = products.get(q.from_user.id, [])
        if not items:
            await q.message.reply_text("لسه مفيش منتجات في قائمتك.")
        else:
            text = "📦 منتجاتك:\n\n"
            for i, p in enumerate(items, 1):
                text += f"{i}- {p['link']}\nالسعر المستهدف: {p['target']} جنيه\n\n"
            await q.message.reply_text(text)

    elif q.data == "help":
        await q.message.reply_text(
            "أرسل رابط منتج Amazon.eg ثم السعر المستهدف بالجنيه."
        )

async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id
    value = update.message.text.strip()
    step = context.user_data.get("step")

    if step == "link":
        if "amazon.eg" not in value and "amzn.to" not in value:
            await update.message.reply_text("من فضلك ابعت رابط أمازون مصر.")
            return

        context.user_data["link"] = value
        context.user_data["step"] = "target"
        await update.message.reply_text("💰 اكتب السعر المستهدف بالجنيه.")

    elif step == "target":
        try:
            target = float(value.replace(",", "."))
            if target <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("اكتب سعر صحيح بالأرقام.")
            return

        products.setdefault(user, []).append({
            "link": context.user_data["link"],
            "target": target
        })

        context.user_data.clear()
        await update.message.reply_text(
            "✅ تمت إضافة المنتج لقائمتك.\n"
            "متابعة الأسعار التلقائية سيتم تفعيلها بعد ربط مصدر أسعار أمازون.",
            reply_markup=menu()
        )

async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, messages))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
