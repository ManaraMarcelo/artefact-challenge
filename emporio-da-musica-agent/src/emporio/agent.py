"""The customer-service agent: a manual tool-use loop over the Anthropic Messages API.

Why a hand-written loop instead of a framework (LangChain, LlamaIndex, CrewAI...)?
The whole agent is one model + six tools. A ~100-line loop keeps every decision visible
(what is sent, which tool ran, what came back), makes the tool trace easy to log and
show in the examples, and has no abstraction to fight when debugging.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import anthropic

from . import config
from .prompts import build_system_prompt, turn_context
from .tools import Toolbox

MAX_TOOL_ROUNDS = 8
MAX_TOKENS = 4096
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ERROR_REPLY = (
    "Poxa, tive um probleminha técnico aqui e não consegui concluir sua consulta. "
    "Pode tentar de novo em instantes? Se preferir, fale com a equipe pelo (67) 3341-4444."
)
REFUSAL_REPLY = (
    "Desculpa, com isso eu não consigo ajudar. Mas se precisar de algo sobre instrumentos, "
    "pedidos ou políticas da loja, é só falar!"
)


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
    cache_read_tokens: int = 0


class EmporioAgent:
    def __init__(
        self,
        client: Any | None = None,
        toolbox: Toolbox | None = None,
        model: str = config.MODEL,
        effort: str = config.EFFORT,
        clock: Callable[[], datetime] = config.now,
        session_id: str | None = None,
        history_dir: Path | None = config.RUNTIME_DIR / "conversations",
    ):
        self.client = client or anthropic.Anthropic()
        self.toolbox = toolbox or Toolbox()
        self.model = model
        self.effort = effort
        self.clock = clock
        self.system = build_system_prompt(self.toolbox.store.category_names())
        self.tools = self.toolbox.definitions
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.history_dir = history_dir
        self.messages: list[dict] = []
        self.transcript: list[dict] = []  # human-readable log: user/assistant/tool entries
        if history_dir:
            self._load()

    # ------------------------------------------------------------------ persistence
    @property
    def history_path(self) -> Path | None:
        return self.history_dir / f"{self.session_id}.json" if self.history_dir else None

    def _load(self) -> None:
        path = self.history_path
        if path and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.messages = data["messages"]
            self.transcript = data.get("transcript", [])

    def _save(self) -> None:
        path = self.history_path
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.session_id,
            "model": self.model,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "messages": self.messages,
            "transcript": self.transcript,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ model call
    def _create(self):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            # Thinking runs adaptively by default on this model; low effort keeps chat replies fast.
            output_config={"effort": self.effort},
            # Cache the stable prefix (tools + system + earlier turns) across turns.
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, retry server-side on Anthropic's recommended fallback.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    @staticmethod
    def _dump(content) -> list[dict]:
        # Keep every block (including thinking blocks and their signatures) so the
        # history can be replayed to the API exactly as it was produced.
        return [block.model_dump(mode="json", exclude_none=True) for block in content]

    # ------------------------------------------------------------------ public API
    def reply(self, user_text: str) -> TurnResult:
        start = len(self.messages)
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": turn_context(self.clock())},
                    {"type": "text", "text": user_text},
                ],
            }
        )
        result = TurnResult(reply="")

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = self._create()
                usage = response.usage
                result.input_tokens += usage.input_tokens or 0
                result.output_tokens += usage.output_tokens or 0
                result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

                if response.stop_reason == "refusal":
                    result.reply = REFUSAL_REPLY
                    self.messages.append({"role": "assistant", "content": [{"type": "text", "text": REFUSAL_REPLY}]})
                    break

                self.messages.append({"role": "assistant", "content": self._dump(response.content)})

                if response.stop_reason == "pause_turn":
                    continue

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if response.stop_reason != "tool_use" or not tool_uses:
                    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                    result.reply = text or "Desculpa, pode repetir? Não entendi bem sua mensagem."
                    break

                # Execute every requested tool and return all results in ONE user message.
                tool_results = []
                for block in tool_uses:
                    output, is_error = self.toolbox.execute(block.name, dict(block.input or {}))
                    result.tool_calls.append(ToolCall(block.name, dict(block.input or {}), output, is_error))
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error}
                    )
                self.messages.append({"role": "user", "content": tool_results})
            else:
                result.reply = ERROR_REPLY
                self.messages.append({"role": "assistant", "content": [{"type": "text", "text": ERROR_REPLY}]})
        except anthropic.APIError as exc:
            # Roll back this turn so the stored history stays valid for the next request.
            del self.messages[start:]
            result.reply = ERROR_REPLY
            result.tool_calls.append(ToolCall("api_error", {}, f"{type(exc).__name__}: {exc}", True))
            return result
        except Exception:
            del self.messages[start:]
            raise

        self.transcript.append({"role": "user", "text": user_text})
        for call in result.tool_calls:
            self.transcript.append(
                {"role": "tool", "name": call.name, "input": call.input, "output": call.output, "is_error": call.is_error}
            )
        self.transcript.append({"role": "assistant", "text": result.reply})
        self._save()
        return result
