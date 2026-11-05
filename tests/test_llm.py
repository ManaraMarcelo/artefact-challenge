"""Gemini backend behaviour that does not need the network: key rotation on daily quota."""

from types import SimpleNamespace

import pytest
from google.genai import errors, types

from emporio import llm

DAILY_QUOTA = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota",
        "status": "RESOURCE_EXHAUSTED",
        "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}],
    }
}
OK = types.GenerateContentResponse.model_validate(
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "Oi!"}]}, "finish_reason": "STOP"}]}
)


class FakeModels:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    def generate_content(self, **_):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture(autouse=True)
def isolated_keys(monkeypatch):
    llm._EXHAUSTED_KEYS.clear()
    monkeypatch.setattr(llm.GeminiBackend, "RETRY_DELAYS", ())
    for i in range(2, 10):
        monkeypatch.delenv(f"GEMINI_API_KEY_{i}", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "key-a")
    monkeypatch.setenv("GEMINI_API_KEY_2", "key-b")
    yield
    llm._EXHAUSTED_KEYS.clear()


def make_backend(first, second):
    backend = llm.GeminiBackend("gemini-test", "system", [])
    backend._clients = [SimpleNamespace(models=first), SimpleNamespace(models=second)]
    backend.client = backend._clients[0]
    backend.add_user_message(["Oi"])
    return backend


def test_daily_quota_rotates_to_next_key():
    exhausted, healthy = FakeModels(errors.ClientError(429, DAILY_QUOTA)), FakeModels(OK)
    backend = make_backend(exhausted, healthy)
    assert backend.generate().text == "Oi!"
    assert (exhausted.calls, healthy.calls) == (1, 1)
    assert "key-a" in llm._EXHAUSTED_KEYS


def test_all_keys_exhausted_raises_llm_error():
    backend = make_backend(FakeModels(errors.ClientError(429, DAILY_QUOTA)), FakeModels(errors.ClientError(429, DAILY_QUOTA)))
    with pytest.raises(llm.LLMError, match="cota diária"):
        backend.generate()


def test_duplicate_keys_are_ignored(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_2", "key-a")
    from emporio import config

    assert config.gemini_api_keys() == ["key-a"]
