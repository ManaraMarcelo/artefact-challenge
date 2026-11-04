"""Deterministic pricing rules from the store policy manual (sections 3 and 6).

These rules are applied in code rather than left to the LLM: arithmetic and threshold
logic are exactly where language models make silent mistakes, and the manual explicitly
says price information must never be given without consulting the system.
"""

from __future__ import annotations

PIX_DISCOUNT = 0.05
MAX_INSTALLMENTS = 12

# Policy 3.1 installment tiers: (max installments in the tier, minimum installment value).
# The table in section 3 says "minimum R$100" for credit, but 3.1 details the rules per tier;
# we follow the more specific rule (3.1) and document the conflict in the README.
INSTALLMENT_TIERS = (
    (3, 50.0),
    (6, 80.0),
    (12, 100.0),
)

MIXED_PAYMENT_THRESHOLD = 2000.0
FREE_SHIPPING_THRESHOLD = 500.0
LOCAL_SHIPPING_FEE = 35.0


def round_brl(value: float) -> float:
    return round(value + 1e-9, 2)


def promo_price(list_price: float, discount_percent: float | None) -> float | None:
    if not discount_percent:
        return None
    return round_brl(list_price * (1 - discount_percent / 100))


def pix_price(list_price: float, has_active_promo: bool) -> float | None:
    """PIX gives 5% off the list price, but never on top of a promotional price (policy 6.2)."""
    if has_active_promo:
        return None
    return round_brl(list_price * (1 - PIX_DISCOUNT))


def max_interest_free_installments(total: float) -> int:
    """Largest number of interest-free installments allowed for a purchase total."""
    best = 1
    lower = 1
    for tier_max, min_value in INSTALLMENT_TIERS:
        for n in range(lower, tier_max + 1):
            if n == 1 or total / n >= min_value:
                best = n
        lower = tier_max + 1
    return best


def installment_plan(total: float) -> dict:
    n = max_interest_free_installments(total)
    return {"max_installments": n, "installment_value": round_brl(total / n)}
