"""Provider chain: OpenAI-compatible text + vision clients with ordered fallback.

Replaces the Ollama-only clients. Contract kept from them: these clients NEVER raise;
text returns ``dict | None`` and vision returns ``PhotoCheck`` (``PhotoCheck(None, None)``
when nothing could be read). A provider that fails in any way (402/429/5xx/401/403,
timeout, transport error, an answer that is not what we asked for) hands over to the next
one; the first good answer wins.
"""

import base64
import json

import httpx
import pytest

from cantrack_ai.vision import PhotoCheck
from cantrack_api.ai_chain import ChainText, ChainVision, Provider, load_providers

JPEG = b"\xff\xd8\xff\xe0fakejpeg"
PNG = b"\x89PNG\r\n\x1a\nfakepng"


def prov(name, host=None):
    return Provider(
        name=name,
        base_url=f"https://{host or name}.test/v1",
        api_key=f"key-{name}",
        text_model=f"{name}-text",
        vision_model=f"{name}-vision",
    )


def http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# ---------------------------------------------------------------- load_providers

class TestLoadProviders:
    def test_default_chain_is_gemini_then_groq(self):
        got = load_providers({"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"})
        assert [p.name for p in got] == ["gemini", "groq"]

    def test_chain_order_follows_ai_chain_and_skips_providers_without_a_key(self):
        env = {"AI_CHAIN": "groq,nvidia,gemini", "GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}
        assert [p.name for p in load_providers(env)] == ["groq", "gemini"]

    def test_unknown_names_and_blank_keys_are_ignored(self):
        env = {"AI_CHAIN": "nope,gemini,groq", "GEMINI_API_KEY": "  ", "GROQ_API_KEY": "q"}
        assert [p.name for p in load_providers(env)] == ["groq"]

    def test_no_keys_means_no_providers(self):
        assert load_providers({}) == []

    def test_defaults_point_at_the_documented_openai_compatible_endpoints(self):
        gem, groq = load_providers({"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"})
        assert gem.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
        assert groq.base_url == "https://api.groq.com/openai/v1"
        assert gem.text_model == "gemini-3.5-flash-lite"
        assert groq.text_model == "openai/gpt-oss-120b"
        assert gem.vision_model and groq.vision_model

    def test_models_and_urls_can_be_overridden_per_provider(self):
        env = {
            "GROQ_API_KEY": "q", "AI_CHAIN": "groq",
            "GROQ_TEXT_MODEL": "t-x", "GROQ_VISION_MODEL": "v-x", "GROQ_BASE_URL": "https://alt.test/v1/",
        }
        (p,) = load_providers(env)
        assert (p.text_model, p.vision_model, p.base_url) == ("t-x", "v-x", "https://alt.test/v1/")


# ---------------------------------------------------------------- ChainText

class TestChainText:
    def test_without_providers_it_is_disabled_and_makes_no_request(self):
        calls = []
        model = ChainText([], client=http(lambda r: calls.append(r)))
        assert model.enabled is False
        assert model.complete_json("s", "p") is None
        assert calls == []

    def test_request_shape(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return ok('{"a": 1}')

        model = ChainText([prov("gemini")], client=http(handler))
        assert model.enabled is True
        assert model.complete_json("SYS", "USER") == {"a": 1}
        assert seen["url"] == "https://gemini.test/v1/chat/completions"
        assert seen["auth"] == "Bearer key-gemini"
        assert seen["body"]["model"] == "gemini-text"
        assert seen["body"]["messages"] == [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USER"},
        ]
        assert seen["body"]["response_format"] == {"type": "json_object"}

    def test_fenced_json_is_decoded(self):
        model = ChainText([prov("a")], client=http(lambda r: ok('```json\n{"x": 2}\n```')))
        assert model.complete_json("s", "p") == {"x": 2}

    @pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 503])
    def test_a_failing_provider_hands_over_to_the_next(self, status):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(status) if request.url.host == "a.test" else ok('{"from": "b"}')

        model = ChainText([prov("a"), prov("b")], client=http(handler))
        assert model.complete_json("s", "p") == {"from": "b"}
        assert hosts == ["a.test", "b.test"]
        assert model.last_provider == "b"

    def test_timeout_and_transport_errors_hand_over(self):
        def handler(request):
            if request.url.host == "a.test":
                raise httpx.ReadTimeout("slow", request=request)
            if request.url.host == "b.test":
                raise httpx.ConnectError("down", request=request)
            return ok('{"from": "c"}')

        model = ChainText([prov("a"), prov("b"), prov("c")], client=http(handler))
        assert model.complete_json("s", "p") == {"from": "c"}

    @pytest.mark.parametrize("content", ["not json at all", "[1, 2]", None])
    def test_an_answer_that_is_not_an_object_hands_over(self, content):
        def handler(request):
            return ok(content) if request.url.host == "a.test" else ok('{"ok": true}')

        model = ChainText([prov("a"), prov("b")], client=http(handler))
        assert model.complete_json("s", "p") == {"ok": True}

    def test_the_first_good_answer_stops_the_chain(self):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return ok('{"n": 1}')

        model = ChainText([prov("a"), prov("b")], client=http(handler))
        assert model.complete_json("s", "p") == {"n": 1}
        assert hosts == ["a.test"]
        assert model.last_provider == "a"

    def test_when_every_provider_fails_the_answer_is_none_and_nothing_raises(self):
        model = ChainText([prov("a"), prov("b")], client=http(lambda r: httpx.Response(500)))
        assert model.complete_json("s", "p") is None
        assert model.last_provider is None

    def test_malformed_response_bodies_never_raise(self):
        model = ChainText([prov("a")], client=http(lambda r: httpx.Response(200, json={"choices": []})))
        assert model.complete_json("s", "p") is None


