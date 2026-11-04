"""Consistency checks over the provided data.

Run with `python -m emporio.data_quality`. The findings are documented in the README;
none of them blocks the agent, but they explain some defensive choices in the tools
(e.g. never trusting `is_active` promotions without also checking product status).
"""

from __future__ import annotations

from .data import StoreData, get_store


def run_checks(store: StoreData) -> list[str]:
    findings: list[str] = []
    p = store.products

    # Order totals vs. sum of items at current list price.
    for _, order in store.orders.iterrows():
        items = store.items_of(order["order_id"])
        computed = float((items["quantity"] * items["price_brl"]).sum())
        if abs(computed - float(order["total_brl"])) > 0.01:
            findings.append(
                f"Pedido {order['order_id']}: total_brl={order['total_brl']:.2f} difere da soma dos itens "
                f"pelo preço atual ({computed:.2f}) — possível desconto/preço histórico não registrado."
            )

    # Orders containing products that are not active today.
    for _, order in store.orders.iterrows():
        for _, item in store.items_of(order["order_id"]).iterrows():
            prod = store.product(item["product_id"])
            if prod is not None and prod["status"] != "active":
                findings.append(
                    f"Pedido {order['order_id']} ({order['status']}) contém o produto {prod['product_id']} "
                    f"'{prod['name']}' com status '{prod['status']}'."
                )

    # Status vs stock contradictions.
    for _, prod in p.iterrows():
        if prod["status"] != "active" and prod["stock_quantity"] > 0:
            findings.append(
                f"Produto {prod['product_id']} '{prod['name']}' está '{prod['status']}' mas tem "
                f"{prod['stock_quantity']} unidade(s) em estoque — tratado como indisponível para venda."
            )
        if prod["status"] == "active" and prod["stock_quantity"] == 0:
            findings.append(f"Produto {prod['product_id']} '{prod['name']}' ativo porém sem estoque.")

    # Promotions.
    promos = store.promotions
    dup = promos[promos.duplicated("product_id", keep=False)].sort_values("product_id")
    for product_id, group in dup.groupby("product_id"):
        findings.append(
            f"Produto {product_id} tem {len(group)} promoções cadastradas "
            f"(ativas: {int(group['is_active'].sum())}) — sem datas de vigência, só o flag is_active."
        )

    # Categories without products.
    empty = set(store.categories["category_id"]) - set(p["category_id"])
    for cat_id in sorted(empty):
        name = store.categories.loc[store.categories["category_id"] == cat_id, "name"].iloc[0]
        findings.append(f"Categoria {cat_id} '{name}' não tem nenhum produto cadastrado.")

    # Shipped/delivered orders must have tracking; others must not.
    for _, order in store.orders.iterrows():
        if order["status"] in ("shipped", "delivered") and not order["tracking_code"]:
            findings.append(f"Pedido {order['order_id']} enviado sem código de rastreio.")

    dup_emails = store.customers[store.customers["email"].duplicated(keep=False)]
    if not dup_emails.empty:
        findings.append(
            "E-mails duplicados entre clientes: "
            + ", ".join(f"{r.customer_id}={r.email}" for r in dup_emails.itertuples())
        )
    return findings


if __name__ == "__main__":
    results = run_checks(get_store())
    print(f"{len(results)} achado(s):")
    for line in results:
        print(" -", line)
