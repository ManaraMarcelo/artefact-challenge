"""LLM provider backends (Google Gemini and Anthropic Claude) behind one small interface.

The agent loop only needs four operations: add a user message, generate a step (text
and/or tool requests), add tool results, and dump/load the history. Each backend keeps
its conversation in the provider's *native* format, because both providers attach
opaque reasoning metadata (Gemini `thought_signature`, Claude `thinking` blocks) that
must be sent back untouched for multi-step tool use to work. Converting to a lowest-
common-denominator format would silently drop it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Protocol

from . import config


class LLMError(Exception):
    """Provider-agnostic failure (network, quota, invalid request...)."""


@dataclass
class ToolRequest:
    id: str
    name: str
    input: dict
    native_id: str | None = None  # id assigned by the provider, when it assigns one


@dataclass
class ModelStep:
    text: str = ""
    tool_requests: list[ToolRequest] = field(default_factory=list)
    refused: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class ChatBackend(Protocol):
    provider: str
    model: str

    def add_user_message(self, blocks: list[str]) -> None: ...
    def generate(self) -> ModelStep: ...
    def add_tool_results(self, results: list[tuple[ToolRequest, str, bool]]) -> None: ...
    def add_assistant_text(self, text: str) -> None: ...
    def checkpoint(self) -> int: ...
    def rollback(self, checkpoint: int) -> None: ...
    def dump(self) -> list[dict]: ...
    def load(self, history: list[dict]) -> None: ...


# =============================================================================== Gemini

class GeminiBackend:
    """Google Gemini via the official `google-genai` SDK (free tier available)."""

    provider = "gemini"
    RETRY_DELAYS = (5, 15, 30)  # free tier has low requests-per-minute limits

    def __init__(self, model: str, system: str, tools: list[dict]):
        from google import genai
        from google.genai import types

        self._types = types
        self._errors = __import__("google.genai.errors", fromlist=["APIError"])
        self.model = model
        api_key = config.gemini_api_key()
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        declarations = []
        for t in tools:
            schema = t["input_schema"]
            decl = {"name": t["name"], "description": t["description"]}
            if schema.get("properties"):
                decl["parameters_json_schema"] = schema
            declarations.append(types.FunctionDeclaration(**decl))
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)],
            # We run tools ourselves (identity checks, logging, error handling).
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self.contents: list = []

    def add_user_message(self, blocks: list[str]) -> None:
        t = self._types
        self.contents.append(t.Content(role="user", parts=[t.Part(text=b) for b in blocks]))

    def _call(self):
        for attempt, delay in enumerate((0, *self.RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                return self.client.models.generate_content(model=self.model, contents=self.contents, config=self.config)
            except self._errors.APIError as exc:
                retryable = exc.code in (429, 500, 503)
                if not retryable or attempt == len(self.RETRY_DELAYS):
                    raise LLMError(f"Gemini {exc.code}: {exc}") from exc
            except Exception as exc:  # network errors
                if attempt == len(self.RETRY_DELAYS):
                    raise LLMError(f"Gemini: {exc}") from exc
        raise LLMError("Gemini: retries exhausted")  # pragma: no cover

    def generate(self) -> ModelStep:
        response = self._call()
        usage = response.usage_metadata
        step = ModelStep(
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0,
            cached_tokens=(usage.cached_content_token_count or 0) if usage else 0,
        )
        candidate = response.candidates[0] if response.candidates else None
        blocked = response.prompt_feedback is not None and response.prompt_feedback.block_reason is not None
        if blocked or candidate is None or candidate.content is None:
            step.refused = True
            return step
        finish = candidate.finish_reason.name if candidate.finish_reason else ""
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            step.refused = True
            return step

        self.contents.append(candidate.content)  # keep thought signatures intact
        for i, part in enumerate(candidate.content.parts or []):
            if part.function_call:
                fc = part.function_call
                step.tool_requests.append(
                    ToolRequest(fc.id or f"call_{len(self.contents)}_{i}", fc.name, dict(fc.args or {}), native_id=fc.id)
                )
            elif part.text and not part.thought:
                step.text += part.text
        step.text = step.text.strip()
        return step

    def add_tool_results(self, results: list[tuple[ToolRequest, str, bool]]) -> None:
        t = self._types
        parts = []
        for req, output, is_error in results:
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                payload = {"result": output}
            if is_error:
                payload = {"error": payload}
            parts.append(
                t.Part(function_response=t.FunctionResponse(id=req.native_id, name=req.name, response=payload))
            )
        self.contents.append(t.Content(role="user", parts=parts))

    def add_assistant_text(self, text: str) -> None:
        t = self._types
        self.contents.append(t.Content(role="model", parts=[t.Part(text=text)]))

    def checkpoint(self) -> int:
        return len(self.contents)

    def rollback(self, checkpoint: int) -> None:
        del self.contents[checkpoint:]

    def dump(self) -> list[dict]:
        return [c.model_dump(mode="json", exclude_none=True) for c in self.contents]

    def load(self, history: list[dict]) -> None:
        self.contents = [self._types.Content.model_validate(c) for c in history]


# ============================================================================ Anthropic

class AnthropicBackend:
    """Anthropic Claude via the official `anthropic` SDK (Messages API)."""

    provider = "anthropic"
    MAX_TOKENS = 4096
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model: str, system: str, tools: list[dict], effort: str = config.EFFORT, client=None):
        import anthropic

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.system = system
        self.tools = tools
        self.effort = effort
        self.messages: list[dict] = []

    def add_user_message(self, blocks: list[str]) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": b} for b in blocks]})

    def _call(self):
        try:
            return self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.MAX_TOKENS,
                system=self.system,
                tools=self.tools,
                messages=self.messages,
                # Thinking runs adaptively by default; low effort keeps chat replies fast.
                output_config={"effort": self.effort},
                # Cache the stable prefix (tools + system + earlier turns) across turns.
                cache_control={"type": "ephemeral"},
                # If a safety classifier declines, retry server-side on the recommended fallback model.
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
            )
        except self._anthropic.APIError as exc:
            raise LLMError(f"Anthropic {type(exc).__name__}: {exc}") from exc
        except TypeError as exc:  # raised by the SDK when no credential is configured
            raise LLMError(f"Anthropic: {exc}") from exc

    def generate(self) -> ModelStep:
        while True:
            response = self._call()
            usage = response.usage
            step = ModelStep(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
            if response.stop_reason == "refusal":
                step.refused = True
                return step
            # Keep every block (thinking + signature included) so history replays exactly.
            self.messages.append(
                {"role": "assistant", "content": [b.model_dump(mode="json", exclude_none=True) for b in response.content]}
            )
            if response.stop_reason == "pause_turn":
                continue
            for block in response.content:
                if block.type == "tool_use":
                    step.tool_requests.append(ToolRequest(block.id, block.name, dict(block.input or {})))
                elif block.type == "text":
                    step.text += block.text
            step.text = step.text.strip()
            return step

    def add_tool_results(self, results: list[tuple[ToolRequest, str, bool]]) -> None:
        # All results go back in ONE user message (keeps parallel tool calls working).
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": req.id, "content": output, "is_error": is_error}
                    for req, output, is_error in results
                ],
            }
        )

    def add_assistant_text(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})

    def checkpoint(self) -> int:
        return len(self.messages)

    def rollback(self, checkpoint: int) -> None:
        del self.messages[checkpoint:]

    def dump(self) -> list[dict]:
        return self.messages

    def load(self, history: list[dict]) -> None:
        self.messages = history


# ============================================================================== factory

def make_backend(system: str, tools: list[dict], provider: str | None = None, model: str | None = None) -> ChatBackend:
    provider = provider or config.provider()
    model = model or config.default_model(provider)
    if provider == "gemini":
        return GeminiBackend(model, system, tools)
    if provider == "anthropic":
        return AnthropicBackend(model, system, tools)
    raise ValueError(f"Provedor desconhecido: {provider!r} (use 'gemini' ou 'anthropic')")
