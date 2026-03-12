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


class TsumOutletParser:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=HEADERS,
                timeout=aiohttp.ClientTimeout(total=25),
                connector=aiohttp.TCPConnector(ssl=False),
            )
        return self._session

    async def get_product(self, url: str) -> Optional[dict]:
        url = url.strip().rstrip("/")
        slug = self._slug(url)
        p = await self._api_product(slug, url)
        if p:
            return p
        return await self._html_product(url)

    async def _api_product(self, slug: str, url: str) -> Optional[dict]:
        if not slug:
            return None
        sess = await self._session_()
        endpoints_get = [
            f"{PRODUCT_URL}?slug={slug}",
            f"{API_BASE}/v4/product/{slug}",
            f"{API_BASE}/v4/catalog/products/{slug}",
        ]
        for endpoint in endpoints_get:
            try:
                async with sess.get(endpoint) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        parsed = self._norm_product(data, url)
                        if parsed:
                            return parsed
            except Exception as e:
                logger.debug(f"API GET error {endpoint}: {e}")
        try:
            async with sess.post(PRODUCT_URL, json={"slug": slug}) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
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
            nd = json.loads(tag.string or "")
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
                data = json.loads(tag.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "Product":
                        brand = item.get("brand") or {}
                        brand_name = brand.get("name") if isinstance(brand, dict) else str(brand)
                        offers = item.get("offers") or {}
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = self._price(str(offers.get("price", "")))
                        in_stock = "InStock" in str(offers.get("availability", ""))
                        return {
                            "brand": brand_name or "—",
                            "name": item.get("name", "—"),
                            "article": item.get("sku"),
                            "price": price,
                            "old_price": None,
                            "discount": None,
                            "available": in_stock,
                            "sizes": [],
                            "colors": [],
                            "condition": None,
                            "url": url,
                            "coming_soon": False,
                        }
            except Exception:
                continue
        return None

    def _bare_html_product(self, soup, url):
        brand = self._txt(soup, [".product-brand", ".brand-name", "[data-brand]", ".product__brand"])
        name  = self._txt(soup, ["h1.product-title", "h1", ".product__name", ".product-name"])
        if not name:
            return None
        price_raw     = self._txt(soup, [".price-current", ".product-price__current", "[data-price]", ".price", ".outlet-price"])
        old_price_raw = self._txt(soup, [".price-old", ".product-price__old", ".price--old", ".outlet-price--old"])
        price     = self._price(price_raw)
        old_price = self._price(old_price_raw)
        discount  = self._calc_discount(price, old_price)
        sizes = []
        for el in soup.select(".size-picker__item, .size-selector__item, [data-size], .product-sizes__item, .size-btn"):
            sv = el.get("data-size") or el.get_text(strip=True)
            avail = not any(c in el.get("class", []) for c in ("unavailable", "disabled", "out-of-stock"))
            if sv:
                sizes.append({"size": sv, "available": avail, "qty": None})
        ct = soup.select_one(".outlet-condition, .product-condition, [data-condition]")
        return {
            "brand": brand or "—",
            "name": name,
            "article": None,
            "price": price,
            "old_price": old_price,
            "discount": discount,
            "available": any(s["available"] for s in sizes) if sizes else True,
            "sizes": sizes,
            "colors": [],
            "condition": ct.get_text(strip=True) if ct else None,
            "url": url,
            "coming_soon": False,
        }

    def _norm_product(self, data: dict, url: str) -> Optional[dict]:
        item = data.get("product") or data.get("item") or data.get("data") or data
        if not isinstance(item, dict) or not item.get("name"):
            return None
        brand = item.get("brand") or {}
        brand_name = brand.get("name") if isinstance(brand, dict) else str(brand or "—")
        pd = item.get("price") or {}
        if isinstance(pd, dict):
            price     = pd.get("current") or pd.get("value") or pd.get("sale")
            old_price = pd.get("old") or pd.get("crossed") or pd.get("original")
        elif isinstance(pd, (int, float)):
            price, old_price = pd, None
        else:
            price = self._price(str(pd))
            old_price = None
        price     = int(price)     if price     else None
        old_price = int(old_price) if old_price else None
        discount  = self._calc_discount(price, old_price) or (int(item.get("discount") or item.get("discountPercent") or 0) or None)
        sizes = []
        offers_list = item.get("offers") or item.get("sizes") or item.get("variants") or []
        for offer in offers_list:
            if isinstance(offer, dict):
                sv    = offer.get("size") or offer.get("value") or offer.get("label") or offer.get("name") or ""
                avail = offer.get("available", offer.get("inStock", True))
                qty   = offer.get("quantity") or offer.get("qty")
                if sv:
                    sizes.append({
                        "size": str(sv),
                        "available": bool(avail) and (qty is None or int(qty) > 0),
                        "qty": int(qty) if qty else None,
                    })
        available = any(s["available"] for s in sizes) if sizes else bool(item.get("available", item.get("inStock", True)))
        # coming_soon определяем только если API вернул quantity
        coming_soon = False
        if offers_list:
            has_qty_info     = any("quantity" in o for o in offers_list)
            has_buyable_info = any("isBuyable" in o for o in offers_list)
            if has_qty_info:
                all_zero   = all(int(o.get("quantity", 0)) == 0 for o in offers_list)
                is_buyable = any(o.get("isBuyable", False) for o in offers_list)
                # Если isBuyable не передаётся вовсе — достаточно all_zero
                coming_soon = all_zero and (is_buyable or not has_buyable_info)
        colors = []
        cf = item.get("color") or item.get("colors") or []
        if isinstance(cf, str):
            colors = [cf]
        elif isinstance(cf, list):
            colors = [c.get("name", c) if isinstance(c, dict) else str(c) for c in cf]
        condition = item.get("condition") or item.get("grade") or item.get("state")
        return {
            "brand": brand_name,
            "name": item.get("name") or item.get("title") or "—",
            "article": item.get("article") or item.get("sku") or item.get("vendorCode"),
            "price": price,
            "old_price": old_price,
            "discount": discount,
            "available": available,
            "sizes": sizes,
            "colors": colors,
            "condition": str(condition) if condition else None,
            "url": item.get("url") or url,
            "coming_soon": coming_soon,
        }

    async def search_products(self, query: str, limit: int = 8) -> list:
        results = await self._api_search(query, limit)
        return results or await self._html_search(query, limit)

    async def _api_search(self, query: str, limit: int) -> list:
        sess = await self._session_()
        try:
            async with sess.post(
                f"{API_BASE}/v4/catalog/search",
                json={"q": query}
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    items = data.get("models") or []
                    if items:
                        return self._norm_models_list(items[:limit])
        except Exception as e:
            logger.error(f"Search error: {e}")
        return []

    def _norm_models_list(self, items: list) -> list:
        out = []
        for item in items:
            brand = item.get("brand") or {}
            brand_name = brand.get("title") or brand.get("name") if isinstance(brand, dict) else str(brand or "—")
            offers = item.get("offers") or []
            price, old_price = None, None
            available, coming_soon = True, False
            if offers:
                p = offers[0].get("price") or {}
                price     = p.get("priceWithDiscount") or p.get("currentPrice")
                old_price = p.get("originalPrice") or p.get("oldPrice")
                has_qty_info     = any("quantity" in o for o in offers)
                has_buyable_info = any("isBuyable" in o for o in offers)
                if has_qty_info:
                    has_stock  = any(int(o.get("quantity", 0)) > 0 for o in offers)
                    all_zero   = all(int(o.get("quantity", 0)) == 0 for o in offers)
                    is_buyable = any(o.get("isBuyable", False) for o in offers)
                    available   = has_stock
                    coming_soon = all_zero and (is_buyable or not has_buyable_info)
                else:
                    available   = True
                    coming_soon = False
            slug = item.get("slug") or str(item.get("id", ""))
            images = item.get("images") or []
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
            })
        return out

    async def _html_search(self, query: str, limit: int) -> list:
        try:
            sess = await self._session_()
            url = f"{BASE_URL}/catalog/search/?q={query}"
            async with sess.get(url) as r:
                if r.status != 200:
                    return []
                html = await r.text()
            soup = BeautifulSoup(html, "html.parser")
            nd_tag = soup.find("script", id="__NEXT_DATA__")
            if nd_tag:
                try:
                    nd = json.loads(nd_tag.string or "")
                    props = nd.get("props", {}).get("pageProps", {})
                    items = (
                        props.get("products") or
                        props.get("items") or
                        props.get("catalog", {}).get("products") or []
                    )
                    if items:
                        return self._norm_search_list(items)[:limit]
                except Exception:
                    pass
            results = []
            for card in soup.select(".product-card, .catalog-item, .item-card, [data-product]")[:limit]:
                brand = self._txt(card, [".brand", ".product-brand"])
                name  = self._txt(card, [".product-name", ".title", "h2", "h3"])
                price = self._price(self._txt(card, [".price", ".product-price"]))
                a_tag = card.find("a")
                href  = a_tag["href"] if a_tag and a_tag.get("href") else ""
                if not href.startswith("http"):
                    href = BASE_URL + href
                if name:
                    results.append({
                        "brand": brand or "—",
                        "name": name,
                        "price": price,
                        "url": href,
                        "available": True,
                        "coming_soon": False,
                    })
            return results
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    def _norm_search_list(self, items: list) -> list:
        out = []
        for item in items:
            brand = item.get("brand") or {}
            brand_name = brand.get("name") if isinstance(brand, dict) else str(brand or "—")
            pd = item.get("price") or {}
            price = pd.get("current") if isinstance(pd, dict) else pd
            slug = item.get("slug") or item.get("url") or item.get("id", "")
            url  = slug if str(slug).startswith("http") else f"{BASE_URL}/product/{slug}"
            out.append({
                "brand": brand_name,
                "name": item.get("name") or item.get("title") or "—",
                "price": int(price) if price else None,
                "url": url,
                "available": True,
                "coming_soon": False,
            })
        return out

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
