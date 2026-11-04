"""Agent loop tests with a scripted fake backend (no network, no API key)."""

from datetime import datetime

import pytest

from emporio.agent import ERROR_REPLY, MAX_TOOL_ROUNDS, REFUSAL_REPLY, EmporioAgent
from emporio.config import STORE_TZ
from emporio.llm import LLMError, ModelStep, ToolRequest

CLOCK = lambda: datetime(2026, 11, 4, 19, 30, tzinfo=STORE_TZ)  # noqa: E731


class FakeBackend:
    provider = "fake"
    model = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)
        self.history: list[dict] = []

    def add_user_message(self, blocks):
        self.history.append({"role": "user", "blocks": blocks})

    def generate(self):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        self.history.append({"role": "model", "text": step.text, "tools": [r.name for r in step.tool_requests]})
        return step

    def add_tool_results(self, results):
        self.history.append({"role": "tool", "results": [(r.name, out, err) for r, out, err in results]})

    def add_assistant_text(self, text):
        self.history.append({"role": "model", "text": text})

    def checkpoint(self):
        return len(self.history)

    def rollback(self, checkpoint):
        del self.history[checkpoint:]

    def dump(self):
        return self.history

    def load(self, history):
        self.history = history


def make_agent(steps, toolbox, **kw):
    return EmporioAgent(backend=FakeBackend(steps), toolbox=toolbox, clock=CLOCK, history_dir=None, **kw)


def test_tool_round_trip(toolbox):
    agent = make_agent(
        [
            ModelStep(tool_requests=[ToolRequest("1", "search_products", {"query": "Takamine GD20"})]),
            ModelStep(text="Sai por R$ 2.199,00."),
        ],
        toolbox,
    )
    result = agent.reply("Quanto custa o Takamine GD20?")
    assert result.reply == "Sai por R$ 2.199,00."
    assert [c.name for c in result.tool_calls] == ["search_products"]
    assert '"product_id": 95' in result.tool_calls[0].output


def test_user_turn_carries_time_context(toolbox):
    agent = make_agent([ModelStep(text="Oi!")], toolbox)
    agent.reply("Oi")
    context, text = agent.backend.history[0]["blocks"]
    assert context.startswith("[contexto] Agora: quarta-feira, 04/11/2026 19:30")
    assert "FECHADA" in context
    assert text == "Oi"


def test_parallel_tool_calls_are_returned_together(toolbox):
    agent = make_agent(
        [
            ModelStep(
                tool_requests=[
                    ToolRequest("1", "list_active_promotions", {}),
                    ToolRequest("2", "search_store_policies", {"query": "pix"}),
                ]
            ),
            ModelStep(text="ok"),
        ],
        toolbox,
    )
    agent.reply("promoções e pix?")
    tool_msgs = [m for m in agent.backend.history if m["role"] == "tool"]
    assert len(tool_msgs) == 1 and len(tool_msgs[0]["results"]) == 2


def test_refusal_returns_safe_message(toolbox):
    agent = make_agent([ModelStep(refused=True)], toolbox)
    assert agent.reply("...").reply == REFUSAL_REPLY


def test_provider_error_rolls_back_turn(toolbox):
    agent = make_agent([LLMError("503 overloaded")], toolbox)
    result = agent.reply("Oi")
    assert result.reply == ERROR_REPLY
    assert result.error == "503 overloaded"
    assert agent.backend.history == []  # history stays valid for the next turn
    assert agent.transcript == []


def test_runaway_tool_loop_is_capped(toolbox):
    steps = [ModelStep(tool_requests=[ToolRequest(str(i), "list_active_promotions", {})]) for i in range(MAX_TOOL_ROUNDS)]
    agent = make_agent(steps, toolbox)
    result = agent.reply("loop")
    assert result.reply == ERROR_REPLY
    assert len(result.tool_calls) == MAX_TOOL_ROUNDS


def test_conversation_is_persisted_and_resumed(toolbox, tmp_path):
    first = EmporioAgent(backend=FakeBackend([ModelStep(text="Oi! Como posso ajudar?")]), toolbox=toolbox, clock=CLOCK, history_dir=tmp_path)
    first.reply("Oi")
    resumed = EmporioAgent(backend=FakeBackend([]), toolbox=toolbox, clock=CLOCK, history_dir=tmp_path, session_id=first.session_id)
    assert [t["role"] for t in resumed.transcript] == ["user", "assistant"]
    assert resumed.backend.history == first.backend.history


def test_resume_with_other_provider_is_rejected(toolbox, tmp_path):
    first = EmporioAgent(backend=FakeBackend([ModelStep(text="Oi")]), toolbox=toolbox, clock=CLOCK, history_dir=tmp_path)
    first.reply("Oi")
    other = FakeBackend([])
    other.provider = "other"
    with pytest.raises(ValueError):
        EmporioAgent(backend=other, toolbox=toolbox, clock=CLOCK, history_dir=tmp_path, session_id=first.session_id)
