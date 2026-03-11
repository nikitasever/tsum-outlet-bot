import asyncio
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from tsum_parser import TsumOutletParser
from tracker import ProductTracker

logger = logging.getLogger(__name__)
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

parser  = TsumOutletParser()
tracker = ProductTracker()


@app.get("/api/search")
async def search(q: str, limit: int = 20):
    results = await parser.search_products(q, limit=limit)
    return {"results": results}


@app.get("/api/product")
async def product(url: str):
    data = await parser.get_product(url)
    if not data:
        return {"error": "not found"}
    return data


@app.get("/api/tracked/{user_id}")
async def tracked(user_id: int):
    items = tracker.get_user_items(user_id)
    return {"items": items}


@app.post("/api/track")
async def track(user_id: int, url: str):
    data = await parser.get_product(url)
    if not data:
        return {"error": "not found"}
    added = tracker.add(user_id, url, data)
    return {"added": added}


@app.post("/api/untrack")
async def untrack(user_id: int, url: str):
    removed = tracker.remove(user_id, url)
    return {"removed": removed}


@app.get("/api/categories")
async def categories():
    return {"categories": [
        {"id": 18644, "title": "Ремни", "slug": "remni-18644"},
        {"id": 18868, "title": "Ремни (мужское)", "slug": "remni-18868"},
        {"id": 19537, "title": "Ремни (мужское)", "slug": "remni-19537"},
        {"id": 18413, "title": "Одежда", "slug": "odezhda"},
        {"id": 18000, "title": "Обувь", "slug": "obuv"},
        {"id": 18100, "title": "Сумки", "slug": "sumki"},
        {"id": 18200, "title": "Аксессуары", "slug": "aksessuary"},
    ]}
]}

Теперь измени `Procfile` чтобы запускались оба сервиса:
]}
web: uvicorn api:app --host 0.0.0.0 --port $PORT

worker: python bot.py
