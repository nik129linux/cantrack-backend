"""The text-model client never raises and only ever yields a dict or None."""

import httpx

from cantrack_api.ai_text import OllamaText, extract_object


def client_answering(handler):
    return OllamaText("https://ollama.test", "key", "m", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_disabled_without_a_key_and_makes_no_request():
    calls = []
    model = OllamaText("https://ollama.test", None, "m",
                       client=httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))))
    assert model.enabled is False
    assert model.complete_json("s", "p") is None
    assert calls == []


def test_returns_the_decoded_object():
    model = client_answering(lambda r: httpx.Response(200, json={"message": {"content": '{"a": 1}'}}))
    assert model.complete_json("s", "p") == {"a": 1}


def test_sends_system_and_user_messages_with_json_format():
    seen = {}

    def handler(request):
        seen.update(httpx.Response(200, content=request.content).json())
        return httpx.Response(200, json={"message": {"content": "{}"}})

    client_answering(handler).complete_json("SYSTEM", "USER")
    assert seen["format"] == "json"
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert seen["messages"][1]["content"] == "USER"


def test_fenced_and_embedded_answers_are_parsed():
    assert extract_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_object('Sure! {"a": 3} hope it helps') == {"a": 3}


def test_failures_are_none():
    for handler in (
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(200, json={"message": {"content": "not json"}}),
        lambda r: httpx.Response(200, json={"message": {"content": "[1, 2]"}}),
        lambda r: httpx.Response(200, json={"nope": 1}),
    ):
        assert client_answering(handler).complete_json("s", "p") is None

    def boom(request):
        raise httpx.ConnectTimeout("slow")

    assert client_answering(boom).complete_json("s", "p") is None
