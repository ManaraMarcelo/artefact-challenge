from datetime import datetime

import pytest

from emporio.config import STORE_TZ
from emporio.policies import get_index, search_policies, store_status


def test_manual_is_split_into_sections():
    ids = [c.section_id for c in get_index().chunks]
    for expected in ("1.2", "2", "3.1", "4.1", "4.2", "5.2", "6.2", "7.3", "8.1", "9"):
        assert expected in ids


def test_errata_fix_contact_inconsistencies():
    text = "\n".join(c.text for c in get_index().chunks)
    assert "contato@emporiodamusica.com.br" in text
    assert "empóriodamusica" not in text
    assert "3321-4500" not in text


@pytest.mark.parametrize(
    "query, section",
    [
        ("me arrependi da compra, posso devolver?", "4.1"),
        ("qual o endereço da loja", "1.2"),
        ("horário de funcionamento", "2"),
        ("posso parcelar em 10x", "3.1"),
        ("meu violão veio com defeito", "4.2"),
        ("quero trocar a cor", "4.3"),
        ("prazo de entrega sedex", "5.2"),
        ("código de rastreamento", "5.3"),
        ("quero fazer uma reclamação", "7.3"),
        ("LGPD meus dados", "9"),
    ],
)
def test_retrieval_top1(query, section):
    assert search_policies(query, top_k=1)[0].section_id == section


@pytest.mark.parametrize(
    "when, expected",
    [
        (datetime(2026, 11, 4, 10, 0), "ABERTA agora (hoje fecha às 18:00)"),  # Wednesday
        (datetime(2026, 11, 4, 19, 30), "FECHADA agora (reabre amanhã às 09:00)"),
        (datetime(2026, 11, 7, 12, 0), "ABERTA agora (hoje fecha às 13:00)"),  # Saturday
        (datetime(2026, 11, 7, 14, 0), "FECHADA agora (reabre segunda-feira às 09:00)"),
        (datetime(2026, 11, 5, 8, 0), "FECHADA agora (reabre hoje às 09:00)"),
    ],
)
def test_store_status(when, expected):
    assert store_status(when.replace(tzinfo=STORE_TZ)) == expected
