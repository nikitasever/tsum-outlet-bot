import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from tsum_parser import TsumOutletParser
from tracker import ProductTracker
from config import BOT_TOKEN, PARSE_INTERVAL_MINUTES

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

parser  = TsumOutletParser()
tracker = ProductTracker()

OUTLET_BASE = "outlet.tsum.ru"

_url_store: dict = {}

def _save_url(url: str) -> str:
    key = str(abs(hash(url)) % 10**12)
    _url_store[key] = url
    return key

def _load_url(key: str) -> str:
    return _url_store.get(key, key)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏷 *Бот анализа ЦУМ Аутлет*\n\n"
        "Помогу проверить наличие товаров на outlet.tsum.ru, "
        "скидки, размеры и состояние.\n\n"
        "*Команды:*\n"
        "• Отправь ссылку на товар — получишь полный анализ\n"
        "• /track `<ссылка>` — добавить в отслеживание\n"
        "• /untrack `<ссылка>` — убрать из отслеживания\n"
        "• /list — отслеживаемые товары\n"
        "• /search `<запрос>` — поиск в аутлете\n"
        "• /help — справка\n\n"
        "Просто скопируй ссылку с outlet.tsum.ru и отправь мне 🔗"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Справка*\n\n"
        "*Анализ товара:*\n"
        "Отправь ссылку вида `https://outlet.tsum.ru/product/...`\n"
        "Получишь: бренд, название, цену со скидкой, состояние товара "
        "и все размеры с их наличием.\n\n"
        "*Отслеживание:*\n"
        "/track — бот уведомит при изменении цены, скидки или наличия.\n"
        "/list — список всех отслеживаемых позиций.\n"
        "/untrack — остановить отслеживание.\n\n"
        "*Поиск:*\n"
        "/search `Prada сумка` — поиск по каталогу аутлета.\n\n"
        f"*Интервал проверки:* каждые {PARSE_INTERVAL_MINUTES} мин."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text(
            "❗ Укажи запрос: `/search Gucci кроссовки`", parse_mode="Markdown"
        )
        return
    msg = await update.message.reply_text(f"🔍 Ищу «{query}» в аутлете...")
    results = await parser.search_products(query)
    if not results:
        await msg.edit_text("😕 Ничего не найдено. Попробуй другой запрос.")
        return

    await msg.delete()
    for item in results[:8]:
        price_str = f"{item['price']:,}".replace(",", " ") + " ₽" if item.get("price") else "цена не указана"
        old_price_str = ""
        if item.get("old_price") and item.get("price") and item["old_price"] > item["price"]:
            old_str = f"{item['old_price']:,}".replace(",", " ")
            discount = round((1 - item["price"] / item["old_price"]) * 100)
            old_price_str = f"\n~~{old_str} ₽~~ (−{discount}%)"
        caption = (
            f"🏷 *{item['brand']}*\n"
            f"📦 {item['name']}\n"
            f"💰 *{price_str}*{old_price_str}\n"
            f"🔗 [Открыть]({item['url']})"
        )
        key = _save_url(item["url"])
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Подробнее", callback_data=f"a:{key}"),
            InlineKeyboardButton("🔔 Отслеживать", callback_data=f"t:{key}"),
        ]])
        try:
            if item.get("image_url"):
                await update.message.reply_photo(
                    photo=item["image_url"],
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    caption, parse_mode="Markdown", reply_markup=keyboard
                )
        except Exception as e:
            logger.error(f"Photo send error: {e}")
            await update.message.reply_text(
                caption, parse_mode="Markdown", reply_markup=keyboard
            )


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❗ Укажи ссылку: `/track https://outlet.tsum.ru/product/...`",
            parse_mode="Markdown"
        )
        return
    url = context.args[0]
    if OUTLET_BASE not in url:
        await update.message.reply_text("❗ Это должна быть ссылка с outlet.tsum.ru")
        return
    user_id = update.effective_user.id
    msg = await update.message.reply_text("⏳ Добавляю в отслеживание...")
    product = await parser.get_product(url)
    if not product:
        await msg.edit_text("❌ Не удалось получить данные. Проверь ссылку.")
        return
    added = tracker.add(user_id, url, product)
    if added:
        await msg.edit_text(
            f"✅ *{product['brand']} {product['name']}* добавлен!\n"
            f"Буду уведомлять об изменениях каждые {PARSE_INTERVAL_MINUTES} мин.",
            parse_mode="Markdown"
        )
    else:
        await msg.edit_text("ℹ️ Этот товар уже в списке отслеживания.")


