import asyncio
import threading
import uvicorn
import os
from api import app


def run_api():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


async def run_bot():
    from bot import (
        Application, CommandHandler, MessageHandler,
        CallbackQueryHandler, filters,
        start, help_command, search, track, untrack,
        list_tracked, handle_message, button_handler,
        post_init, run_scheduler, logger, BOT_TOKEN
    )
    from config import BOT_TOKEN
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start",   start))
    application.add_handler(CommandHandler("help",    help_command))
    application.add_handler(CommandHandler("search",  search))
    application.add_handler(CommandHandler("track",   track))
    application.add_handler(CommandHandler("untrack", untrack))
    application.add_handler(CommandHandler("list",    list_tracked))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    asyncio.create_task(run_scheduler(application))
    logger.info("🤖 ЦУМ Аутлет бот запущен")
    await application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    asyncio.run(run_bot())
