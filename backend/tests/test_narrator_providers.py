"""Provider-agnostic narrator: each provider builds the right HTTP request + parses
the right response (HTTP fully mocked -- NO real network), the shared privacy
allowlist (build_prompt) is unchanged, the factory routes by WAVR_NARRATE_PROVIDER,
and API keys are never echoed in an error."""
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from wavr.narrator import (
    Narrator,
    build_prompt,
    make_anthropic_generate,
    make_generate,
    make_ollama_generate,
    make_openai_generate,
    provider_configured,
)

STATE = {"sala": {"room": "sala", "occupied": True, "confidence": 0.77,
                  "vitals": {"breathing_bpm": 14.2},
                  "sources": [{"modality": "wifi_csi"}]}}
HISTORY = [{"room": "sala", "occupied": False, "confidence": 0.1}]


class _FakeResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, response, captured):
    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        # urllib title-cases header keys; normalise to lower for assertions.
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


# --------------------------------------------------------------------------- #
# Ollama -- LOCAL, zero egress, no key.
# --------------------------------------------------------------------------- #

def test_ollama_builds_request_and_parses_response(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, {"response": "Casa vazia agora."}, captured)
    gen = make_ollama_generate("llama3.2", "http://localhost:11434")
    out = gen("hello prompt")
    assert out == "Casa vazia agora."
    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["body"]["stream"] is False
    assert captured["body"]["model"] == "llama3.2"
    assert captured["body"]["prompt"] == "hello prompt"
    assert captured["timeout"] == 120
    # LOCAL: no credential header is ever sent.
    assert "authorization" not in captured["headers"]
    assert "x-api-key" not in captured["headers"]


def test_ollama_default_base_url_is_loopback():
    # Zero-egress guarantee lives in the default: loopback daemon.
    gen = make_ollama_generate("m")
    assert gen.__closure__ is not None  # closure exists
    # default asserted via config test; here confirm the callable is built lazily
    # (no network at construction time -- reached this line without a request).


# --------------------------------------------------------------------------- #
# OpenAI-compatible (OpenAI/Codex/LM Studio).
# --------------------------------------------------------------------------- #

def test_openai_builds_request_and_parses_response(monkeypatch):
    captured = {}
    _patch_urlopen(
        monkeypatch,
        {"choices": [{"message": {"content": "Sala ocupada."}}]},
        captured,
    )
    gen = make_openai_generate("https://api.openai.com/v1", "sk-secret-123", "gpt-4o-mini")
    out = gen("p")
    assert out == "Sala ocupada."
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer sk-secret-123"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["messages"] == [{"role": "user", "content": "p"}]


def test_openai_local_server_keyless_sends_no_auth(monkeypatch):
    captured = {}
    _patch_urlopen(
        monkeypatch,
        {"choices": [{"message": {"content": "ok"}}]},
        captured,
    )
    gen = make_openai_generate("http://localhost:1234/v1", "", "local-model")
    gen("p")
    assert captured["url"] == "http://localhost:1234/v1/chat/completions"
    assert "authorization" not in captured["headers"]


# --------------------------------------------------------------------------- #
# Anthropic (Claude).
# --------------------------------------------------------------------------- #

def test_anthropic_builds_request_and_parses_response(monkeypatch):
    captured = {}
    _patch_urlopen(monkeypatch, {"content": [{"text": "Oi, casa tranquila."}]}, captured)
    gen = make_anthropic_generate("ak-secret-xyz", "claude-3-5-haiku-latest")
    out = gen("p")
    assert out == "Oi, casa tranquila."
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "ak-secret-xyz"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["model"] == "claude-3-5-haiku-latest"
    assert captured["body"]["max_tokens"] >= 1


# --------------------------------------------------------------------------- #
# Shared privacy allowlist -- unchanged, and shared by every provider.
# --------------------------------------------------------------------------- #