async def untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tracked = tracker.get_user_items(user_id)
    if not tracked:
        await update.message.reply_text("У тебя нет отслеживаемых товаров.")
        return
    if context.args:
        removed = tracker.remove(user_id, context.args[0])
        await update.message.reply_text("✅ Убрано." if removed else "❗ Не найдено в списке.")
        return
    keyboard = []
    for item in tracked:
        label = f"{item['brand']} {item['name']}"[:45]
        key = _save_url(item["url"])
        keyboard.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"u:{key}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="close")])
    await update.message.reply_text(
        "Выбери товар для удаления:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def list_tracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tracked = tracker.get_user_items(user_id)
    if not tracked:
        await update.message.reply_text("📋 Список пуст. Добавь командой /track")
        return
    text = f"📋 *Отслеживаемые товары ({len(tracked)}):*\n\n"
    keyboard = []
    for i, item in enumerate(tracked, 1):
        price_str = f"{item['price']:,}".replace(",", " ") + " ₽" if item.get("price") else "—"
        disc = f"  −{item['discount']}%" if item.get("discount") else ""
        avail = "🟢" if item.get("available") else "🔴"
        text += f"{i}. {avail} *{item['brand']}* {item['name']}\n   💰 {price_str}{disc}\n\n"
        key = _save_url(item["url"])
        keyboard.append([InlineKeyboardButton(
            f"📊 {item['brand']} {item['name'][:25]}",
            callback_data=f"a:{key}"
        )])
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if OUTLET_BASE in text:
        await _do_analyze(update, text)
    else:
        await update.message.reply_text(
            "🤔 Не понял запрос. Отправь ссылку с outlet.tsum.ru или воспользуйся /search"
        )


async def _do_analyze(update: Update, url: str):
    msg = await update.message.reply_text("⏳ Получаю данные...")
    product = await parser.get_product(url)
    if not product:
        await msg.edit_text(
            "❌ Не удалось получить данные.\n"
            "Убедись, что ссылка ведёт на конкретный товар outlet.tsum.ru"
        )
        return
    text     = format_product(product)
    keyboard = build_keyboard(url, update.effective_user.id)
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)


def format_product(p: dict) -> str:
    lines = []
    lines.append(f"🏷 *{p.get('brand', '—')}*")
    lines.append(f"📦 {p.get('name', '—')}")
    if p.get("condition"):
        lines.append(f"📋 Состояние: _{p['condition']}_")
    lines.append("")
    if p.get("price"):
        price_str = f"{p['price']:,}".replace(",", " ")
        old_price = p.get("old_price")
        discount  = p.get("discount")
        if old_price and old_price > p["price"]:
            old_str  = f"{old_price:,}".replace(",", " ")
            disc_tag = f" (−{discount}%)" if discount else ""
            lines.append(f"💰 *{price_str} ₽* ~~{old_str} ₽~~{disc_tag}")
        else:
            disc_tag = f" (−{discount}% от оригинала)" if discount else ""
            lines.append(f"💰 *{price_str} ₽*{disc_tag}")
    if p.get("article"):
        lines.append(f"🔖 Артикул: `{p['article']}`")
    lines.append("")
    sizes = p.get("sizes", [])
    if sizes:
        lines.append("📐 *Наличие по размерам:*")
        avail_s   = [s for s in sizes if s.get("available")]
        unavail_s = [s for s in sizes if not s.get("available")]
        if avail_s:
            parts = []
            for s in avail_s:
                tag = f"`{s['size']}`"
                if s.get("qty") is not None:
                    tag += f"({s['qty']} шт.)"
                parts.append(tag)
            lines.append(f"🟢 В наличии: {'  '.join(parts)}")
        if unavail_s:
            unavail_list = "  ".join(f"`{s['size']}`" for s in unavail_s)
            lines.append(f"🔴 Нет: {unavail_list}")
        lines.append(f"\n📊 Доступно: {len(avail_s)}/{len(sizes)} размеров")
    else:
        status = "🟢 В наличии" if p.get("available") else "🔴 Нет в наличии"
        lines.append(status)
    if p.get("colors"):
        lines.append(f"\n🎨 Цвет: {', '.join(p['colors'])}")
    if p.get("url"):
        lines.append(f"\n🔗 [Открыть в аутлете]({p['url']})")
    return "\n".join(lines)


