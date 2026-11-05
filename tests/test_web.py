"""Web API tests with a scripted backend (no network)."""

from fastapi.testclient import TestClient

from emporio.agent import EmporioAgent
from emporio.llm import ModelStep, ToolRequest
from emporio.web import create_app
from tests.test_agent import CLOCK, FakeBackend


def make_client(toolbox, steps):
    def factory(session_id):
        return EmporioAgent(backend=FakeBackend(list(steps)), toolbox=toolbox, clock=CLOCK, history_dir=None, session_id=session_id)

    return TestClient(create_app(factory))


def test_index_and_static_are_served(toolbox):
    client = make_client(toolbox, [])
    page = client.get("/")
    assert page.status_code == 200 and "Lia" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_status_endpoint(toolbox):
    data = make_client(toolbox, []).get("/api/status").json()
    assert data["store"].startswith(("ABERTA", "FECHADA"))


def test_chat_returns_reply_and_tool_chips(toolbox):
    client = make_client(
        toolbox,
        [
            ModelStep(tool_requests=[ToolRequest("1", "search_products", {"query": "Takamine GD20"})]),
            ModelStep(text="Sai por R$ 2.199,00."),
        ],
    )
    data = client.post("/api/chat", json={"message": "Quanto custa o Takamine GD20?"}).json()
    assert data["reply"] == "Sai por R$ 2.199,00."
    assert data["tools"][0]["label"] == "catálogo" and data["tools"][0]["kind"] == "catalog"
    assert data["session_id"]


def test_session_is_kept_between_messages(toolbox):
    client = make_client(toolbox, [ModelStep(text="Oi!"), ModelStep(text="Tudo ótimo!")])
    first = client.post("/api/chat", json={"message": "Oi"}).json()
    second = client.post("/api/chat", json={"message": "Tudo bem?", "session_id": first["session_id"]}).json()
    assert second["session_id"] == first["session_id"]
    assert second["reply"] == "Tudo ótimo!"


def test_message_too_long_is_rejected(toolbox):
    client = make_client(toolbox, [])
    assert client.post("/api/chat", json={"message": "a" * 5000}).status_code == 422
