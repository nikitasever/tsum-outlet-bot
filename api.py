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
        {"id": 18413, "title": "Одежда", "slug": "odezhda"},
        {"id": 18000, "title": "Обувь", "slug": "obuv"},
        {"id": 18100, "title": "Сумки", "slug": "sumki"},
        {"id": 18200, "title": "Аксессуары", "slug": "aksessuary"},
    ]}
import json, os, time

CATALOG_FILE = "catalog_store.json"

def load_catalog():
    if os.path.exists(CATALOG_FILE):
        with open(CATALOG_FILE) as f:
            return json.load(f)
    return {}

def save_catalog(data):
    with open(CATALOG_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)

@app.post("/api/catalog/save")
async def catalog_save(products: list[dict]):
    catalog = load_catalog()
    sold = load_sold()
    now = int(time.time())
    for p in products:
        url = p.get("url")
        if not url:
            continue
        prev = catalog.get(url)
        # Детект продажи: был available, стал нет
        if prev and prev["product"].get("available") and not p.get("available"):
            sold.append({
                "product": p,
                "ts": now,
                "prev_price": prev["product"].get("price")
            })
        if url not in catalog:
            catalog[url] = {"product": p, "history": []}
        catalog[url]["product"] = p
        history = catalog[url]["history"]
        if not history or history[-1]["price"] != p.get("price"):
            history.append({"price": p.get("price"), "ts": now})
        catalog[url]["history"] = history[-30:]
    save_catalog(catalog)
    save_sold(sold[-1000:])
    return {"saved": len(products)}

@app.get("/api/stats")
async def stats():
    catalog = load_catalog()
    items = list(catalog.values())
    # топ скидок
    top_discount = sorted(
        items,
        key=lambda x: x["product"].get("old_price", 0) - x["product"].get("price", 0),
        reverse=True
    )[:20]
    # недавние снижения цен
    price_drops = [
        i for i in items
        if len(i["history"]) >= 2 and i["history"][-1]["price"] < i["history"][-2]["price"]
    ]
    return {
        "total": len(items),
        "top_discount": [i["product"] for i in top_discount],
        "price_drops": [i["product"] for i in price_drops[:20]],
    }
SOLD_FILE = "sold_store.json"

def load_sold():
    if os.path.exists(SOLD_FILE):
        with open(SOLD_FILE) as f:
            return json.load(f)
    return []

def save_sold(data):
    with open(SOLD_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)

@app.get("/api/sold")
async def sold_today():
    sold = load_sold()
    today = int(time.time()) - 86400
    recent = [s for s in sold if s.get("ts", 0) > today]
    return {"count": len(recent), "items": recent}

@app.get("/api/coming_soon")
async def coming_soon():
    catalog = load_catalog()
    items = [v["product"] for v in catalog.values() if v["product"].get("coming_soon")]
    return {"count": len(items), "items": items}