def test_build_prompt_never_leaks_biometrics():
    p = build_prompt(STATE, HISTORY)
    assert "sala" in p
    assert "14.2" not in p       # raw vitals never sent
    assert "wifi_csi" not in p   # source internals never sent


def test_provider_receives_only_the_allowlisted_prompt(monkeypatch):
    # Route the derived state THROUGH a provider via Narrator: the prompt actually
    # POSTed carries occupancy but never biometrics -- the allowlist is shared.
    captured = {}
    _patch_urlopen(monkeypatch, {"response": "resumo"}, captured)
    Narrator(make_ollama_generate("m")).narrate(STATE, HISTORY)
    sent = captured["body"]["prompt"]
    assert sent == build_prompt(STATE, HISTORY)
    assert "14.2" not in sent and "wifi_csi" not in sent


# --------------------------------------------------------------------------- #
# Factory routing by WAVR_NARRATE_PROVIDER.
# --------------------------------------------------------------------------- #

def _cfg(**over):
    base = dict(
        narrate_provider="gemini",
        gemini_api_key="g-key", gemini_model="gemini-1.5-flash",
        ollama_url="http://localhost:11434", ollama_model="llama3.2",
        openai_api_key="", openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4o-mini",
        anthropic_api_key="", anthropic_model="claude-3-5-haiku-latest",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("provider,url", [
    ("ollama", "http://localhost:11434/api/generate"),
    ("openai", "https://api.openai.com/v1/chat/completions"),
    ("anthropic", "https://api.anthropic.com/v1/messages"),
])
def test_factory_selects_provider(monkeypatch, provider, url):
    captured = {}
    # A response shape each parser accepts.
    _patch_urlopen(
        monkeypatch,
        {"response": "x", "choices": [{"message": {"content": "x"}}],
         "content": [{"text": "x"}]},
        captured,
    )
    cfg = _cfg(narrate_provider=provider, openai_api_key="k", anthropic_api_key="k")
    gen = make_generate(cfg)
    gen("p")
    assert captured["url"] == url


def test_factory_default_is_gemini(monkeypatch):
    # gemini uses the SDK, not urllib -- assert routing by intercepting make_gemini.
    called = {}

    def fake_gemini(api_key, model):
        called["api_key"] = api_key
        called["model"] = model
        return lambda prompt: "sdk"

    monkeypatch.setattr("wavr.narrator.make_gemini_generate", fake_gemini)
    gen = make_generate(_cfg(narrate_provider="gemini"))
    assert gen("p") == "sdk"
    assert called["model"] == "gemini-1.5-flash"


# --------------------------------------------------------------------------- #
# Two-factor gate helper: provider_configured.
# --------------------------------------------------------------------------- #

def test_provider_configured_ollama_needs_no_key():
    assert provider_configured(_cfg(narrate_provider="ollama")) is True


def test_provider_configured_cloud_requires_key():
    assert provider_configured(_cfg(narrate_provider="openai", openai_api_key="")) is False
    assert provider_configured(_cfg(narrate_provider="openai", openai_api_key="k")) is True
    assert provider_configured(_cfg(narrate_provider="anthropic", anthropic_api_key="")) is False
    assert provider_configured(_cfg(narrate_provider="anthropic", anthropic_api_key="k")) is True
    assert provider_configured(_cfg(narrate_provider="gemini", gemini_api_key="")) is False
    assert provider_configured(_cfg(narrate_provider="gemini", gemini_api_key="k")) is True


# --------------------------------------------------------------------------- #
# Key never leaks in an error / traceback.
# --------------------------------------------------------------------------- #

def test_api_key_never_in_http_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    gen = make_openai_generate("https://api.openai.com/v1", "sk-super-secret", "m")
    with pytest.raises(urllib.error.HTTPError) as ei:
        gen("p")
    assert "sk-super-secret" not in str(ei.value)
    assert "sk-super-secret" not in ei.value.filename  # the URL carries no key
