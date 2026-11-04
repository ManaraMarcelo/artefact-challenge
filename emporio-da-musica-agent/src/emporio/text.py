"""Text helpers shared by product search and policy retrieval."""

from __future__ import annotations

import re
import unicodedata

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Portuguese function words that carry no meaning for search.
STOPWORDS = frozenset(
    "a o as os de da do das dos e em no na nos nas um uma uns umas para por com sem que "
    "se ao aos meu minha seu sua qual quais como e eh ser ter tem voces voce vcs vc me "
    "mais menos muito ja ou isso esse essa este esta loja".split()
)


def normalize(text: str) -> str:
    """Lowercase and strip accents: 'Violões' -> 'violoes'."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def tokenize(text: str, drop_stopwords: bool = True) -> list[str]:
    tokens = _TOKEN_RE.findall(normalize(text))
    if drop_stopwords:
        tokens = [t for t in tokens if t not in STOPWORDS]
    return tokens


def stem(token: str) -> str:
    """Very light Portuguese stemmer, enough to match singular/plural forms.

    violões/violão -> viol, guitarras -> guitarr, teclados -> teclad.
    """
    for suffix in ("oes", "aes", "ao", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token
