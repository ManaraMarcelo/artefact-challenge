"""Loading and cleaning of the store's operational tables (data/*.csv).

All tables are small (< 100 rows), so they are loaded once into pandas DataFrames and
queried in memory. In production these would be SQL queries against the store's ERP;
the tool layer only depends on the methods exposed here, so swapping the backend
would not touch the agent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

from . import pricing
from .config import DATA_DIR
from .text import normalize

_PREFIX = "desafio_tecnico_ai_eng - "

PRODUCT_STATUS_PT = {
    "active": "ativo",
    "discontinued": "descontinuado",
    "coming_soon": "em breve (pré-lançamento)",
}

ORDER_STATUS_PT = {
    "pending": "aguardando confirmação de pagamento",
    "confirmed": "pagamento confirmado, em separação",
    "shipped": "enviado",
    "delivered": "entregue",
    "cancelled": "cancelado",
}

PAYMENT_METHOD_PT = {
    "pix": "PIX",
    "debit": "cartão de débito",
    "boleto": "boleto bancário",
}

SPEC_LABELS_PT = {
    "top": "tampo",
    "back_sides": "fundo e laterais",
    "neck": "braço",
    "strings": "cordas",
    "scale": "escala",
    "electronics": "eletrônica/captação",
    "color": "cor",
}


def describe_payment(method: str) -> str:
    """'credit_12x' -> 'cartão de crédito em 12x'."""
    match = re.fullmatch(r"credit_(\d+)x", method or "")
    if match:
        return f"cartão de crédito em {match.group(1)}x"
    return PAYMENT_METHOD_PT.get(method, method)


def only_digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _read(name: str, data_dir: Path) -> pd.DataFrame:
    return pd.read_csv(data_dir / f"{_PREFIX}{name}.csv")


def _parse_specs(raw) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


@dataclass
class StoreData:
    categories: pd.DataFrame
    products: pd.DataFrame
    promotions: pd.DataFrame
    customers: pd.DataFrame
    orders: pd.DataFrame
    order_items: pd.DataFrame

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> "StoreData":
        categories = _read("categories", data_dir)
        products = _read("products", data_dir)
        promotions = _read("promotions", data_dir)
        customers = _read("customers", data_dir)
        orders = _read("orders", data_dir)
        order_items = _read("order_items", data_dir)

        # --- categories -------------------------------------------------------------
        categories["name"] = categories["name"].str.strip()

        # --- promotions: only active ones matter; if a product had several active
        # promotions we keep the biggest discount (policy: promotions don't stack).
        promotions["is_active"] = promotions["is_active"].astype(int).astype(bool)
        active = (
            promotions[promotions["is_active"]]
            .sort_values("discount_percent", ascending=False)
            .drop_duplicates("product_id")
            .rename(columns={"description": "promo_name", "discount_percent": "promo_discount"})
        )[["product_id", "promo_discount", "promo_name"]]

        # --- products ----------------------------------------------------------------
        products["name"] = products["name"].str.strip()
        products["description"] = products["description"].fillna("").str.strip()
        products["specs"] = products["specs"].map(_parse_specs)
        products["stock_quantity"] = products["stock_quantity"].fillna(0).astype(int)
        products = products.merge(
            categories[["category_id", "name"]].rename(columns={"name": "category"}),
            on="category_id",
            how="left",
        )
        products = products.merge(active, on="product_id", how="left")
        products["promo_discount"] = products["promo_discount"].astype("Float64")
        products["final_price"] = [
            pricing.promo_price(p, d if pd.notna(d) else None) or p
            for p, d in zip(products["price_brl"], products["promo_discount"])
        ]
        # A product is sellable only if it is active AND has stock.
        products["available"] = (products["status"] == "active") & (products["stock_quantity"] > 0)
        products["search_text"] = (
            products["name"]
            + " "
            + products["category"].fillna("")
            + " "
            + products["description"]
            + " "
            + products["specs"].map(lambda s: " ".join(str(v) for v in s.values()))
        ).map(normalize)
        products = products.sort_values("product_id").reset_index(drop=True)

        # --- customers ---------------------------------------------------------------
        customers["email"] = customers["email"].str.strip().str.lower()
        customers["phone_digits"] = customers["phone"].map(only_digits)

        # --- orders ------------------------------------------------------------------
        orders["order_date"] = pd.to_datetime(orders["order_date"]).dt.date
        orders["estimated_delivery"] = pd.to_datetime(orders["estimated_delivery"]).dt.date
        orders["tracking_code"] = orders["tracking_code"].where(orders["tracking_code"].notna(), None)
        orders["notes"] = orders["notes"].where(orders["notes"].notna(), None)

        return cls(categories, products, promotions, customers, orders, order_items)

    # ------------------------------------------------------------------ lookups
    def category_names(self) -> list[str]:
        return self.categories["name"].tolist()

    def product(self, product_id: int) -> pd.Series | None:
        rows = self.products[self.products["product_id"] == product_id]
        return None if rows.empty else rows.iloc[0]

    def order(self, order_id: int) -> pd.Series | None:
        rows = self.orders[self.orders["order_id"] == order_id]
        return None if rows.empty else rows.iloc[0]

    def customer(self, customer_id: int) -> pd.Series | None:
        rows = self.customers[self.customers["customer_id"] == customer_id]
        return None if rows.empty else rows.iloc[0]

    def items_of(self, order_id: int) -> pd.DataFrame:
        items = self.order_items[self.order_items["order_id"] == order_id]
        return items.merge(
            self.products[["product_id", "name", "price_brl", "category"]], on="product_id", how="left"
        )


@lru_cache(maxsize=1)
def get_store() -> StoreData:
    return StoreData.load()