# ---------------------------------------------------------------- ChainVision

class TestChainVision:
    def test_without_providers_it_returns_no_answer_and_makes_no_request(self):
        calls = []
        vision = ChainVision([], client=http(lambda r: calls.append(r)))
        assert vision.check(JPEG) == PhotoCheck(None, None)
        assert calls == []

    def test_request_carries_the_photo_as_a_jpeg_data_url(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return ok('{"dog_visible": true, "note": "Clean white coat."}')

        result = ChainVision([prov("groq")], client=http(handler)).check(JPEG)
        assert result == PhotoCheck(True, "Clean white coat.")
        assert seen["auth"] == "Bearer key-groq"
        assert seen["body"]["model"] == "groq-vision"
        (message,) = seen["body"]["messages"]
        assert message["role"] == "user"
        text_part, image_part = message["content"]
        assert text_part["type"] == "text" and "dog" in text_part["text"].lower()
        assert image_part["type"] == "image_url"
        assert image_part["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()

    def test_png_bytes_are_sent_as_png(self):
        seen = {}

        def handler(request):
            seen["url"] = json.loads(request.content)["messages"][0]["content"][1]["image_url"]["url"]
            return ok('{"dog_visible": false, "note": "A bike."}')

        assert ChainVision([prov("a")], client=http(handler)).check(PNG) == PhotoCheck(False, "A bike.")
        assert seen["url"].startswith("data:image/png;base64,")

    def test_the_alias_key_the_real_model_uses_is_accepted(self):
        vision = ChainVision([prov("a")], client=http(lambda r: ok('```json\n{"is_dog_visible": true, "note": "ok"}\n```')))
        assert vision.check(JPEG) == PhotoCheck(True, "ok")

    @pytest.mark.parametrize("status", [402, 429, 500])
    def test_a_failing_provider_hands_over_to_the_next(self, status):
        def handler(request):
            return httpx.Response(status) if request.url.host == "a.test" else ok('{"dog_visible": true, "note": "b"}')

        vision = ChainVision([prov("a"), prov("b")], client=http(handler))
        assert vision.check(JPEG) == PhotoCheck(True, "b")
        assert vision.last_provider == "b"

    def test_an_unreadable_answer_hands_over_to_the_next(self):
        def handler(request):
            return ok("I cannot tell") if request.url.host == "a.test" else ok('{"dog_visible": false, "note": "x"}')

        assert ChainVision([prov("a"), prov("b")], client=http(handler)).check(JPEG) == PhotoCheck(False, "x")

    def test_when_every_provider_fails_it_degrades_to_no_answer(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        assert ChainVision([prov("a"), prov("b")], client=http(handler)).check(JPEG) == PhotoCheck(None, None)


# ---------------------------------------------------------------- wiring

class TestDependencies:
    def test_get_text_model_and_get_vision_build_chains_from_the_environment(self, monkeypatch):
        from cantrack_api import deps

        monkeypatch.setenv("GEMINI_API_KEY", "g")
        monkeypatch.setenv("GROQ_API_KEY", "q")
        monkeypatch.delenv("AI_CHAIN", raising=False)
        deps.get_text_model.cache_clear()
        deps.get_vision.cache_clear()
        try:
            text, vision = deps.get_text_model(), deps.get_vision()
            assert isinstance(text, ChainText) and text.enabled
            assert isinstance(vision, ChainVision)
            assert [p.name for p in text.providers] == ["gemini", "groq"]
        finally:
            deps.get_text_model.cache_clear()
            deps.get_vision.cache_clear()

    def test_without_any_key_the_features_are_disabled_not_broken(self, monkeypatch):
        from cantrack_api import deps

        for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "AI_CHAIN"):
            monkeypatch.delenv(name, raising=False)
        deps.get_text_model.cache_clear()
        try:
            assert deps.get_text_model().enabled is False
        finally:
            deps.get_text_model.cache_clear()
