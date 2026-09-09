"""What an AI agent can actually learn about a Wavr Space — run it and see.

Companion to `chaos_demo.py`: no hardware, no network, no LLM. It stands up a
real MCP server over a real Space and asks the three questions an agent asks,
then proves the two things that matter more than the answers — that nothing
private leaked, and that a default agent is genuinely bounded.

    python scripts/mcp_space_demo.py

Needs the [mcp] extra:  pip install -e backend[mcp]

---

The product target this exists to demonstrate:

    "I can ask an external AI what is happening in my physical environment, and
     the AI can retrieve a structured, explainable, permission-bounded answer
     from Wavr."

This plays the AI's side: it stands up a real MCP server over the real Space
model and asks the questions an agent would ask. It also checks the
permission-bounded half, which is the part that would be easy to skip.
"""
import asyncio
import json
import sys
from pathlib import Path

# Run from a checkout without installing, and without hard-coding anyone's
# machine into a public repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from wavr.auth import effective_tool_scopes, tool_call_allowed          # noqa: E402
from wavr.core_registry import CoreRegistry                             # noqa: E402
from wavr.mcp import build_mcp_server                                   # noqa: E402
from wavr.space_store import ROLE_ADMIN, ROLE_OWNER, SpaceStore         # noqa: E402


class Provider:
    """Stands in for the live FusionEngine with a plausible evening at home."""

    ROOMS = {
        "sala": {
            "room": "sala", "occupied": True, "confidence": 0.86, "person_count": 2,
            "sources": [{"modality": "camera", "confidence": 0.9},
                        {"modality": "network", "confidence": 0.5}],
            "explanation": "camera sees two people; two known phones on the network",
            "precision_level": "count", "precision_next": "position",
            "ts": "2026-09-03T20:14:00+00:00",
            # None of this may reach the agent.
            "vitals": {"breathing_bpm": 13.9}, "identities": [{"person": "Alex"}],
            "targets": [{"id": 1, "x": 2.2, "y": 1.4, "posture": "sitting"}],
        },
        "cozinha": {
            "room": "cozinha", "occupied": False, "confidence": 0.08,
            "person_count": None, "sources": [{"modality": "network", "confidence": 0.2}],
            "explanation": "no device seen in range for 40 minutes",
            "precision_level": "room", "ts": "2026-09-03T20:14:00+00:00",
        },
        "garagem": {
            "room": "garagem", "occupied": False, "confidence": 0.0,
            "person_count": None, "sources": [], "explanation": "",
            "precision_level": "none", "ts": "2026-09-03T20:14:00+00:00",
        },
    }

    def list_rooms(self):
        return list(self.ROOMS)

    def room_state(self, room):
        return self.ROOMS.get(room)

    def house_map(self):
        return {}


def main():
    space = SpaceStore(":memory:")
    cores = CoreRegistry(":memory:")
    sp = space.create_space("My Home", "home")
    space.add_person("Alex", ROLE_OWNER)
    space.add_person("Sam", ROLE_ADMIN)
    cores.register("core-laptop-0001", sp.space_id, "Laptop", is_self=True,
                   platform="windows", portable=True, room="Office")
    cores.promote("core-laptop-0001", space.bump_epoch())
    cores.heartbeat("core-laptop-0001")

    server = build_mcp_server(
        Provider(),
        space_fn=lambda: space.get_space().to_dict(),
        cores_fn=lambda: cores.topology(),
        people_count_fn=lambda: len(space.list_people()),
        space_devices_fn=lambda: [])

    async def ask(name, **kw):
        result = await server.call_tool(name, kw)
        # FastMCP returns (content, structured) or a content list depending on
        # version; pull the structured payload either way.
        if isinstance(result, tuple) and len(result) == 2:
            return result[1]
        return result

    async def run():
        print("=" * 66)
        print('AGENT: "What is happening in my home right now?"')
        print("=" * 66)
        ctx = await ask("get_space_context")
        print(json.dumps(ctx, indent=2, default=str)[:1200])

        print()
        print("=" * 66)
        print('AGENT: "Why do you think someone is in the sala?"')
        print("=" * 66)
        why = await ask("explain_room_state", room="sala")
        print(json.dumps(why, indent=2, default=str)[:1000])

        print()
        print("=" * 66)
        print('AGENT: "Which rooms can you actually see?"')
        print("=" * 66)
        cov = await ask("get_sensor_coverage")
        print(json.dumps(cov, indent=2, default=str)[:800])

        print()
        print("=" * 66)
        print("PERMISSION BOUNDS")
        print("=" * 66)
        blob = json.dumps([ctx, why, cov], default=str)
        for forbidden, what in (("breathing_bpm", "vital signs"),
                                ("Alex", "a person's name"),
                                ("posture", "per-person posture"),
                                ('"x"', "per-person position")):
            leaked = forbidden in blob
            print(f"  {what:24} leaked: {leaked}")
            assert not leaked, f"{what} reached the agent"

        scopes = effective_tool_scopes("agent", None)
        print()
        print("  a DEFAULT agent may call:")
        for t in sorted(scopes):
            print(f"    + {t}")
        denied = [t for t in ("get_device_context", "get_network_inventory",
                              "query_occupancy_history", "get_house_map",
                              "call_ha_service")
                  if not tool_call_allowed(scopes, t)]
        print("  and is denied:")
        for t in denied:
            print(f"    - {t}")
        assert "get_device_context" in denied
        assert "call_ha_service" in denied
        print()
        print("OK: structured, explainable, permission-bounded.")

    asyncio.run(run())
    space.close()
    cores.close()


if __name__ == "__main__":
    main()
