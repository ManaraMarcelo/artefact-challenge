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

MODEL = os.getenv("EMPORIO_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("EMPORIO_EFFORT", "low")


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
