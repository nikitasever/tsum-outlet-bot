import asyncio
import threading
import uvicorn
from api import app
from bot import main as bot_main

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=int(__import__("os").environ.get("PORT", 8000)))

if __name__ == "__main__":
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    bot_main()
