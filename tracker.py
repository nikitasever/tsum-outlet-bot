"""
Трекер изменений товаров.
Хранит данные в JSON-файле, уведомляет об изменениях цены и наличия.
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

STORAGE_FILE = "tracked_products.json"


class ProductTracker:
    def __init__(self, storage_path: str = STORAGE_FILE):
        self.path = storage_path
        self._data: dict[str, list] = {}  # {user_id: [items]}
        self._load()

    # ─── Операции ─────────────────────────────────────────────────────────────

    def add(self, user_id: int, url: str, product: dict) -> bool:
        key = str(user_id)
        if key not in self._data:
            self._data[key] = []

        if any(i["url"] == url for i in self._data[key]):
            return False

        self._data[key].append({
            "url": url,
            "brand": product.get("brand", "—"),
            "name": product.get("name", "—"),
            "price": product.get("price"),
            "old_price": product.get("old_price"),
            "available": product.get("available", True),
            "sizes": product.get("sizes", []),
            "added_at": datetime.now().isoformat(),
            "last_checked": datetime.now().isoformat(),
        })
        self._save()
        return True

    def remove(self, user_id: int, url: str) -> bool:
        key = str(user_id)
        items = self._data.get(key, [])
        new_items = [i for i in items if i["url"] != url]
        if len(new_items) == len(items):
            return False
        self._data[key] = new_items
        self._save()
        return True

    def is_tracked(self, user_id: int, url: str) -> bool:
        items = self._data.get(str(user_id), [])
        return any(i["url"] == url for i in items)

    def get_user_items(self, user_id: int) -> list:
        return self._data.get(str(user_id), [])

    def get_all_items(self) -> dict[str, list]:
        """Вернуть все товары всех пользователей."""
        return {uid: items for uid, items in self._data.items() if items}

    def update_and_diff(self, user_id: int, url: str, new_data: dict) -> list[str]:
        """
        Обновить данные товара и вернуть список изменений.
        Пустой список = изменений нет.
        """
        key = str(user_id)
        items = self._data.get(key, [])
        changes = []

        for item in items:
            if item["url"] != url:
                continue

            # Цена
            old_price = item.get("price")
            new_price = new_data.get("price")
            if old_price and new_price and old_price != new_price:
                direction = "📉 снизилась" if new_price < old_price else "📈 выросла"
                old_str = f"{old_price:,}".replace(",", " ")
                new_str = f"{new_price:,}".replace(",", " ")
                diff = abs(new_price - old_price)
                diff_str = f"{diff:,}".replace(",", " ")
                changes.append(
                    f"Цена {direction}: {old_str} ₽ → *{new_str} ₽* (Δ {diff_str} ₽)"
                )

            # Общее наличие
            old_avail = item.get("available")
            new_avail = new_data.get("available")
            if old_avail is not None and old_avail != new_avail:
                status = "🟢 появился в наличии" if new_avail else "🔴 вышел из наличия"
                changes.append(f"Товар {status}")

            # Размеры — детально
            old_sizes = {s["size"]: s["available"] for s in item.get("sizes", [])}
            new_sizes = {s["size"]: s["available"] for s in new_data.get("sizes", [])}

            appeared = []
            disappeared = []
            for size, avail in new_sizes.items():
                if size in old_sizes:
                    if not old_sizes[size] and avail:
                        appeared.append(f"`{size}`")
                    elif old_sizes[size] and not avail:
                        disappeared.append(f"`{size}`")
                else:
                    if avail:
                        appeared.append(f"`{size}` (новый)")

            if appeared:
                changes.append(f"🟢 Появились размеры: {', '.join(appeared)}")
            if disappeared:
                changes.append(f"🔴 Пропали размеры: {', '.join(disappeared)}")

            # Сохранить новые данные
            item.update({
                "price": new_data.get("price", item["price"]),
                "old_price": new_data.get("old_price"),
                "available": new_data.get("available", item["available"]),
                "sizes": new_data.get("sizes", item["sizes"]),
                "last_checked": datetime.now().isoformat(),
            })
            break

        if changes:
            self._save()
        return changes

    # ─── Хранение ─────────────────────────────────────────────────────────────

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(f"Загружено {sum(len(v) for v in self._data.values())} записей.")
            except Exception as e:
                logger.error(f"Ошибка загрузки {self.path}: {e}")
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения {self.path}: {e}")
