"""The customer-service agent: a hand-written tool-use loop, provider-agnostic.

Why a hand-written loop instead of a framework (LangChain, LlamaIndex, CrewAI...)?
The whole agent is one model + six tools. A short loop keeps every decision visible
(what is sent, which tool ran, what came back), makes the tool trace easy to log and
show in the examples, and has no abstraction to fight when debugging. Provider
specifics live in `llm.py`; deterministic safety checks live in `guardrails.py`;
this module only orchestrates.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, guardrails
from .llm import ChatBackend, LLMError, make_backend
from .policies import get_index
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
    events: list[str] = field(default_factory=list)  # guardrail events, for logs/debug/UI


def _policy_values() -> set[float]:
    """Every R$ amount written in the policy manual (free-shipping threshold, fees...)."""
    values: set[float] = set()
    for chunk in get_index().chunks:
        values |= guardrails.brl_values(chunk.text)
    return values


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
        rate_limiter: guardrails.RateLimiter | None = None,
    ):
        self.toolbox = toolbox or Toolbox()
        self.clock = clock
        system = build_system_prompt(self.toolbox.store.category_names())
        self.backend = backend or make_backend(system, self.toolbox.definitions, provider=provider, model=model)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.history_dir = history_dir
        self.rate_limiter = rate_limiter or guardrails.RateLimiter()
        self.transcript: list[dict] = []  # human-readable log (PII masked)
        # Prices the agent is allowed to quote: policy manual + anything a tool returned
        # or the customer wrote in this conversation.
        self.grounded_prices: set[float] = _policy_values()
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
            self.grounded_prices |= {float(v) for v in data.get("grounded_prices", [])}

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
            "grounded_prices": sorted(self.grounded_prices),
            "messages": self.backend.dump(),  # native provider format, replayable as-is
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    def _record(self, user_text: str, result: TurnResult) -> None:
        mask = guardrails.mask_pii
        self.transcript.append({"role": "user", "text": mask(user_text)})
        for call in result.tool_calls:
            self.transcript.append(
                {
                    "role": "tool",
                    "name": call.name,
                    "input": json.loads(mask(json.dumps(call.input, ensure_ascii=False))),
                    "output": mask(call.output),
                    "is_error": call.is_error,
                }
            )
        if result.events:
            self.transcript.append({"role": "guardrail", "events": result.events})
        self.transcript.append({"role": "assistant", "text": result.reply})
        self._save()

    # ------------------------------------------------------------------ main loop
    def reply(self, user_text: str) -> TurnResult:
        check = guardrails.check_input(user_text, self.rate_limiter)
        result = TurnResult(reply="", events=list(check.events))
        if check.blocked_reply:
            result.reply = check.blocked_reply
            self._record(user_text, result)
            return result

        self.grounded_prices |= guardrails.brl_values(check.text)
        checkpoint = self.backend.checkpoint()
        # Volatile context (date/time, open/closed) rides along with the user turn, so the
        # system prompt stays static and cacheable.
        blocks = [turn_context(self.clock())]
        if check.injection_suspected:
            blocks.append(guardrails.INJECTION_REMINDER)
        self.backend.add_user_message([*blocks, check.text])

        corrected = False
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                step = self.backend.generate()
                result.input_tokens += step.input_tokens
                result.output_tokens += step.output_tokens
                result.cached_tokens += step.cached_tokens

                if step.refused:
                    result.reply = REFUSAL_REPLY
                    result.events.append("model_refusal")
                    self.backend.add_assistant_text(REFUSAL_REPLY)
                    break

                if not step.tool_requests:
                    reply = step.text or EMPTY_REPLY
                    out = guardrails.check_output(reply, self.grounded_prices)
                    if out.ok:
                        result.reply = reply
                        break
                    if not corrected:
                        # One corrective round: tell the model what failed and let it fix it.
                        corrected = True
                        result.events.append(
                            f"output_retry:prices={out.ungrounded_prices},leak={out.leaked_markers}"
                        )
                        self.backend.add_user_message([out.correction()])
                        continue
                    result.events.append("output_blocked")
                    result.reply = guardrails.SAFE_FALLBACK
                    self.backend.add_assistant_text(guardrails.SAFE_FALLBACK)
                    break

                outputs = []
                for req in step.tool_requests:
                    output, is_error = self.toolbox.execute(req.name, req.input)
                    result.tool_calls.append(ToolCall(req.name, req.input, output, is_error))
                    if not is_error:
                        self.grounded_prices |= guardrails.brl_values(output)
                    outputs.append((req, output, is_error))
                self.backend.add_tool_results(outputs)
            else:
                result.reply = ERROR_REPLY
                result.events.append("max_tool_rounds")
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

        self._record(user_text, result)
        return result
