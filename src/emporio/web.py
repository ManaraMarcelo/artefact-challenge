"""Web interface: a small FastAPI app serving a single-page chat and a JSON API.

Usage:
    python -m emporio.web                 # http://127.0.0.1:8000
    python -m emporio.web --port 8080

The API is intentionally thin: it maps an HTTP session to an `EmporioAgent` and returns
the reply plus *what the agent consulted* (tool chips in the UI) and guardrail events.
Chat endpoints are sync functions, so FastAPI runs them in its threadpool and a slow
LLM call never blocks the event loop.
"""

from __future__ import annotations

import argparse
import logging
import threading
import uuid
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .agent import EmporioAgent
from .policies import store_status

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("emporio.web")
MAX_SESSIONS = 200

# How each tool is shown to the customer: (label, kind). `kind` drives the chip colour.
TOOL_LABELS = {
    "search_products": ("catálogo", "catalog"),
    "get_product_details": ("detalhes do produto", "catalog"),
    "list_active_promotions": ("promoções", "promo"),
    "get_order_status": ("pedido", "order"),
    "search_store_policies": ("políticas da loja", "policy"),
    "escalate_to_human": ("equipe humana", "human"),
}


class ChatIn(BaseModel):
    message: str = Field(..., max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)


class ToolChip(BaseModel):
    name: str
    label: str
    kind: str
    input: dict
    ok: bool


class ChatOut(BaseModel):
    session_id: str
    reply: str
    tools: list[ToolChip]
    events: list[str]
    error: bool


class _Session:
    def __init__(self, agent: EmporioAgent):
        self.agent = agent
        self.lock = threading.Lock()  # one turn at a time per conversation


def create_app(agent_factory: Callable[[str], EmporioAgent] | None = None) -> FastAPI:
    """`agent_factory(session_id)` is injectable so tests can use a fake backend."""
    factory = agent_factory or (lambda sid: EmporioAgent(session_id=sid))
    sessions: dict[str, _Session] = {}
    sessions_lock = threading.Lock()
    app = FastAPI(title="Empório da Música · Lia", docs_url="/api/docs", redoc_url=None)

    def get_session(session_id: str | None) -> tuple[str, _Session]:
        with sessions_lock:
            sid = session_id if session_id in sessions else None
            if sid is None:
                if len(sessions) >= MAX_SESSIONS:  # simple bound for a demo server
                    sessions.pop(next(iter(sessions)))
                sid = uuid.uuid4().hex[:16]
                sessions[sid] = _Session(factory(sid))
            return sid, sessions[sid]

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> dict:
        now = config.now()
        return {
            "store": store_status(now),
            "open": store_status(now).startswith("ABERTA"),
            "now": now.isoformat(timespec="minutes"),
            "provider": config.provider(),
        }

    @app.post("/api/chat", response_model=ChatOut)
    def chat(body: ChatIn) -> ChatOut:
        try:
            sid, session = get_session(body.session_id)
        except Exception as exc:  # e.g. missing API key when building the backend
            raise HTTPException(status_code=503, detail=f"Agente indisponível: {exc}") from exc
        with session.lock:
            result = session.agent.reply(body.message)
        if result.error:  # surfaced in the server terminal (quota, outage...), never to the customer
            log.warning("Erro do provedor na sessão %s: %s", sid, result.error[:300])
        chips = [
            ToolChip(
                name=call.name,
                label=TOOL_LABELS.get(call.name, (call.name, "other"))[0],
                kind=TOOL_LABELS.get(call.name, (call.name, "other"))[1],
                input=call.input,
                ok=not call.is_error,
            )
            for call in result.tool_calls
        ]
        return ChatOut(session_id=sid, reply=result.reply, tools=chips, events=result.events, error=bool(result.error))

    @app.post("/api/reset")
    def reset(body: dict | None = None) -> dict:
        sid = (body or {}).get("session_id")
        with sessions_lock:
            sessions.pop(sid, None)
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Interface web da Lia (Empório da Música)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    print(f"🎸 Lia no ar: http://{args.host}:{args.port}")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
