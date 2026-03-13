import asyncio
import json
import logging
import re
from typing import Optional
import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL    = "https://outlet.tsum.ru"
API_BASE    = "https://api.tsum.ru"
PRODUCT_URL = f"{API_BASE}/v4/catalog/product"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "ru",
    "Content-Type": "application/json",
    "Origin": "https://outlet.tsum.ru",
    "Referer": "https://outlet.tsum.ru/",
    "x-store": "outlet",
    "x-site-region": "RU",
}

COMING_SOON_URLS = [
    f"{BASE_URL}/women/clothes/",
    f"{BASE_URL}/men/clothes/",
    f"{BASE_URL}/women/shoes/",
    f"{BASE_URL}/men/shoes/",
    f"{BASE_URL}/women/bags/",
    f"{BASE_URL}/accessories/",
    f"{BASE_URL}/catalog/",
    f"{BASE_URL}/",
]

SCAN_CONCURRENCY = 10


class TsumOutletParser:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
                connector=aiohttp.TCPConnector(ssl=False, limit=50),
            )
        return self._session

    # ── Public API ───────────────────────────────────────────────────────────

    async def get_product(self, url: str) -> Optional[dict]:
        url = url.strip().rstrip("/")
        slug = self._slug(url)
        p = await self._api_product(slug, url)
        if p:
            return p
        return await self._html_product(url)

    async def search_products(self, query: str, limit: int = 8) -> list:
        results = await self._api_search(query, limit)
        return results or await self._html_search(query, limit)

    async def scan_full_catalog(self) -> tuple[list, int]:
        """
        Scan the full outlet catalog using concurrent requests.
        Returns (items, api_total_count).
        Two-pass strategy:
          Pass 1 — concurrent (semaphore=10), collect failed pages
          Pass 2 — retry failed pages sequentially with delay
        """
        sess = await self._session_()
        sem  = asyncio.Semaphore(SCAN_CONCURRENCY)

        # Step 1: page 1 to get total
        try:
            async with sess.post(
                f"{API_BASE}/v4/catalog/search",
                json={"page": 1, "limit": 40}
            ) as r:
                data        = await r.json(content_type=None)
                pagination  = data.get("pagination") or {}
                total_pages = pagination.get("pageCount", 1)
                total_items = pagination.get("totalCount", 0)
                first_items = data.get("models") or []
        except Exception as e:
            logger.error(f"Catalog scan page 1 failed: {e}")
            return [], 0

        logger.info(
            f"Catalog scan started: {total_items} items, "
            f"{total_pages} pages (concurrency={SCAN_CONCURRENCY})"
        )

        all_items   = self._norm_models_list(first_items)
        failed_pages = []

        # Step 2: concurrent pass
        async def fetch_page(page: int) -> tuple[int, list]:
            async with sem:
                for attempt in range(3):
                    try:
                        async with sess.post(
                            f"{API_BASE}/v4/catalog/search",
                            json={"page": page, "limit": 40}
                        ) as r:
                            if r.status == 429:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            if r.status != 200:
                                logger.debug(f"Page {page} status {r.status}")
                                return page, []
                            d = await r.json(content_type=None)
                            items = self._norm_models_list(d.get("models") or [])
                            return page, items
                    except Exception as e:
                        logger.debug(f"Page {page} attempt {attempt+1}: {e}")
                        await asyncio.sleep(1)
                return page, []  # mark as failed

        tasks   = [fetch_page(p) for p in range(2, total_pages + 1)]
        results = await asyncio.gather(*tasks)

        for page, batch in results:
            if batch:
                all_items.extend(batch)
            else:
                failed_pages.append(page)

        logger.info(
            f"Pass 1 done: {len(all_items)} items, "
            f"{len(failed_pages)} failed pages"
        )

        # Step 3: retry failed pages in small batches
        if failed_pages:
            logger.info(f"Retrying {len(failed_pages)} failed pages in batches of 5...")
            retry_sem = asyncio.Semaphore(5)
            recovered = 0

            async def retry_page(page: int) -> tuple[int, list]:
                async with retry_sem:
                    await asyncio.sleep(0.5)
                    for attempt in range(4):
                        try:
                            async with sess.post(
                                f"{API_BASE}/v4/catalog/search",
                                json={"page": page, "limit": 40}
                            ) as r:
                                if r.status == 429:
                                    await asyncio.sleep(2 * (attempt + 1))
                                    continue
                                if r.status == 200:
                                    d = await r.json(content_type=None)
                                    return page, self._norm_models_list(d.get("models") or [])
                        except Exception as e:
                            logger.debug(f"Retry page {page} attempt {attempt+1}: {e}")
                            await asyncio.sleep(1)
                    return page, []

            retry_results = await asyncio.gather(*[retry_page(p) for p in failed_pages])
            for page, batch in retry_results:
                if batch:
                    all_items.extend(batch)
                    recovered += 1

            logger.info(f"Pass 2 done: recovered {recovered}/{len(failed_pages)} pages")

        fetched = len(all_items)
        coverage = round(fetched / total_items * 100, 1) if total_items else 0
        logger.info(
            f"Catalog scan complete: {fetched}/{total_items} items "
            f"({coverage}% coverage)"
        )
        return all_items, total_items

    async def scan_search_index(
        self,
        known_urls: set,
        max_pages: int = 20,
        yandex_user: str = "",
        yandex_key: str = "",
    ) -> list:
        """
        Find historically sold products by:
        1. Parsing outlet.tsum.ru sitemap.xml (most reliable, no IP blocks)
        2. Falling back to Yandex XML API if sitemap unavailable
        """
        sess = await self._session_()
        found_urls = set()

        # ── Source 1: sitemap.xml ─────────────────────────────────────────────
        logger.info("Search index: trying sitemap.xml...")
        sitemap_urls = await self._fetch_sitemap(sess)
        if sitemap_urls:
            found_urls |= sitemap_urls
            logger.info(f"Sitemap: found {len(sitemap_urls)} product URLs")
        elif yandex_user and yandex_key:
            logger.info("Sitemap empty — trying Yandex XML...")
            for page_num in range(max_pages):
                try:
                    params = {
                        "user":    yandex_user,
                        "key":     yandex_key,
                        "query":   "site:outlet.tsum.ru/product/",
                        "page":    page_num,
                        "groupby": "attr=d.mode=deep.groups-on-page=10.docs-in-group=1",
                        "lr":      "213",
                    }
                    async with sess.get(
                        "https://yandex.ru/search/xml", params=params,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        if r.status != 200:
                            logger.info(f"Yandex XML stopped at page {page_num}: status {r.status}")
                            break
                        xml_text = await r.text()
                    soup_xml = BeautifulSoup(xml_text, "xml")
                    if soup_xml.find("error"):
                        logger.warning(f"Yandex XML error: {soup_xml.find('error').get_text()}")
                        break
                    page_urls = set()
                    for url_tag in soup_xml.find_all("url"):
                        u = url_tag.get_text(strip=True)
                        if "outlet.tsum.ru/product/" in u:
                            page_urls.add(u.rstrip("/"))
                    found_urls |= page_urls
                    logger.info(f"Yandex XML page {page_num+1}: {len(page_urls)} URLs")
                    if not page_urls:
                        break
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Yandex XML page {page_num} error: {e}")
                    break
        else:
            logger.info("No sitemap URLs and no Yandex credentials — skipping")

        # ── Step 2: check which URLs are no longer live ───────────────────────
        unknown_urls = list(found_urls - known_urls)
        logger.info(f"Checking {len(unknown_urls)} unknown URLs...")
        if not unknown_urls:
            return []

        sem = asyncio.Semaphore(10)
        async def check_url(url: str) -> dict:
            async with sem:
                await asyncio.sleep(0.1)
                slug = self._slug(url)
                try:
                    async with sess.get(url, headers={**HEADERS, "Accept": "text/html"},
                                        allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        return {"url": url, "slug": slug, "status": "sold" if r.status == 404 else "available"}
                except Exception:
                    return {"url": url, "slug": slug, "status": "unknown"}

        checks = await asyncio.gather(*[check_url(u) for u in unknown_urls[:3000]])
        results = [r for r in checks if r["status"] == "sold"]
        logger.info(f"Search index scan done: {len(results)} historically sold")
        return results

    async def _fetch_sitemap(self, sess) -> set:
        """Fetch product URLs from outlet.tsum.ru sitemap."""
        urls = set()
        candidates = [
            "https://outlet.tsum.ru/sitemap.xml",
            "https://outlet.tsum.ru/sitemap_index.xml",
            "https://outlet.tsum.ru/sitemap-products.xml",
            "https://outlet.tsum.ru/sitemap/products.xml",
        ]
        for sitemap_url in candidates:
            try:
                async with sess.get(sitemap_url, headers={**HEADERS, "Accept": "text/xml,application/xml"},
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        logger.info(f"Sitemap {sitemap_url}: status {r.status}")
                        continue
                    text = await r.text()
                soup = BeautifulSoup(text, "xml")
                # Sitemap index — check sub-sitemaps
                sub_locs = [s.find("loc").get_text(strip=True) for s in soup.find_all("sitemap") if s.find("loc")]
                if sub_locs:
                    logger.info(f"Sitemap index at {sitemap_url}: {len(sub_locs)} sub-sitemaps")
                    for sub_url in sub_locs:
                        if any(k in sub_url.lower() for k in ("product", "catalog", "item")):
                            sub_urls = await self._fetch_single_sitemap(sess, sub_url)
                            urls |= sub_urls
                    if not urls:
                        for sub_url in sub_locs[:15]:
                            urls |= await self._fetch_single_sitemap(sess, sub_url)
                    if urls:
                        return urls
                # Direct sitemap
                page_urls = self._extract_sitemap_urls(soup)
                if page_urls:
                    logger.info(f"Sitemap {sitemap_url}: {len(page_urls)} product URLs")
                    return page_urls
            except Exception as e:
                logger.debug(f"Sitemap {sitemap_url}: {e}")
        return urls

    async def _fetch_single_sitemap(self, sess, url: str) -> set:
        try:
            async with sess.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    return set()
                text = await r.text()
            return self._extract_sitemap_urls(BeautifulSoup(text, "xml"))
        except Exception:
            return set()

    def _extract_sitemap_urls(self, soup: BeautifulSoup) -> set:
        return {
            loc.get_text(strip=True).rstrip("/")
            for loc in soup.find_all("loc")
            if "outlet.tsum.ru/product/" in loc.get_text()
        }


    async def scan_coming_soon_api(self) -> list:
        """
        Fetch coming_soon items from TSUM API with full pagination.
        Tries several filter strategies, uses the first that works.
        """
        sess = await self._session_()
        results = []

        # Try strategies in order — stop at first that returns items
        strategies = [
            {"availableSoon": True},
            {"filter": {"availableSoon": True}},
            {"isAvailableSoon": True},
            {"inStock": False, "isBuyable": True},
        ]

        working_payload = None
        for base_payload in strategies:
            try:
                probe = {**base_payload, "page": 1, "limit": 40}
                async with sess.post(f"{API_BASE}/v4/catalog/search", json=probe) as r:
                    if r.status != 200:
                        continue
                    data  = await r.json(content_type=None)
                    items = data.get("models") or []
                    if not items:
                        logger.info(f"Coming soon strategy {base_payload}: 0 items")
                        continue

                    pagination  = data.get("pagination") or {}
                    total_pages = pagination.get("pageCount") or 1
                    total_count = pagination.get("totalCount") or len(items)
                    logger.info(f"Coming soon strategy {base_payload}: {total_count} total, {total_pages} pages")
                    working_payload = base_payload
                    # Add first page results
                    normed = self._norm_models_list(items)
                    for p in normed:
                        p["coming_soon"] = True
                        p["available"]   = False
                    results.extend(normed)

                    # Fetch remaining pages concurrently
                    if total_pages > 1:
                        sem = asyncio.Semaphore(5)
                        async def fetch_page(page_num):
                            async with sem:
                                await asyncio.sleep(0.2)
                                payload = {**base_payload, "page": page_num, "limit": 40}
                                async with sess.post(f"{API_BASE}/v4/catalog/search", json=payload) as rp:
                                    if rp.status == 200:
                                        d = await rp.json(content_type=None)
                                        pg_items = d.get("models") or []
                                        normed_pg = self._norm_models_list(pg_items)
                                        for p in normed_pg:
                                            p["coming_soon"] = True
                                            p["available"]   = False
                                        return normed_pg
                                return []
                        pages = await asyncio.gather(*[fetch_page(p) for p in range(2, total_pages + 1)])
                        for pg in pages:
                            results.extend(pg)
                    break

            except Exception as e:
                logger.debug(f"Coming soon strategy {base_payload} error: {e}")

        logger.info(f"scan_coming_soon_api total: {len(results)} items (strategy: {working_payload})")
        return results

    async def get_coming_soon(self, category_url: str) -> list:
        """Parse HTML category page to find 'Ожидается поступление' block."""
        try:
            sess    = await self._session_()
            headers = {**HEADERS, "Accept": "text/html,application/xhtml+xml"}
            async with sess.get(category_url, headers=headers) as r:
                if r.status != 200:
                    logger.info(f"Coming soon {category_url}: status={r.status}")
                    return []
                html = await r.text()

            soup = BeautifulSoup(html, "html.parser")

            coming_block = soup.find(
                lambda tag: tag.name in ("div", "section") and
                any("availableSoon" in (c or "") for c in tag.get("class", []))
            )

            logger.info(f"Coming soon {category_url}: html_len={len(html)}, block_found={coming_block is not None}")
            all_classes = set()
            for tag in soup.find_all("div", class_=True)[:100]:
                for c in tag.get("class", []):
                    if any(w in c.lower() for w in ["soon", "available", "coming", "notify", "expect"]):
                        all_classes.add(c)
            if all_classes:
                logger.info(f"Relevant classes on page: {all_classes}")

            if not coming_block:
                return []

            results = []
            for card in coming_block.select("a[href*='/product/']"):
                href = card.get("href", "")
                if not href.startswith("http"):
                    href = BASE_URL + href

                img       = card.select_one("img[src], img[data-src]")
                image_url = None
                if img:
                    image_url = img.get("src") or img.get("data-src")
                    if image_url and ("placeholder" in image_url or len(image_url) < 20):
                        image_url = None

                brand_el     = card.select_one("[class*='brand'], [class*='Brand']")
                name_el      = card.select_one("[class*='name'], [class*='Name'], [class*='title'], [class*='Title']")
                price_el     = card.select_one("[class*='price'], [class*='Price']")
                old_price_el = card.select_one("[class*='old'], [class*='Old'], [class*='crossed'], [class*='original']")

                brand     = brand_el.get_text(strip=True)       if brand_el     else "—"
                name      = name_el.get_text(strip=True)        if name_el      else "—"
                price     = self._price(price_el.get_text()     if price_el     else "")
                old_price = self._price(old_price_el.get_text() if old_price_el else "")

                if href and href != BASE_URL:
                    results.append({
                        "brand": brand, "name": name, "price": price,
                        "old_price": old_price if old_price and old_price != price else None,
                        "image_url": image_url, "url": href,
                        "available": False, "coming_soon": True, "sizes": [],
                    })
            logger.info(f"Coming soon {category_url}: found {len(results)} items")
            return results
        except Exception as e:
            logger.error(f"Coming soon parse error {category_url}: {e}")
            return []

    # ── Product fetching ─────────────────────────────────────────────────────

    async def _api_product(self, slug: str, url: str) -> Optional[dict]:
        if not slug:
            return None
        sess = await self._session_()
        for endpoint in [
            f"{PRODUCT_URL}?slug={slug}",
            f"{API_BASE}/v4/product/{slug}",
            f"{API_BASE}/v4/catalog/products/{slug}",
        ]:
            try:
                async with sess.get(endpoint) as r:
                    if r.status == 200:
                        data   = await r.json(content_type=None)
                        parsed = self._norm_product(data, url)
                        if parsed:
                            return parsed
            except Exception as e:
                logger.debug(f"API GET error {endpoint}: {e}")
        try:
            async with sess.post(PRODUCT_URL, json={"slug": slug}) as r:
                if r.status == 200:
                    data   = await r.json(content_type=None)
                    parsed = self._norm_product(data, url)
                    if parsed:
                        return parsed
        except Exception as e:
            logger.debug(f"API POST error: {e}")
        return None

    async def _html_product(self, url: str) -> Optional[dict]:
        try:
            sess = await self._session_()
            async with sess.get(url) as r:
                if r.status != 200:
                    return None
                html = await r.text()
            soup = BeautifulSoup(html, "html.parser")
            return (
                self._next_data_product(soup, url) or
                self._jsonld_product(soup, url) or
                self._bare_html_product(soup, url)
            )
        except Exception as e:
            logger.error(f"HTML error {url}: {e}")
            return None

    def _next_data_product(self, soup, url):
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag:
            return None
        try:
            nd    = json.loads(tag.string or "")
            props = nd.get("props", {}).get("pageProps", {})
            for key in ("product", "initialProduct", "item", "productData"):
                p = props.get(key)
                if p:
                    return self._norm_product(p, url)
        except Exception:
            pass
        return None

    def _jsonld_product(self, soup, url):
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data  = json.loads(tag.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "Product":
                        brand      = item.get("brand") or {}
                        brand_name = brand.get("name") if isinstance(brand, dict) else str(brand)
                        offers     = item.get("offers") or {}
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price    = self._price(str(offers.get("price", "")))
                        in_stock = "InStock" in str(offers.get("availability", ""))
                        return {
                            "brand": brand_name or "—", "name": item.get("name", "—"),
                            "article": item.get("sku"), "price": price, "old_price": None,
                            "discount": None, "available": in_stock, "sizes": [], "colors": [],
                            "condition": None, "url": url, "coming_soon": False,
                        }
            except Exception:
                continue
        return None

    def _bare_html_product(self, soup, url):
        brand = self._txt(soup, [".product-brand", ".brand-name", "[data-brand]", ".product__brand"])
        name  = self._txt(soup, ["h1.product-title", "h1", ".product__name", ".product-name"])
        if not name:
            return None
        price     = self._price(self._txt(soup, [".price-current", ".product-price__current", ".price", ".outlet-price"]))
        old_price = self._price(self._txt(soup, [".price-old", ".product-price__old", ".price--old"]))
        sizes     = []
        for el in soup.select(".size-picker__item, .size-selector__item, [data-size], .product-sizes__item"):
            sv    = el.get("data-size") or el.get_text(strip=True)
            avail = not any(c in el.get("class", []) for c in ("unavailable", "disabled", "out-of-stock"))
            if sv:
                sizes.append({"size": sv, "available": avail, "qty": None})
        ct = soup.select_one(".outlet-condition, .product-condition, [data-condition]")
        return {
            "brand": brand or "—", "name": name, "article": None,
            "price": price, "old_price": old_price,
            "discount": self._calc_discount(price, old_price),
            "available": any(s["available"] for s in sizes) if sizes else True,
            "sizes": sizes, "colors": [],
            "condition": ct.get_text(strip=True) if ct else None,
            "url": url, "coming_soon": False,
        }

    # ── Normalization ────────────────────────────────────────────────────────

    def _norm_product(self, data: dict, url: str) -> Optional[dict]:
        item = data.get("product") or data.get("item") or data.get("data") or data
        if not isinstance(item, dict) or not item.get("name"):
            return None
        brand      = item.get("brand") or {}
        brand_name = brand.get("name") if isinstance(brand, dict) else str(brand or "—")
        pd         = item.get("price") or {}
        if isinstance(pd, dict):
            price     = pd.get("current") or pd.get("value") or pd.get("sale")
            old_price = pd.get("old") or pd.get("crossed") or pd.get("original")
        elif isinstance(pd, (int, float)):
            price, old_price = pd, None
        else:
            price     = self._price(str(pd))
            old_price = None
        price     = int(price)     if price     else None
        old_price = int(old_price) if old_price else None
        discount  = self._calc_discount(price, old_price) or (int(item.get("discount") or item.get("discountPercent") or 0) or None)
        sizes       = []
        offers_list = item.get("offers") or item.get("sizes") or item.get("variants") or []
        for offer in offers_list:
            if not isinstance(offer, dict):
                continue
            # Size can be a nested dict (TSUM API) or a plain string
            size_raw = offer.get("size") or {}
            if isinstance(size_raw, dict):
                sv = (
                    size_raw.get("russianSize") or
                    size_raw.get("vendorSize") or
                    size_raw.get("label") or
                    str(size_raw.get("id", ""))
                )
            else:
                sv = (
                    str(size_raw) or
                    offer.get("value") or
                    offer.get("label") or
                    offer.get("name") or
                    ""
                )
            qty   = offer.get("quantity") or offer.get("qty")
            qty   = int(qty) if qty is not None else None
            avail = bool(offer.get("available", offer.get("inStock", True)))
            if qty is not None:
                avail = qty > 0
            if sv:
                sizes.append({
                    "size":      sv.strip(),
                    "available": avail,
                    "qty":       qty,
                })
        available   = any(s["available"] for s in sizes) if sizes else bool(item.get("available", item.get("inStock", True)))
        coming_soon = False
        if offers_list:
            has_qty_info     = any("quantity" in o for o in offers_list)
            has_buyable_info = any("isBuyable" in o for o in offers_list)
            if has_qty_info:
                all_zero    = all(int(o.get("quantity", 0)) == 0 for o in offers_list)
                is_buyable  = any(o.get("isBuyable", False) for o in offers_list)
                coming_soon = all_zero and (is_buyable or not has_buyable_info)
        colors = []
        cf     = item.get("color") or item.get("colors") or []
        if isinstance(cf, str):
            colors = [cf]
        elif isinstance(cf, list):
            colors = [c.get("name", c) if isinstance(c, dict) else str(c) for c in cf]
        condition = item.get("condition") or item.get("grade") or item.get("state")
        return {
            "brand": brand_name, "name": item.get("name") or item.get("title") or "—",
            "article": item.get("article") or item.get("sku") or item.get("vendorCode"),
            "price": price, "old_price": old_price, "discount": discount,
            "available": available, "sizes": sizes, "colors": colors,
            "condition": str(condition) if condition else None,
            "url": item.get("url") or url, "coming_soon": coming_soon,
        }

    def _norm_models_list(self, items: list) -> list:
        out = []
        for item in items:
            brand      = item.get("brand") or {}
            brand_name = brand.get("title") or brand.get("name") if isinstance(brand, dict) else str(brand or "—")
            offers     = item.get("offers") or []
            price, old_price       = None, None
            available, coming_soon = True, False
            sizes                  = []

            if offers:
                p         = offers[0].get("price") or {}
                price     = p.get("priceWithDiscount") or p.get("currentPrice")
                old_price = p.get("originalPrice") or p.get("oldPrice")

                # quantity + isBuyable confirmed present in API response
                all_zero   = all(int(o.get("quantity", 0)) == 0 for o in offers)
                has_stock  = any(int(o.get("quantity", 0)) > 0 for o in offers)
                is_buyable = any(o.get("isBuyable", False) for o in offers)

                available   = has_stock
                coming_soon = all_zero and is_buyable

                for offer in offers:
                    size_info  = offer.get("size") or {}
                    size_label = (
                        size_info.get("russianSize") or size_info.get("vendorSize") or
                        size_info.get("label") or str(size_info.get("id", ""))
                    ) if isinstance(size_info, dict) else str(size_info)
                    qty = int(offer.get("quantity", 0))
                    if size_label:
                        sizes.append({"size": size_label, "available": qty > 0, "qty": qty})

            slug      = item.get("slug") or str(item.get("id", ""))
            images    = item.get("images") or []
            image_url = images[0].get("small") if images else None

            out.append({
                "brand":       brand_name or "—",
                "name":        item.get("title") or item.get("name") or "—",
                "price":       int(price) if price else None,
                "old_price":   int(old_price) if old_price else None,
                "image_url":   image_url,
                "url":         f"https://outlet.tsum.ru/product/{slug}",
                "available":   available,
                "coming_soon": coming_soon,
                "sizes":       sizes,
            })
        return out

    # ── Search ───────────────────────────────────────────────────────────────

    async def _api_search(self, query: str, limit: int) -> list:
        sess = await self._session_()
        try:
            async with sess.post(
                f"{API_BASE}/v4/catalog/search",
                json={"q": query}
            ) as r:
                if r.status == 200:
                    data  = await r.json(content_type=None)
                    items = data.get("models") or []
                    if items:
                        return self._norm_models_list(items[:limit])
        except Exception as e:
            logger.error(f"Search error: {e}")
        return []

    async def _html_search(self, query: str, limit: int) -> list:
        try:
            sess = await self._session_()
            url  = f"{BASE_URL}/catalog/search/?q={query}"
            async with sess.get(url) as r:
                if r.status != 200:
                    return []
                html = await r.text()
            soup   = BeautifulSoup(html, "html.parser")
            nd_tag = soup.find("script", id="__NEXT_DATA__")
            if nd_tag:
                try:
                    nd    = json.loads(nd_tag.string or "")
                    props = nd.get("props", {}).get("pageProps", {})
                    items = (
                        props.get("products") or props.get("items") or
                        props.get("catalog", {}).get("products") or []
                    )
                    if items:
                        return self._norm_search_list(items)[:limit]
                except Exception:
                    pass
            results = []
            for card in soup.select(".product-card, .catalog-item, [data-product]")[:limit]:
                name  = self._txt(card, [".product-name", ".title", "h2", "h3"])
                price = self._price(self._txt(card, [".price", ".product-price"]))
                a_tag = card.find("a")
                href  = a_tag["href"] if a_tag and a_tag.get("href") else ""
                if not href.startswith("http"):
                    href = BASE_URL + href
                if name:
                    results.append({
                        "brand": self._txt(card, [".brand"]) or "—",
                        "name": name, "price": price, "sizes": [],
                        "url": href, "available": True, "coming_soon": False,
                    })
            return results
        except Exception as e:
            logger.error(f"HTML search error: {e}")
            return []

    def _norm_search_list(self, items: list) -> list:
        out = []
        for item in items:
            brand      = item.get("brand") or {}
            brand_name = brand.get("name") if isinstance(brand, dict) else str(brand or "—")
            pd         = item.get("price") or {}
            price      = pd.get("current") if isinstance(pd, dict) else pd
            slug       = item.get("slug") or item.get("url") or item.get("id", "")
            url        = slug if str(slug).startswith("http") else f"{BASE_URL}/product/{slug}"
            out.append({
                "brand": brand_name, "name": item.get("name") or item.get("title") or "—",
                "price": int(price) if price else None, "sizes": [],
                "url": url, "available": True, "coming_soon": False,
            })
        return out

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _slug(self, url: str) -> str:
        for pat in [
            r"outlet\.tsum\.ru/product/([^/?#]+)",
            r"outlet\.tsum\.ru/catalog/[^/]+/([^/?#]+)",
            r"outlet\.tsum\.ru/[^/]+/([^/?#]{10,})",
        ]:
            m = re.search(pat, url)
            if m:
                return m.group(1)
        return ""

    def _txt(self, soup, selectors: list) -> Optional[str]:
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if t:
                    return t
        return None

    def _price(self, text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else None

    def _calc_discount(self, price: Optional[int], old_price: Optional[int]) -> Optional[int]:
        if price and old_price and old_price > price:
            return round((1 - price / old_price) * 100)
        return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
