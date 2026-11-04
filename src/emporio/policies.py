"""Store policy manual: PDF extraction, section chunking and lexical retrieval (BM25).

Why not embeddings + vector DB? The manual is ~2.5k tokens split into ~25 well-titled
sections, written in the same vocabulary customers use ("troca", "frete", "parcelamento").
A section-aware BM25 with accent folding and light stemming is deterministic, needs no
extra service or API key, is trivially debuggable, and retrieves the right section for
every question in our test set. The retrieval interface (`search`) is isolated so it can
be swapped for hybrid/embedding search if the document base grows.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

from .config import POLICIES_PDF
from .text import stem, tokenize

# Known inconsistencies inside the manual itself, fixed at ingestion time so the model
# never sees two conflicting values. Each one is documented in the README.
ERRATA = {
    # Section 1.2 has an accented domain, which is not a valid e-mail address; the cover
    # page uses the unaccented form.
    "contato@empóriodamusica.com.br": "contato@emporiodamusica.com.br",
    # Section 7 lists a different WhatsApp number from the cover page and section 1.2
    # ("Telefone / WhatsApp"). We keep the number that appears twice and flag it.
    "(67) 3321-4500": "(67) 3341-4444",
}

_SECTION_RE = re.compile(r"^(\d{1,2})\.\s+([^:]{3,70})$")
_SUBSECTION_RE = re.compile(r"^(\d{1,2}\.\d{1,2})\s+([^:]{3,70})$")
_NOISE_RE = re.compile(r"^(Empório da Música Manual de Políticas e Procedimentos|Página \d+)$")


@dataclass(frozen=True)
class PolicyChunk:
    section_id: str  # e.g. "4.1"
    title: str  # e.g. "Política de Trocas e Devoluções > Direito de Arrependimento (Compras Online)"
    text: str

    def render(self) -> str:
        return f"[Seção {self.section_id} — {self.title}]\n{self.text}"


def _clean_lines(raw: str) -> list[str]:
    lines = []
    for line in raw.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line or _NOISE_RE.match(line):
            continue
        for wrong, right in ERRATA.items():
            line = line.replace(wrong, right)
        lines.append(line)
    return lines


def extract_chunks(pdf_path: Path = POLICIES_PDF) -> list[PolicyChunk]:
    reader = PdfReader(str(pdf_path))
    # Page 1 is the cover (its contact data is repeated in section 1.2).
    lines: list[str] = []
    for page in reader.pages[1:]:
        lines.extend(_clean_lines(page.extract_text(extraction_mode="layout")))

    chunks: list[PolicyChunk] = []
    section_title = ""
    current_id: str | None = None
    current_title = ""
    buffer: list[str] = []

    def flush():
        body = "\n".join(buffer).strip()
        if current_id and body:
            chunks.append(PolicyChunk(current_id, current_title, body))

    for line in lines:
        sub = _SUBSECTION_RE.match(line)
        sec = None if sub else _SECTION_RE.match(line)
        if sec:
            flush()
            current_id, section_title = sec.group(1), sec.group(2).strip()
            current_title, buffer = section_title, []
        elif sub:
            flush()
            current_id = sub.group(1)
            current_title, buffer = f"{section_title} > {sub.group(2).strip()}", []
        else:
            buffer.append(line)
    flush()
    return chunks


class BM25Index:
    """Minimal Okapi BM25 over stemmed, accent-folded tokens."""

    def __init__(self, chunks: list[PolicyChunk], k1: float = 1.5, b: float = 0.75, title_boost: int = 3):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [
            [stem(t) for t in tokenize(f"{(c.title + ' ') * title_boost} {c.text}")] for c in chunks
        ]
        self.tf = [Counter(d) for d in self.docs]
        self.avgdl = sum(len(d) for d in self.docs) / max(len(self.docs), 1)
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, top_k: int = 3) -> list[tuple[PolicyChunk, float]]:
        terms = [stem(t) for t in tokenize(query)]
        scored = []
        for chunk, tf, doc in zip(self.chunks, self.tf, self.docs):
            score = 0.0
            for t in terms:
                if t not in tf:
                    continue
                f = tf[t]
                score += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(doc) / self.avgdl))
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


@lru_cache(maxsize=1)
def get_index() -> BM25Index:
    return BM25Index(extract_chunks())


def search_policies(query: str, top_k: int = 3) -> list[PolicyChunk]:
    return [chunk for chunk, _ in get_index().search(query, top_k)]


def all_section_titles() -> list[str]:
    return [f"{c.section_id} {c.title}" for c in get_index().chunks]


# --------------------------------------------------------------------------- opening hours
# Policy section 2, encoded as data so "are you open now?" is computed, not guessed.
# weekday(): Monday=0 ... Sunday=6
OPENING_HOURS = {
    0: (time(9), time(18)),
    1: (time(9), time(18)),
    2: (time(9), time(18)),
    3: (time(9), time(18)),
    4: (time(9), time(18)),
    5: (time(9), time(13)),
    6: None,
}
WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def store_status(now: datetime) -> str:
    """Human-readable open/closed status. Holidays are not modelled (no calendar in the data)."""
    hours = OPENING_HOURS[now.weekday()]
    if hours and hours[0] <= now.time() < hours[1]:
        return f"ABERTA agora (hoje fecha às {hours[1].strftime('%H:%M')})"

    # Find the next opening.
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        h = OPENING_HOURS[day.weekday()]
        if not h:
            continue
        if offset == 0 and now.time() >= h[0]:
            continue
        when = "hoje" if offset == 0 else ("amanhã" if offset == 1 else WEEKDAYS_PT[day.weekday()])
        return f"FECHADA agora (reabre {when} às {h[0].strftime('%H:%M')})"
    return "FECHADA agora"
