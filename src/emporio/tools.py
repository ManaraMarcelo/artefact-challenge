"""Tools exposed to the LLM (function calling).

Design principles:
- Tools return *facts already computed* (final price, PIX price, installment plan,
  days since delivery, eligibility windows). The LLM's job is to choose the tool,
  interpret the customer's intent and write the answer, not to do arithmetic.
- Every tool returns JSON with Portuguese labels, so the model can quote values
  directly in the customer's language.
- Personal data is only returned after identity verification (LGPD, policy section 9).
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any, Callable

import pandas as pd

from . import pricing
from .config import RUNTIME_DIR, today as default_today
from .data import (
    ORDER_STATUS_PT,
    PRODUCT_STATUS_PT,
    SPEC_LABELS_PT,
    StoreData,
    describe_payment,
    get_store,
    only_digits,
)
from .guardrails import validate_tool_args
from .policies import search_policies
from .text import normalize, stem, tokenize

LOW_STOCK_THRESHOLD = 2

# Policy 1 / 7.1: the store sells instruments only. Searching for these terms returns a
# scope notice instead of loosely related instruments (e.g. "amplificador" ~ "amplificado").
ACCESSORY_TERMS = frozenset(
    "amplificador amplificadores amp pedal pedais pedaleira palheta palhetas cabo cabos case cases "
    "bag capa capas encordoamento afinador correia estante suporte baqueta baquetas".split()
)
SCOPE_NOTICE = (
    "A Empório da Música trabalha exclusivamente com instrumentos musicais e não vende acessórios "
    "(cordas avulsas, palhetas, cabos, cases, pedais, amplificadores etc.)."
)
REGRET_WINDOW_DAYS = 7  # policy 4.1 / 4.3
DEFECT_EXCHANGE_DAYS = 30  # policy 4.2
LEGAL_WARRANTY_DAYS = 90  # policy 8.1

# --------------------------------------------------------------------------- schemas

def tool_definitions(categories: list[str]) -> list[dict]:
    return [
        {
            "name": "search_products",
            "description": (
                "Busca instrumentos no catálogo da loja (preço, promoção e disponibilidade em tempo real). "
                "Use para qualquer pergunta sobre produtos, modelos, marcas, faixas de preço ou se a loja tem algo. "
                "Pode combinar texto livre com filtros. Retorna no máximo `limit` itens ordenados por relevância."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto livre: marca, modelo ou característica (ex.: 'Takamine GD20', 'eletroacústico nylon', 'Yamaha'). Omitir para listar por categoria/preço.",
                    },
                    "category": {"type": "string", "enum": categories, "description": "Filtra por categoria."},
                    "max_price": {"type": "number", "description": "Preço final máximo em R$ (já considera promoções)."},
                    "min_price": {"type": "number", "description": "Preço final mínimo em R$."},
                    "include_unavailable": {
                        "type": "boolean",
                        "description": "Inclui itens esgotados, descontinuados ou em pré-lançamento. Padrão: false.",
                    },
                    "limit": {"type": "integer", "description": "Máximo de itens (padrão 8, máx. 15)."},
                },
                "required": [],
            },
        },
        {
            "name": "get_product_details",
            "description": (
                "Detalhes completos de UM produto pelo product_id: descrição, especificações, preço de tabela, "
                "promoção ativa, preço no PIX, parcelamento máximo sem juros e disponibilidade. Se o produto "
                "estiver indisponível, já retorna alternativas semelhantes disponíveis."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        },
        {
            "name": "list_active_promotions",
            "description": "Lista todas as promoções vigentes agora, com preço original, desconto e preço promocional.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_order_status",
            "description": (
                "Consulta pedidos de um cliente. Exige verificação de identidade: o e-mail OU telefone cadastrado. "
                "Com order_id retorna aquele pedido (status, itens, rastreio, previsão de entrega e prazos de "
                "troca/devolução já calculados). Sem order_id, lista os pedidos do cliente. "
                "Nunca invente o contato: peça ao cliente se ele não informou."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "integer", "description": "Número do pedido."},
                    "contact": {
                        "type": "string",
                        "description": "E-mail ou telefone do cliente, exatamente como ele informou.",
                    },
                },
                "required": ["contact"],
            },
        },
        {
            "name": "search_store_policies",
            "description": (
                "Busca no manual oficial de políticas da loja. Use para: endereço, contato, horários, formas de "
                "pagamento e parcelamento, trocas, devoluções, arrependimento, defeito, garantia, frete, prazos de "
                "entrega, rastreamento, regras de promoção, privacidade/LGPD. Escreva a consulta com palavras-chave "
                "(ex.: 'devolução arrependimento', 'frete outras cidades')."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "escalate_to_human",
            "description": (
                "Registra um atendimento para a equipe humana e gera um protocolo. Use para: reclamações, "
                "solicitações de devolução/troca/reembolso que precisam ser efetivadas, acionamento de garantia, "
                "dúvidas que as políticas não cobrem, ou quando o cliente pedir para falar com uma pessoa. "
                "Antes de chamar, confirme com o cliente e tenha um contato dele."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["reclamacao", "devolucao_troca", "garantia", "duvida_nao_coberta", "pedido_do_cliente", "outro"],
                    },
                    "summary": {"type": "string", "description": "Resumo objetivo do caso para a equipe."},
                    "customer_name": {"type": "string"},
                    "contact": {"type": "string", "description": "Telefone ou e-mail para retorno."},
                    "order_id": {"type": "integer"},
                },
                "required": ["reason", "summary", "contact"],
            },
        },
    ]


# --------------------------------------------------------------------------- helpers

def _brl(value: float) -> str:
    s = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _availability(row: pd.Series) -> str:
    if row["status"] == "discontinued":
        return "descontinuado (não faz mais parte do catálogo)"
    if row["status"] == "coming_soon":
        return "em breve (ainda não disponível para venda)"
    if row["stock_quantity"] <= 0:
        return "esgotado no momento"
    if row["stock_quantity"] <= LOW_STOCK_THRESHOLD:
        return "disponível (últimas unidades)"
    return "disponível"


def _price_block(row: pd.Series) -> dict:
    has_promo = pd.notna(row["promo_discount"])
    block: dict[str, Any] = {"preco_tabela": _brl(row["price_brl"])}
    if has_promo:
        block["promocao"] = {
            "nome": row["promo_name"],
            "desconto_percentual": int(row["promo_discount"]),
            "preco_promocional": _brl(row["final_price"]),
        }
    block["preco_final"] = _brl(row["final_price"])
    return block


def _summary(row: pd.Series) -> dict:
    return {
        "product_id": int(row["product_id"]),
        "nome": row["name"],
        "categoria": row["category"],
        **_price_block(row),
        "disponibilidade": _availability(row),
    }


def _stems(text: str) -> set[str]:
    return {stem(t) for t in tokenize(text)}


# --------------------------------------------------------------------------- toolbox

class Toolbox:
    """Executes tool calls against the store data. `today` is injectable for tests/demos."""

    def __init__(self, store: StoreData | None = None, today: Callable[[], date] = default_today):
        self.store = store or get_store()
        self.today = today
        self._product_stems = {
            int(r.product_id): _stems(r.search_text) for r in self.store.products.itertuples()
        }
        self._handlers: dict[str, Callable[..., dict]] = {
            "search_products": self.search_products,
            "get_product_details": self.get_product_details,
            "list_active_promotions": self.list_active_promotions,
            "get_order_status": self.get_order_status,
            "search_store_policies": self.search_store_policies,
            "escalate_to_human": self.escalate_to_human,
        }

    @property
    def definitions(self) -> list[dict]:
        return tool_definitions(self.store.category_names())

    def execute(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a tool; returns (json_result, is_error). Never raises to the agent loop."""
        handler = self._handlers.get(name)
        if handler is None:
            return json.dumps({"erro": f"Ferramenta desconhecida: {name}"}), True
        # Never trust model-produced arguments: validate against the tool schema first.
        schema = next(d["input_schema"] for d in self.definitions if d["name"] == name)
        args, problems = validate_tool_args(schema, args or {})
        if problems:
            return json.dumps({"erro": "Argumentos inválidos: " + "; ".join(problems)}, ensure_ascii=False), True
        try:
            result = handler(**args)
            return json.dumps(result, ensure_ascii=False, default=str), False
        except TypeError as exc:  # bad/missing arguments from the model
            return json.dumps({"erro": f"Argumentos inválidos: {exc}"}, ensure_ascii=False), True
        except Exception as exc:  # pragma: no cover - defensive
            return json.dumps({"erro": f"Falha ao executar {name}: {exc}"}, ensure_ascii=False), True

    # ------------------------------------------------------------- products
    def _relevance(self, product_id: int, name: str, query: str) -> float:
        q_stems = _stems(query)
        if not q_stems:
            return 1.0
        p_stems = self._product_stems[product_id]
        hits = 0.0
        for q in q_stems:
            if q in p_stems:
                hits += 1
            elif any(len(q) >= 4 and (q in p or SequenceMatcher(None, q, p).ratio() >= 0.85) for p in p_stems):
                hits += 0.8
        score = hits / len(q_stems)
        # Model numbers written with/without spaces or dashes: "GD 20", "gd-20" -> "gd20".
        compact_q = "".join(tokenize(query, drop_stopwords=False))
        compact_name = "".join(tokenize(name, drop_stopwords=False))
        if len(compact_q) >= 4 and compact_q in compact_name:
            score = max(score, 1.0)
        return score

    def search_products(
        self,
        query: str | None = None,
        category: str | None = None,
        max_price: float | None = None,
        min_price: float | None = None,
        include_unavailable: bool = False,
        limit: int = 8,
    ) -> dict:
        scope_notice = None
        if query:
            tokens = tokenize(query)
            accessories = [t for t in tokens if t in ACCESSORY_TERMS]
            if accessories:
                scope_notice = SCOPE_NOTICE
                query = " ".join(t for t in tokens if t not in ACCESSORY_TERMS)
                if not query and not category:
                    return {"total_encontrado": 0, "itens": [], "aviso_escopo": scope_notice}

        df = self.store.products
        if category:
            df = df[df["category"].map(normalize) == normalize(category)]
        if max_price is not None:
            df = df[df["final_price"] <= max_price]
        if min_price is not None:
            df = df[df["final_price"] >= min_price]
        if query:
            scores = [self._relevance(int(r.product_id), r.name, query) for r in df.itertuples()]
            df = df.assign(_score=scores)
            # Keep matches close to the best one: asking for "GF-3D" must not return "GF-1R"
            # (same brand, different model) just because two of three words match.
            best = df["_score"].max() if not df.empty else 0
            df = df[df["_score"] >= max(0.6, best - 0.3)].sort_values(["_score", "final_price"], ascending=[False, True])
        else:
            df = df.sort_values("final_price")

        only_unavailable = False
        if not include_unavailable:
            available = df[df["available"]]
            # A customer asking for a specific model that is out of stock should hear that
            # explicitly (policy 7.3), so fall back to showing the unavailable match.
            only_unavailable = bool(query) and available.empty and not df.empty
            if not only_unavailable:
                df = available

        limit = max(1, min(int(limit or 8), 15))
        result: dict[str, Any] = {
            "total_encontrado": int(len(df)),
            "itens": [_summary(r) for _, r in df.head(limit).iterrows()],
        }
        if only_unavailable:
            result["observacao"] = (
                "Só há correspondência com itens indisponíveis. Informe o status e ofereça alternativas "
                "(get_product_details traz alternativas disponíveis)."
            )
        if len(df) > limit:
            result["observacao"] = f"Mostrando {limit} de {len(df)}. Refine a busca se necessário."
        if df.empty:
            result["observacao"] = (
                "Nenhum produto encontrado com esses critérios. Considere relaxar filtros "
                "(ex.: incluir indisponíveis, outra categoria ou faixa de preço). Lembre: a loja vende apenas "
                "instrumentos musicais, não acessórios."
            )
        if scope_notice:
            result["aviso_escopo"] = scope_notice
        return result

    def _alternatives(self, row: pd.Series, n: int = 3) -> list[dict]:
        df = self.store.products
        df = df[(df["category_id"] == row["category_id"]) & df["available"] & (df["product_id"] != row["product_id"])]
        df = df.assign(_dist=(df["final_price"] - row["final_price"]).abs()).sort_values("_dist")
        return [_summary(r) for _, r in df.head(n).iterrows()]

    def get_product_details(self, product_id: int) -> dict:
        row = self.store.product(int(product_id))
        if row is None:
            return {"erro": f"Produto {product_id} não encontrado. Use search_products para localizar o id."}

        has_promo = pd.notna(row["promo_discount"])
        details: dict[str, Any] = {
            **_summary(row),
            "descricao": row["description"],
            "especificacoes": {SPEC_LABELS_PT.get(k, k): v for k, v in row["specs"].items()},
            "status_cadastro": PRODUCT_STATUS_PT.get(row["status"], row["status"]),
        }
        pix = pricing.pix_price(row["price_brl"], has_promo)
        details["pagamento"] = {
            "pix": (
                f"{_brl(pix)} (5% de desconto à vista)"
                if pix is not None
                else "preço promocional já aplicado; desconto do PIX não é cumulativo com promoções"
            ),
            "cartao_credito": (
                lambda plan: f"até {plan['max_installments']}x sem juros de {_brl(plan['installment_value'])}"
            )(pricing.installment_plan(row["final_price"])),
            "debito_e_boleto": f"{_brl(row['final_price'])} à vista",
        }
        if row["final_price"] > pricing.MIXED_PAYMENT_THRESHOLD:
            details["pagamento"]["combinacao_formas"] = "permitido combinar formas de pagamento (ex.: PIX + cartão)"
        details["frete_campo_grande"] = (
            "grátis (pedido acima de R$ 500,00)"
            if row["final_price"] > pricing.FREE_SHIPPING_THRESHOLD
            else f"taxa fixa de {_brl(pricing.LOCAL_SHIPPING_FEE)}"
        )
        if not row["available"]:
            details["alternativas_disponiveis"] = self._alternatives(row)
        return details

    def list_active_promotions(self) -> dict:
        df = self.store.products
        df = df[df["promo_discount"].notna()].sort_values("promo_discount", ascending=False)
        return {
            "promocoes_ativas": [_summary(r) for _, r in df.iterrows()],
            "regras": "Promoções não são cumulativas e o desconto do PIX não se aplica sobre preço promocional.",
        }

    # ------------------------------------------------------------- orders
    def _match_customers(self, contact: str) -> pd.DataFrame:
        c = self.store.customers
        contact = (contact or "").strip()
        if "@" in contact:
            return c[c["email"] == contact.lower()]
        digits = only_digits(contact)
        if len(digits) < 8:
            return c.iloc[0:0]
        # Compare the last 8 digits so "(67) 99812-3456", "67998123456" and "+55 67 99812-3456" all match.
        return c[c["phone_digits"].str[-8:] == digits[-8:]]

    def _order_view(self, order: pd.Series) -> dict:
        today = self.today()
        items = self.store.items_of(int(order["order_id"]))
        view: dict[str, Any] = {
            "order_id": int(order["order_id"]),
            "data_pedido": order["order_date"].isoformat(),
            "status": ORDER_STATUS_PT.get(order["status"], order["status"]),
            "itens": [
                {"product_id": int(i.product_id), "nome": i.name, "quantidade": int(i.quantity)}
                for i in items.itertuples()
            ],
            "total": _brl(float(order["total_brl"])),
            "forma_pagamento": describe_payment(order["payment_method"]),
        }
        if order["tracking_code"]:
            view["codigo_rastreio"] = order["tracking_code"]
        if pd.notna(order["estimated_delivery"]):
            view["previsao_entrega"] = order["estimated_delivery"].isoformat()
        if order["notes"]:
            view["observacoes"] = order["notes"]

        if order["status"] == "shipped" and pd.notna(order["estimated_delivery"]) and order["estimated_delivery"] < today:
            view["alerta"] = (
                f"Previsão de entrega ({order['estimated_delivery'].isoformat()}) já passou e o pedido "
                "ainda consta como enviado. Ofereça abrir um chamado com a equipe."
            )

        if order["status"] == "delivered" and pd.notna(order["estimated_delivery"]):
            # Assumption (documented): the data has no actual delivery date, so we use the
            # estimated delivery date as the delivery date.
            delivered = order["estimated_delivery"]
            days = (today - delivered).days
            view["prazos_pos_venda"] = {
                "data_entrega_considerada": delivered.isoformat(),
                "dias_desde_entrega": days,
                "arrependimento_ou_troca_por_preferencia_7_dias": days <= REGRET_WINDOW_DAYS,
                "troca_por_defeito_na_loja_30_dias": days <= DEFECT_EXCHANGE_DAYS,
                "garantia_legal_90_dias": days <= LEGAL_WARRANTY_DAYS,
                "nota": "Data de entrega real não disponível no sistema; considerada a data prevista.",
            }
        return view

    def get_order_status(self, contact: str, order_id: int | None = None) -> dict:
        customers = self._match_customers(contact)
        fail = {
            "verificado": False,
            "erro": "Não foi possível verificar a identidade com esse contato. Peça ao cliente para confirmar "
            "o e-mail ou telefone usado na compra. Por segurança, não revele se o pedido existe.",
        }
        if customers.empty:
            return fail

        orders = self.store.orders[self.store.orders["customer_id"].isin(customers["customer_id"])]
        if order_id is not None:
            match = orders[orders["order_id"] == int(order_id)]
            if match.empty:
                return fail
            order = match.iloc[0]
            customer = self.store.customer(int(order["customer_id"]))
            return {"verificado": True, "cliente": customer["name"].split()[0], **self._order_view(order)}

        if len(customers) > 1:
            return {
                "verificado": False,
                "erro": "Este contato está associado a mais de um cadastro. Peça o número do pedido ou o telefone.",
            }
        customer = customers.iloc[0]
        if orders.empty:
            return {"verificado": True, "cliente": customer["name"].split()[0], "pedidos": []}
        return {
            "verificado": True,
            "cliente": customer["name"].split()[0],
            "pedidos": [self._order_view(o) for _, o in orders.sort_values("order_date", ascending=False).iterrows()],
        }

    # ------------------------------------------------------------- policies
    def search_store_policies(self, query: str) -> dict:
        chunks = search_policies(query, top_k=3)
        if not chunks:
            return {
                "trechos": [],
                "observacao": "Nada encontrado no manual. Não invente: diga que vai confirmar com a equipe "
                "(escalate_to_human) ou tente outras palavras-chave.",
            }
        return {"trechos": [c.render() for c in chunks]}

    # ------------------------------------------------------------- handoff
    def escalate_to_human(
        self,
        reason: str,
        summary: str,
        contact: str,
        customer_name: str | None = None,
        order_id: int | None = None,
    ) -> dict:
        protocol = f"EM-{self.today().strftime('%Y%m%d')}-{secrets.token_hex(2).upper()}"
        ticket = {
            "protocolo": protocol,
            "criado_em": datetime.now().isoformat(timespec="seconds"),
            "motivo": reason,
            "resumo": summary,
            "cliente": customer_name,
            "contato": contact,
            "order_id": order_id,
        }
        RUNTIME_DIR.mkdir(exist_ok=True)
        with open(RUNTIME_DIR / "tickets.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ticket, ensure_ascii=False) + "\n")
        return {
            "protocolo": protocol,
            "prazo_retorno": "até 24 horas úteis",
            "mensagem": "Chamado registrado e encaminhado à equipe responsável.",
        }
