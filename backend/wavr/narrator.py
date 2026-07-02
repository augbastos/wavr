from __future__ import annotations

from typing import Callable


def build_prompt(state: dict, history: list) -> str:
    """Build a natural-language-summary prompt from DERIVED occupancy only. Never
    include raw vitals numbers, source internals, frames, MACs, or RTSP URLs — the
    cloud LLM sees occupancy, not biometrics."""
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


_MODEL = None


def make_gemini_generate(api_key: str, model: str = "gemini-1.5-flash") -> Callable[[str], str]:
    """Real generator: lazy-imports the Gemini SDK. Only reached when narration is
    configured + invoked."""
    def generate(prompt: str) -> str:
        global _MODEL
        if _MODEL is None:
            import google.generativeai as genai   # optional dep
            genai.configure(api_key=api_key)
            _MODEL = genai.GenerativeModel(model)
        return _MODEL.generate_content(prompt).text
    return generate
