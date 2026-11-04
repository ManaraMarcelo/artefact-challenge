import json


def run(toolbox, name, **args):
    output, is_error = toolbox.execute(name, args)
    return json.loads(output), is_error


# ------------------------------------------------------------------ products

def test_search_by_model_name_tolerates_formatting(toolbox):
    for query in ("Takamine GD20", "takamine gd-20", "TAKAMINE GD 20"):
        result, _ = run(toolbox, "search_products", query=query)
        assert [i["product_id"] for i in result["itens"]] == [95], query


def test_search_exact_model_beats_same_brand(toolbox):
    result, _ = run(toolbox, "search_products", query="Giannini GF-3D")
    assert [i["product_id"] for i in result["itens"]] == [96]
    assert result["itens"][0]["disponibilidade"] == "esgotado no momento"


def test_search_by_category_and_price(toolbox):
    result, _ = run(toolbox, "search_products", category="Violões", max_price=1000, limit=15)
    prices = [float(i["preco_final"].replace("R$ ", "").replace(".", "").replace(",", ".")) for i in result["itens"]]
    assert result["total_encontrado"] == 12
    assert all(p <= 1000 for p in prices)
    assert prices == sorted(prices)
    assert all(i["disponibilidade"].startswith("disponível") for i in result["itens"])


def test_search_for_accessory_returns_scope_notice(toolbox):
    result, _ = run(toolbox, "search_products", query="amplificador")
    assert result["itens"] == []
    assert "exclusivamente com instrumentos" in result["aviso_escopo"]


def test_details_promo_product_has_no_pix_discount(toolbox):
    result, _ = run(toolbox, "get_product_details", product_id=90)
    assert result["promocao"]["desconto_percentual"] == 18
    assert result["preco_final"] == "R$ 1.967,18"
    assert "não é cumulativo" in result["pagamento"]["pix"]


def test_details_regular_product(toolbox):
    result, _ = run(toolbox, "get_product_details", product_id=95)
    assert result["pagamento"]["pix"].startswith("R$ 2.089,05")
    assert result["pagamento"]["cartao_credito"] == "até 12x sem juros de R$ 183,25"
    assert "alternativas_disponiveis" not in result


def test_details_out_of_stock_suggests_available_alternatives(toolbox):
    result, _ = run(toolbox, "get_product_details", product_id=96)
    alternatives = result["alternativas_disponiveis"]
    assert len(alternatives) == 3
    assert all(a["disponibilidade"].startswith("disponível") for a in alternatives)


def test_unknown_product(toolbox):
    result, _ = run(toolbox, "get_product_details", product_id=9999)
    assert "não encontrado" in result["erro"]


def test_active_promotions(toolbox):
    result, _ = run(toolbox, "list_active_promotions")
    assert len(result["promocoes_ativas"]) == 4


# ------------------------------------------------------------------ orders

def test_order_requires_matching_contact(toolbox):
    result, _ = run(toolbox, "get_order_status", order_id=8, contact="someone@else.com")
    assert result["verificado"] is False
    assert "itens" not in result


def test_order_of_another_customer_is_not_revealed(toolbox):
    # Valid e-mail (customer 2), but order 7 belongs to customer 14.
    result, _ = run(toolbox, "get_order_status", order_id=7, contact="anacarol.ferreira@coldmail.com")
    assert result["verificado"] is False


def test_order_by_email(toolbox):
    result, _ = run(toolbox, "get_order_status", order_id=8, contact="AnaCarol.Ferreira@coldmail.com")
    assert result["verificado"] is True
    assert result["status"] == "enviado"
    assert result["codigo_rastreio"] == "BRJL5544332BR"
    assert "alerta" in result  # estimated delivery (2026-02-28) is before the snapshot date


def test_order_by_phone_in_any_format(toolbox):
    for phone in ("(67) 99789-0123", "67997890123", "+55 67 99789-0123"):
        result, _ = run(toolbox, "get_order_status", order_id=7, contact=phone)
        assert result["verificado"] is True, phone


def test_delivered_order_has_post_sale_windows(toolbox):
    result, _ = run(toolbox, "get_order_status", order_id=7, contact="(67) 99789-0123")
    windows = result["prazos_pos_venda"]
    assert windows["dias_desde_entrega"] == 36
    assert windows["arrependimento_ou_troca_por_preferencia_7_dias"] is False
    assert windows["troca_por_defeito_na_loja_30_dias"] is False
    assert windows["garantia_legal_90_dias"] is True


def test_list_orders_without_id(toolbox):
    result, _ = run(toolbox, "get_order_status", contact="(67) 98432-1098")  # customer 3
    assert result["verificado"] is True
    assert {o["order_id"] for o in result["pedidos"]} == {1, 19}


def test_shared_email_requires_more_info(toolbox):
    result, _ = run(toolbox, "get_order_status", contact="bruno.martins@jmail.com")
    assert result["verificado"] is False


# ------------------------------------------------------------------ policies & handoff

def test_policy_search(toolbox):
    result, _ = run(toolbox, "search_store_policies", query="devolução arrependimento")
    assert result["trechos"][0].startswith("[Seção 4.1")


def test_escalation_creates_ticket(toolbox, tmp_path):
    result, _ = run(
        toolbox, "escalate_to_human", reason="reclamacao", summary="Pedido atrasado", contact="(67) 99654-7821", order_id=8
    )
    assert result["protocolo"].startswith("EM-20260325-")
    ticket = json.loads((tmp_path / "tickets.jsonl").read_text(encoding="utf-8").strip())
    assert ticket["order_id"] == 8


def test_bad_arguments_are_reported_as_tool_errors(toolbox):
    _, is_error = run(toolbox, "get_product_details")
    assert is_error
    _, is_error = run(toolbox, "does_not_exist")
    assert is_error
