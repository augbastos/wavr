from wavr.narrator import Narrator, build_prompt

STATE = {"sala": {"room": "sala", "occupied": True, "confidence": 0.77, "vitals": {"breathing_bpm": 14.2},
                  "sources": [{"modality": "wifi_csi"}], "explanation": "wifi: presente", "ts": "2026-07-02T10:00:00+00:00"}}
HISTORY = [{"room": "sala", "occupied": False, "confidence": 0.1, "vitals": {}, "sources": [],
            "explanation": "", "ts": "2026-07-02T09:59:00+00:00"}]

def test_build_prompt_includes_room_state_but_never_secrets():
    p = build_prompt(STATE, HISTORY)
    assert "sala" in p and ("ocupad" in p.lower() or "occupied" in p.lower())
    # PRIVACY: raw vitals numbers, source internals must not be dumped into the cloud prompt
    assert "14.2" not in p           # raw breathing value never sent
    assert "wifi_csi" not in p       # source modality internals not sent (occupancy summary only)

def test_narrate_calls_generate_with_prompt():
    seen = {}
    def fake_generate(prompt):
        seen["prompt"] = prompt
        return "Sala ocupada desde as 10h."
    out = Narrator(fake_generate).narrate(STATE, HISTORY)
    assert out == "Sala ocupada desde as 10h."
    assert "sala" in seen["prompt"]
