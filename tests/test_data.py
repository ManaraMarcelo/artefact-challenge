from emporio.data import describe_payment
from emporio.data_quality import run_checks


def test_tables_load(store):
    assert len(store.products) == 65
    assert len(store.orders) == 20
    assert store.category_names()[0] == "Guitarras"


def test_only_active_promotions_are_applied(store):
    promo = store.products[store.products["promo_discount"].notna()]
    assert sorted(promo["product_id"]) == [90, 94, 121, 127]
    crafter = store.product(90)
    assert crafter["final_price"] == 1967.18


def test_availability_requires_active_status_and_stock(store):
    assert not store.product(96)["available"]  # active, 0 in stock
    assert not store.product(136)["available"]  # discontinued with stock
    assert not store.product(130)["available"]  # coming soon with stock
    assert store.product(95)["available"]


def test_missing_values_are_none_not_nan(store):
    order = store.order(9)
    assert order["tracking_code"] is None
    assert order["notes"] is None


def test_payment_labels():
    assert describe_payment("credit_12x") == "cartão de crédito em 12x"
    assert describe_payment("pix") == "PIX"


def test_data_quality_checks_find_known_issues(store):
    findings = "\n".join(run_checks(store))
    assert "Pedido 3: total_brl" in findings
    assert "Categoria 8 'Cordas Orquestrais'" in findings