def build_keyboard(url: str, user_id: int) -> InlineKeyboardMarkup:
    is_tracked = tracker.is_tracked(user_id, url)
    key = _save_url(url)
    track_btn = (
        InlineKeyboardButton("🔕 Убрать из отслеживания", callback_data=f"u:{key}")
        if is_tracked else
        InlineKeyboardButton("🔔 Отслеживать", callback_data=f"t:{key}")
    )
    return InlineKeyboardMarkup([
        [track_btn],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"a:{key}")],
    ])


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data    = query.data
    user_id = query.from_user.id

    if data == "close":
        await query.message.delete()

    elif data.startswith("a:"):
        url = _load_url(data[2:])
        await query.edit_message_text("⏳ Обновляю...")
        product = await parser.get_product(url)
        if product:
            await query.edit_message_text(
                format_product(product), parse_mode="Markdown",
                reply_markup=build_keyboard(url, user_id)
            )
        else:
            await query.edit_message_text("❌ Не удалось получить данные.")

    elif data.startswith("t:"):
        url = _load_url(data[2:])
        product = await parser.get_product(url)
        if product:
            added = tracker.add(user_id, url, product)
            status = "✅ Добавлено!" if added else "ℹ️ Уже отслеживается."
            await query.edit_message_text(
                f"{status}\n\n{format_product(product)}",
                parse_mode="Markdown", reply_markup=build_keyboard(url, user_id)
            )

    elif data.startswith("u:"):
        url = _load_url(data[2:])
        tracker.remove(user_id, url)
        product = await parser.get_product(url)
        if product:
            await query.edit_message_text(
                f"🔕 Убрано из отслеживания.\n\n{format_product(product)}",
                parse_mode="Markdown", reply_markup=build_keyboard(url, user_id)
            )
        else:
            await query.edit_message_text("✅ Товар убран из отслеживания.")


async def check_updates(app: Application):
    logger.info("Проверяю обновления...")
    for user_id, items in tracker.get_all_items().items():
        for item in items:
            try:
                new_data = await parser.get_product(item["url"])
                if not new_data:
                    continue
                changes = tracker.update_and_diff(int(user_id), item["url"], new_data)
                if changes:
                    text = (
                        f"🔔 *Изменение: {new_data.get('brand')} {new_data.get('name')}*\n\n"
                        + "\n".join(f"• {c}" for c in changes)
                        + f"\n\n🔗 [Открыть]({new_data.get('url')})"
                    )
                    await app.bot.send_message(
                        int(user_id), text, parse_mode="Markdown",
                        reply_markup=build_keyboard(item["url"], int(user_id))
                    )
            except Exception as e:
                logger.error(f"Ошибка проверки {item['url']}: {e}")
        await asyncio.sleep(0.5)


async def run_scheduler(app: Application):
    while True:
        await asyncio.sleep(PARSE_INTERVAL_MINUTES * 60)
        await check_updates(app)


async def post_init(app):
    await app.bot.set_my_commands([
        ("start",   "🏠 Главное меню"),
        ("search",  "🔍 Поиск товара"),
        ("list",    "📋 Мои отслеживания"),
        ("track",   "🔔 Добавить отслеживание"),
        ("untrack", "🔕 Убрать отслеживание"),
        ("help",    "📖 Справка"),
    ])


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("search",  search))
    app.add_handler(CommandHandler("track",   track))
    app.add_handler(CommandHandler("untrack", untrack))
    app.add_handler(CommandHandler("list",    list_tracked))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    loop = asyncio.get_event_loop()
    loop.create_task(run_scheduler(app))

    logger.info("🤖 ЦУМ Аутлет бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
