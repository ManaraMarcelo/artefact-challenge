import pytest

from emporio import pricing


def test_pix_discount_is_5_percent():
    assert pricing.pix_price(2199.0, has_active_promo=False) == 2089.05


def test_pix_discount_does_not_stack_with_promotion():
    assert pricing.pix_price(2399.0, has_active_promo=True) is None


def test_promo_price():
    assert pricing.promo_price(2399.0, 18) == 1967.18
    assert pricing.promo_price(2399.0, None) is None


@pytest.mark.parametrize(
    "total, expected",
    [
        (40.0, 1),  # a 2x installment would be below R$ 50
        (100.0, 2),  # 2x R$ 50,00 is the limit of the first tier
        (159.9, 3),
        (319.99, 3),  # 4x would be R$ 79,99 (< R$ 80)
        (320.0, 4),
        (429.9, 5),
        (699.0, 6),  # 7x would be R$ 99,86 (< R$ 100)
        (700.0, 7),
        (1200.0, 12),
        (11499.0, 12),
    ],
)
def test_installment_tiers(total, expected):
    assert pricing.max_interest_free_installments(total) == expected


def test_installment_plan_value():
    assert pricing.installment_plan(2199.0) == {"max_installments": 12, "installment_value": 183.25}
