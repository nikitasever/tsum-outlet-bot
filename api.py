import asyncio
import json
import logging
import os
import time
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tsum_parser import TsumOutletParser, COMING_SOON_URLS
from tracker import ProductTracker

logger = logging.getLogger(__name__)

CATALOG_FILE = "catalog_store.json"
SOLD_FILE    = "sold_store.json"
EVENTS_FILE  = "events_store.json"
META_FILE    = "scan_meta.json"


# ── Storage helpers ──────────────────────────────────────────────────────────

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


def load_meta():
    if os.path.exists(META_FILE):
        with open(META_FILE) as f:
            return json.load(f)
    return {}


def save_meta(data):
    with open(META_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


# ── Upsert with sold detection ───────────────────────────────────────────────

def _total_qty(product: dict) -> int:
    """Sum of all size quantities for a product."""
    sizes = product.get("sizes") or []
    if sizes and any(s.get("qty") is not None for s in sizes):
        return sum(s.get("qty") or 0 for s in sizes)
    return -1  # unknown


def _upsert_product(catalog, sold, p, now):
    url = p.get("url")
    if not url:
        return

    prev = catalog.get(url)

    if prev:
        prev_product = prev["product"]
        prev_sizes = {s["size"]: s for s in (prev_product.get("sizes") or [])}
        curr_sizes = {s["size"]: s for s in (p.get("sizes") or [])}

        # Whole product sold out
        if prev_product.get("available") and not p.get("available") and not p.get("coming_soon"):
            sold.append({
                "type": "product",
                "product": p,
                "ts": now,
                "prev_price": prev_product.get("price"),
                "size": None,
            })
        else:
            # Individual size sold out
            for size_label, prev_size in prev_sizes.items():
                curr_size = curr_sizes.get(size_label)
                if (
                    prev_size.get("available") and
                    curr_size is not None and
                    not curr_size.get("available")
                ):
                    sold.append({
                        "type": "size",
                        "product": p,
                        "ts": now,
                        "prev_price": prev_product.get("price"),
                        "size": size_label,
                    })

        # Track qty velocity — how many units sold since last scan
        prev_qty = _total_qty(prev_product)
        curr_qty = _total_qty(p)
        if prev_qty >= 0 and curr_qty >= 0 and curr_qty < prev_qty:
            units_sold = prev_qty - curr_qty
            qty_history = prev.get("qty_history") or []
            qty_history.append({"ts": now, "sold": units_sold, "remaining": curr_qty})
            qty_history = qty_history[-48:]  # keep 48 data points (~24h at 30min scans)
            prev["qty_history"] = qty_history

    if url not in catalog:
        catalog[url] = {"product": p, "history": [], "qty_history": []}

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
            t_start = time.time()
            logger.info("🔍 Начало сканирования каталога...")

            catalog = load_catalog()
            sold = load_sold()
            now = int(time.time())

            # Full catalog scan — picks up coming_soon via quantity=0 + isBuyable=true
            results, api_total = await scanner.scan_full_catalog()
            coming_in_catalog = 0
            for p in results:
                _upsert_product(catalog, sold, p, now)
                if p.get("coming_soon"):
                    coming_in_catalog += 1

            save_catalog(catalog)
            save_sold(sold[-5000:])
            logger.info(
                f"Catalog scan done in {round(time.time()-t_start, 1)}s. "
                f"DB: {len(catalog)}, coming_soon in catalog: {coming_in_catalog}"
            )

            # Strategy 1: direct API coming_soon scan (tries different filters)
            coming_api = await scanner.scan_coming_soon_api()
            for p in coming_api:
                _upsert_product(catalog, sold, p, now)
            logger.info(f"Coming soon via API filter: {len(coming_api)} items")

            # Strategy 2: HTML coming_soon scan (parallel across all category URLs)
            async def scan_coming(url):
                try:
                    return await scanner.get_coming_soon(url)
                except Exception as e:
                    logger.error(f"Coming soon error {url}: {e}")
                    return []

            coming_results = await asyncio.gather(*[scan_coming(u) for u in COMING_SOON_URLS])
            html_count = 0
            for coming in coming_results:
                for p in coming:
                    _upsert_product(catalog, sold, p, now)
                    html_count += 1
            logger.info(f"Coming soon via HTML: {html_count} items")

            save_catalog(catalog)
            save_sold(sold[-5000:])

            total_coming = len([v for v in catalog.values() if v["product"].get("coming_soon")])
            sold_today_count = len([s for s in sold if s.get("ts", 0) > now - 86400])

            meta = {
                "last_scan_ts":      now,
                "last_scan_dur_s":   round(time.time() - t_start, 1),
                "api_total":         api_total,        # official TSUM count
                "catalog_db":        len(catalog),     # our DB (accumulated)
                "fetched_this_scan": len(results),     # actually fetched this run
                "coming_soon":       total_coming,
                "sold_24h":          sold_today_count,
            }
            save_meta(meta)

            logger.info(
                f"✅ Сканирование завершено за {meta['last_scan_dur_s']}s. "
                f"API: {api_total}, получено: {len(results)}, "
                f"в базе: {len(catalog)}, ожидается: {total_coming}, "
                f"продаж сегодня: {sold_today_count}"
            )

        except Exception as e:
            logger.error(f"Scanner error: {e}")

        await asyncio.sleep(30 * 60)  # scan every 30 minutes


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

parser = TsumOutletParser()
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
    sold = load_sold()
    now = int(time.time())
    for p in products:
        _upsert_product(catalog, sold, p, now)
    save_catalog(catalog)
    save_sold(sold[-5000:])
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
    events = load_events()
    items = list(catalog.values())

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
    sold = load_sold()
    since = int(time.time()) - 86400
    recent = [s for s in sold if s.get("ts", 0) > since]
    return {"count": len(recent), "items": recent}


@app.get("/api/coming_soon")
async def coming_soon():
    catalog = load_catalog()
    items = [v["product"] for v in catalog.values() if v["product"].get("coming_soon")]
    return {"count": len(items), "items": items}


@app.get("/api/hot")
async def hot_items(hours: int = 24, limit: int = 30):
    """
    Returns fastest-selling items based on qty drops between scans.
    velocity = total units sold in last `hours` hours.
    """
    catalog = load_catalog()
    now = int(time.time())
    since = now - hours * 3600

    results = []
    for entry in catalog.values():
        qty_history = entry.get("qty_history") or []
        if not qty_history:
            continue

        # Sum units sold within the window
        recent = [h for h in qty_history if h.get("ts", 0) >= since]
        if not recent:
            continue

        total_sold = sum(h.get("sold", 0) for h in recent)
        if total_sold <= 0:
            continue

        # Time span of the data we have
        first_ts = recent[0]["ts"]
        span_hours = max((now - first_ts) / 3600, 0.5)
        velocity = round(total_sold / span_hours, 2)  # units per hour

        remaining = qty_history[-1].get("remaining", None)

        results.append({
            "product":      entry["product"],
            "total_sold":   total_sold,
            "velocity":     velocity,   # units/hour
            "remaining":    remaining,  # current stock
            "data_points":  len(recent),
        })

    results.sort(key=lambda x: x["total_sold"], reverse=True)
    return {
        "hours":   hours,
        "count":   len(results),
        "items":   results[:limit],
    }


@app.get("/api/debug")
async def debug():
    """Health check — shows catalog state and last scan metadata."""
    catalog = load_catalog()
    sold = load_sold()
    meta = load_meta()
    now = int(time.time())

    total = len(catalog)
    available = sum(1 for v in catalog.values() if v["product"].get("available"))
    coming = sum(1 for v in catalog.values() if v["product"].get("coming_soon"))
    with_sizes = sum(1 for v in catalog.values() if v["product"].get("sizes"))
    sold_24h = len([s for s in sold if s.get("ts", 0) > now - 86400])

    last_scan_ago = None
    if meta.get("last_scan_ts"):
        last_scan_ago = round((now - meta["last_scan_ts"]) / 60, 1)

    sample_coming = [
        v["product"] for v in catalog.values()
        if v["product"].get("coming_soon")
    ][:3]

    return {
        # Точные цифры
        "api_total":          meta.get("api_total", "не скачано"),
        "fetched_last_scan":  meta.get("fetched_this_scan", "—"),
        "catalog_db":         total,
        "available":          available,
        "coming_soon":        coming,
        "with_sizes":         with_sizes,
        # Продажи
        "sold_24h":           sold_24h,
        "sold_by_product":    len([s for s in sold if s.get("type") == "product"]),
        "sold_by_size":       len([s for s in sold if s.get("type") == "size"]),
        # Скан
        "last_scan_ago_min":  last_scan_ago,
        "last_scan_dur_s":    meta.get("last_scan_dur_s"),
        "sample_coming_soon": sample_coming,
    }
