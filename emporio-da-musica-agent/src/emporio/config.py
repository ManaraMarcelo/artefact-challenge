"""Central configuration: paths, timezone, model settings and the notion of "today"."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "data"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
POLICIES_PDF = DATA_DIR / "políticas_da_loja.pdf"

# The store is in Campo Grande/MS, which uses UTC-4 all year round.
STORE_TZ = ZoneInfo("America/Campo_Grande")

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",  # available on the Google AI Studio free tier
    "anthropic": "claude-opus-5-5",
}
EFFORT = os.getenv("EMPORIO_EFFORT", "low")  # Anthropic only


def gemini_api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def provider() -> str:
    """EMPORIO_PROVIDER wins; otherwise pick whichever provider has a key (Gemini first)."""
    explicit = os.getenv("EMPORIO_PROVIDER")
    if explicit:
        return explicit.strip().lower()
    if gemini_api_key():
        return "gemini"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "gemini"


def default_model(provider_name: str) -> str:
    return os.getenv("EMPORIO_MODEL") or DEFAULT_MODELS[provider_name]


def now() -> datetime:
    """Current time in the store's timezone.

    `EMPORIO_TODAY=YYYY-MM-DD` freezes the date (keeping the real time of day), which keeps
    demos and tests reproducible against the static order data.
    """
    real = datetime.now(STORE_TZ)
    frozen = os.getenv("EMPORIO_TODAY")
    if frozen:
        d = date.fromisoformat(frozen)
        return real.replace(year=d.year, month=d.month, day=d.day)
    return real


def today() -> date:
    return now().date()
