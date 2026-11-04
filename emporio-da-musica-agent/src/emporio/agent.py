"""The customer-service agent: a hand-written tool-use loop, provider-agnostic.

Why a hand-written loop instead of a framework (LangChain, LlamaIndex, CrewAI...)?
The whole agent is one model + six tools. A short loop keeps every decision visible
(what is sent, which tool ran, what came back), makes the tool trace easy to log and
show in the examples, and has no abstraction to fight when debugging. Provider
specifics live in `llm.py`; this module only orchestrates.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config
from .llm import ChatBackend, LLMError, make_backend
from .prompts import build_system_prompt, turn_context
from .tools import Toolbox

MAX_TOOL_ROUNDS = 8

ERROR_REPLY = (
    "Poxa, tive um probleminha técnico aqui e não consegui concluir sua consulta. "
    "Pode tentar de novo em instantes? Se preferir, fale com a equipe pelo (67) 3341-4444."
)
REFUSAL_REPLY = (
    "Desculpa, com isso eu não consigo ajudar. Mas se precisar de algo sobre instrumentos, "
    "pedidos ou políticas da loja, é só falar!"
)
EMPTY_REPLY = "Desculpa, pode repetir? Não entendi bem sua mensagem."


@dataclass
class ToolCall:
    name: str
    input: dict
    output: str
    is_error: bool


@dataclass
class TurnResult:
    reply: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    error: str | None = None


class EmporioAgent:
    def __init__(
        self,
        backend: ChatBackend | None = None,
        toolbox: Toolbox | None = None,
        provider: str | None = None,
        model: str | None = None,
        clock: Callable[[], datetime] = config.now,
        session_id: str | None = None,
        history_dir: Path | None = config.RUNTIME_DIR / "conversations",
    ):
        self.toolbox = toolbox or Toolbox()
        self.clock = clock
        system = build_system_prompt(self.toolbox.store.category_names())
        self.backend = backend or make_backend(system, self.toolbox.definitions, provider=provider, model=model)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.history_dir = history_dir
        self.transcript: list[dict] = []  # human-readable log: user / tool / assistant entries
        if history_dir:
            self._load()

    @property
    def model(self) -> str:
        return f"{self.backend.provider}:{self.backend.model}"

    # ------------------------------------------------------------------ persistence
    @property
    def history_path(self) -> Path | None:
        return self.history_dir / f"{self.session_id}.json" if self.history_dir else None

    def _load(self) -> None:
        path = self.history_path
        if path and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("provider") != self.backend.provider:
                raise ValueError(
                    f"A sessão {self.session_id} foi criada com o provedor {data.get('provider')}; "
                    f"o atual é {self.backend.provider}."
                )
            self.backend.load(data["messages"])
            self.transcript = data.get("transcript", [])

    def _save(self) -> None:
        path = self.history_path
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.session_id,
            "provider": self.backend.provider,
            "model": self.backend.model,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "transcript": self.transcript,
            "messages": self.backend.dump(),  # native provider format, replayable as-is
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ main loop
    def reply(self, user_text: str) -> TurnResult:
        checkpoint = self.backend.checkpoint()
        # Volatile context (date/time, open/closed) rides along with the user turn, so the
        # system prompt stays static and cacheable.
        self.backend.add_user_message([turn_context(self.clock()), user_text])
        result = TurnResult(reply="")

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                step = self.backend.generate()
                result.input_tokens += step.input_tokens
                result.output_tokens += step.output_tokens
                result.cached_tokens += step.cached_tokens

                if step.refused:
                    result.reply = REFUSAL_REPLY
                    self.backend.add_assistant_text(REFUSAL_REPLY)
                    break
                if not step.tool_requests:
                    result.reply = step.text or EMPTY_REPLY
                    break

                outputs = []
                for req in step.tool_requests:
                    output, is_error = self.toolbox.execute(req.name, req.input)
                    result.tool_calls.append(ToolCall(req.name, req.input, output, is_error))
                    outputs.append((req, output, is_error))
                self.backend.add_tool_results(outputs)
            else:
                result.reply = ERROR_REPLY
                self.backend.add_assistant_text(ERROR_REPLY)
        except LLMError as exc:
            # Roll back this turn so the stored history stays valid for the next request.
            self.backend.rollback(checkpoint)
            result.reply = ERROR_REPLY
            result.error = str(exc)
            return result
        except Exception:
            self.backend.rollback(checkpoint)
            raise

        self.transcript.append({"role": "user", "text": user_text})
        for call in result.tool_calls:
            self.transcript.append(
                {"role": "tool", "name": call.name, "input": call.input, "output": call.output, "is_error": call.is_error}
            )
        self.transcript.append({"role": "assistant", "text": result.reply})
        self._save()
        return result
