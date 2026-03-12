import asyncio
import json
import logging
import os
import time
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tsum_parser import TsumOutletParser, CATEGORY_SLUGS, COMING_SOON_URLS
from tracker import ProductTracker

logger = logging.getLogger(__name__)

# ── Storage helpers ──────────────────────────────────────────────────────────

CATALOG_FILE = "catalog_store.json"
SOLD_FILE    = "sold_store.json"
EVENTS_FILE  = "events_store.json"


def load_catalog():
    if os.path.exists(CATALOG_FILE):
        with open(CATALOG_FILE) as f:
            return json.load(f)
    return {}


def save_catalog(data):
    with open(CATALOG_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def load_sold():
    if os.path.exists(SOLD_FILE):
        with open(SOLD_FILE) as f:
            return json.load(f)
    return []


def save_sold(data):
    with open(SOLD_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def load_events():
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE) as f:
            return json.load(f)
    return []


def save_events(data):
    with open(EVENTS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def _upsert_product(catalog: dict, sold: list, p: dict, now: int):
    """Save product to catalog, detect sales."""
    url = p.get("url")
    if not url:
        return
    prev = catalog.get(url)
    # Detect sale: was available, now not available and not coming_soon
    if prev and prev["product"].get("available") and not p.get("available") and not p.get("coming_soon"):
        sold.append({
            "product":    p,
            "ts":         now,
            "prev_price": prev["product"].get("price"),
        })
    if url not in catalog:
        catalog[url] = {"product": p, "history": []}
    catalog[url]["product"] = p
    history = catalog[url]["history"]
    if not history or history[-1]["price"] != p.get("price"):
        history.append({"price": p.get("price"), "ts": now})
    catalog[url]["history"] = history[-30:]


# ── Background scanner ───────────────────────────────────────────────────────

async def scan_all_categories():
    scanner = TsumOutletParser()
    while True:
        try:
            logger.info("🔍 Начало сканирования каталога...")
            catalog = load_catalog()
            sold    = load_sold()
            now     = int(time.time())

            # Full category scan with pagination
            for slug in CATEGORY_SLUGS:
                try:
                    results = await scanner.scan_category(slug, max_pages=15)
                    for p in results:
                        _upsert_product(catalog, sold, p, now)
                    # Save after each category to avoid data loss
                    save_catalog(catalog)
                    save_sold(sold[-1000:])
                except Exception as e:
                    logger.error(f"Category scan error '{slug}': {e}")
                await asyncio.sleep(3)

            # HTML-based coming_soon scan
            for cat_url in COMING_SOON_URLS:
                try:
                    coming = await scanner.get_coming_soon(cat_url)
                    for p in coming:
                        _upsert_product(catalog, sold, p, now)
                    if coming:
                        save_catalog(catalog)
                except Exception as e:
                    logger.error(f"Coming soon scan error {cat_url}: {e}")
                await asyncio.sleep(3)

            save_catalog(catalog)
            save_sold(sold[-1000:])
            logger.info(f"✅ Сканирование завершено. Товаров в базе: {len(catalog)}")

        except Exception as e:
            logger.error(f"Scanner error: {e}")

        await asyncio.sleep(2 * 60 * 60)  # next full scan in 2 hours


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(scan_all_categories())
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

parser  = TsumOutletParser()
tracker = ProductTracker()


# ── Endpoints ────────────────────────────────────────────────────────────────

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
        {"id": 1, "title": "Женская одежда", "slug": "women-odezhda"},
        {"id": 2, "title": "Мужская одежда", "slug": "men-odezhda"},
        {"id": 3, "title": "Женская обувь",  "slug": "women-obuv"},
        {"id": 4, "title": "Мужская обувь",  "slug": "men-obuv"},
        {"id": 5, "title": "Сумки",          "slug": "women-sumki"},
        {"id": 6, "title": "Аксессуары",     "slug": "aksessuary"},
    ]}


@app.post("/api/catalog/save")
async def catalog_save(products: list[dict]):
    catalog = load_catalog()
    sold    = load_sold()
    now     = int(time.time())
    for p in products:
        _upsert_product(catalog, sold, p, now)
    save_catalog(catalog)
    save_sold(sold[-1000:])
    return {"saved": len(products)}


@app.post("/api/event")
async def track_event(event: dict):
    events = load_events()
    events.append({
        "type":    event.get("type"),
        "url":     event.get("url"),
        "user_id": event.get("user_id"),
        "ts":      int(time.time()),
    })
    save_events(events[-5000:])
    return {"ok": True}


@app.get("/api/stats")
async def stats():
    catalog = load_catalog()
    events  = load_events()
    items   = list(catalog.values())

    views  = Counter(e["url"] for e in events if e.get("type") == "view")
    clicks = Counter(e["url"] for e in events if e.get("type") == "click")

    top_discount = sorted(
        items,
        key=lambda x: (x["product"].get("old_price") or 0) - (x["product"].get("price") or 0),
        reverse=True,
    )[:20]

    price_drops = [
        i for i in items
        if len(i["history"]) >= 2
        and (i["history"][-1]["price"] or 0) < (i["history"][-2]["price"] or 0)
    ]

    top_views = sorted(
        [i for i in items if views.get(i["product"].get("url", ""), 0) > 0],
        key=lambda x: views.get(x["product"].get("url", ""), 0),
        reverse=True,
    )[:20]

    top_clicks = sorted(
        [i for i in items if clicks.get(i["product"].get("url", ""), 0) > 0],
        key=lambda x: clicks.get(x["product"].get("url", ""), 0),
        reverse=True,
    )[:20]

    return {
        "total":        len(items),
        "top_discount": [i["product"] for i in top_discount],
        "price_drops":  [i["product"] for i in price_drops[:20]],
        "top_views":    [{"product": i["product"], "views":  views[i["product"]["url"]]}  for i in top_views],
        "top_clicks":   [{"product": i["product"], "clicks": clicks[i["product"]["url"]]} for i in top_clicks],
    }


@app.get("/api/sold")
async def sold_today():
    sold   = load_sold()
    since  = int(time.time()) - 86400
    recent = [s for s in sold if s.get("ts", 0) > since]
    return {"count": len(recent), "items": recent}


@app.get("/api/coming_soon")
async def coming_soon():
    catalog = load_catalog()
    items   = [v["product"] for v in catalog.values() if v["product"].get("coming_soon")]
    return {"count": len(items), "items": items}
