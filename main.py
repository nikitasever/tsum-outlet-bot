import asyncio
import os
import uvicorn
from api import app as fastapi_app


async def main():
    from bot import (
        Application, CommandHandler, MessageHandler,
        CallbackQueryHandler, filters,
        start, help_command, search, track, untrack,
        list_tracked, handle_message, button_handler,
        post_init, run_scheduler, logger,
        scan_command, scan_history_command,
    )
    from config import BOT_TOKEN

    port = int(os.environ.get("PORT", 8080))
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=port, loop="none")
    server = uvicorn.Server(config)

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start",        start))
    application.add_handler(CommandHandler("help",         help_command))
    application.add_handler(CommandHandler("search",       search))
    application.add_handler(CommandHandler("track",        track))
    application.add_handler(CommandHandler("untrack",      untrack))
    application.add_handler(CommandHandler("list",         list_tracked))
    application.add_handler(CommandHandler("scan",         scan_command))
    application.add_handler(CommandHandler("scan_history", scan_history_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    async with application:
        await application.initialize()
        await application.start()
        asyncio.create_task(run_scheduler(application))
        logger.info("🤖 ЦУМ Аутлет бот запущен")
        await asyncio.gather(
            server.serve(),
            application.updater.start_polling(drop_pending_updates=True),
        )
        await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
