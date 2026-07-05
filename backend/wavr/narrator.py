from __future__ import annotations

import json
import urllib.request
from typing import Callable


def build_prompt(state: dict, history: list) -> str:
    """Build a natural-language-summary prompt from DERIVED occupancy only. Never
    include raw vitals numbers, source internals, frames, MACs, or RTSP URLs — the
    LLM sees occupancy, not biometrics. This PRIVACY ALLOWLIST is shared verbatim by
    EVERY provider (cloud or local): switching the backend never changes what leaves
    the box."""
    lines = ["Resuma em português, em 1-2 frases, o estado de presença da casa.",
             "Estado atual por cômodo:"]
    for room, rs in sorted(state.items()):
        pct = round(rs.get("confidence", 0) * 100)
        status = "ocupado" if rs.get("occupied") else "vazio"
        lines.append(f"- {room}: {status} ({pct}% de confiança)")
    if history:
        occ = sum(1 for h in history if h.get("occupied"))
        lines.append(f"Nas últimas {len(history)} leituras houve {occ} com presença detectada.")
    return "\n".join(lines)


class Narrator:
    """Summarizes derived RoomState into natural language via an injected LLM seam."""

    def __init__(self, generate: Callable[[str], str]):
        self._generate = generate

    def narrate(self, state: dict, history: list) -> str:
        return self._generate(build_prompt(state, history))


def _post_json(url: str, payload: dict, headers: dict | None = None,
               timeout: float = 30) -> dict:
    """Minimal stdlib JSON POST — no third-party HTTP dependency. Credentials, when
    present, live ONLY in the `headers` dict (never in the URL or body), so a urllib
    error string / traceback (which carries the URL + status, not headers) can never
    echo an API key."""
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # nosec B310 (fixed https/http LAN)
        return json.loads(resp.read().decode("utf-8"))


# ----------------------------------------------------------------------------- #
# Providers. Each returns a `generate(prompt) -> str` closure. All are lazy/on-
# demand (no work at import or wiring time) and carry a timeout. Only Ollama is
# LOCAL (zero cloud egress); the rest reach a cloud endpoint by default.
# ----------------------------------------------------------------------------- #

def make_ollama_generate(model: str,
                         base_url: str = "http://localhost:11434") -> Callable[[str], str]:
    """LOCAL provider — ZERO external egress. Talks to a self-hosted Ollama daemon on
    the LAN/loopback via its native /api/generate (stream:false). No API key exists,
    so nothing sensitive can leave the box; this is the privacy-first narrator."""
    def generate(prompt: str) -> str:
        url = base_url.rstrip("/") + "/api/generate"
        body = {"model": model, "prompt": prompt, "stream": False}
        data = _post_json(url, body, timeout=120)   # local models can be slow
        return (data.get("response") or "").strip()
    return generate


def make_openai_generate(base_url: str, api_key: str, model: str) -> Callable[[str], str]:
    """OpenAI-compatible /v1/chat/completions (covers OpenAI/Codex AND local servers
    like LM Studio / llama.cpp). Cloud egress when `base_url` is the OpenAI default;
    LOCAL when pointed at a loopback server. The key is sent only in the Authorization
    header and only when set (local servers accept a keyless request)."""
    def generate(prompt: str) -> str:
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        data = _post_json(url, body, headers=headers, timeout=30)
        return data["choices"][0]["message"]["content"].strip()
    return generate


def make_anthropic_generate(api_key: str, model: str) -> Callable[[str], str]:
    """Anthropic Claude /v1/messages. CLOUD egress. The key is sent only in the
    x-api-key header (never URL/body), matching the no-leak guarantee of the others."""
    def generate(prompt: str) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}]}
        data = _post_json(url, body, headers=headers, timeout=30)
        return data["content"][0]["text"].strip()
    return generate


_MODEL = None


def make_gemini_generate(api_key: str, model: str = "gemini-1.5-flash") -> Callable[[str], str]:
    """CLOUD provider: lazy-imports the Gemini SDK. Only reached when narration is
    configured + invoked. Unchanged — the operator's economical default."""
    def generate(prompt: str) -> str:
        global _MODEL
        if _MODEL is None:
            import google.generativeai as genai   # optional dep
            genai.configure(api_key=api_key)
            _MODEL = genai.GenerativeModel(model)
        return _MODEL.generate_content(prompt, request_options={"timeout": 30}).text
    return generate


def provider_configured(cfg) -> bool:
    """Is the SELECTED narrator provider actually usable? This is the second factor of
    the two-factor default-OFF gate (the first is cfg.narrate_enabled, checked by the
    caller). Local Ollama needs no key — merely selecting it counts as configured.
    Cloud providers require their key present, so a bare/missing key stays 503.
    (A keyless local OpenAI-compatible server, e.g. LM Studio, can pass any
    placeholder in WAVR_OPENAI_API_KEY — it is ignored by the local server but keeps
    this gate simple and explicit.)"""
    provider = getattr(cfg, "narrate_provider", "gemini")
    if provider == "ollama":
        return True
    if provider == "openai":
        return bool(cfg.openai_api_key)
    if provider == "anthropic":
        return bool(cfg.anthropic_api_key)
    return bool(cfg.gemini_api_key)


def make_generate(cfg) -> Callable[[str], str]:
    """Factory: pick the provider closure by cfg.narrate_provider
    (gemini|ollama|openai|anthropic). Defaults to gemini for backward-compat. Callers
    must have already confirmed narrate_enabled AND provider_configured(cfg)."""
    provider = getattr(cfg, "narrate_provider", "gemini")
    if provider == "ollama":
        return make_ollama_generate(cfg.ollama_model, cfg.ollama_url)
    if provider == "openai":
        return make_openai_generate(cfg.openai_base_url, cfg.openai_api_key, cfg.openai_model)
    if provider == "anthropic":
        return make_anthropic_generate(cfg.anthropic_api_key, cfg.anthropic_model)
    return make_gemini_generate(cfg.gemini_api_key, cfg.gemini_model)
