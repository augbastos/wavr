# Wavr Core on the Xiaomi 12T Pro — Design

**Date:** 2026-07-06
**Status:** Approved (Augusto, 2026-07-06)
**Depends on:** [Wavr product taxonomy](2026-07-06-wavr-product-taxonomy-design.md)
**Scope:** Turn a dedicated Xiaomi 12T Pro (model 22081212UG, 8GB physical + 4GB virtual
RAM, 256GB UFS 3.1, Snapdragon 8+ Gen 1) into a working **Wavr Core** — the always-on,
admin-by-nature appliance that centralizes everything, running the real Wavr backend on
the phone itself.

## Goal

Prove the Core is real: the Wavr backend runs on the phone, other devices (Desktop,
Mobile) connect to *it*, and the phone — placed at a strategic spot in the room — uses
its **own cameras** as a live presence sensor. Push toward maximum functionality; roll
back any module that overloads the device. The battery/thermal meter on screen is how the
operator *sees* the overload.

## Device fit (verified)

Raw compute **exceeds** the requirement — the SD 8+ Gen 1 beats the Raspberry Pi tier the
Wavr targets. The limiter is the **Android sandbox**, not power:
- **RAM:** 8GB physical + 4GB virtual (Xiaomi RAM-expansion = UFS swap; anti-OOM buffer,
  not a workhorse). Discipline: **duty-cycle** — never run YOLO + LLM + backend flat-out
  at once. The LLM working set must fit real RAM (small model).
- **CV:** torch ARM64 **CPU-only** (Adreno unusable by stock torch). Nano model, low fps,
  duty-cycled. Sustained inference = heat → the meter + duty-cycler back off.
- **Network recon:** high-level scans (mDNS/SSDP/port) work unrooted; **ARP sweep +
  passive DHCP need root** → deferred to the root-gated phase.
- **BLE presence:** hardest gap — Termux/proot can't reach the Android BT stack easily.
  Defer or bridge later.

## The four modules

1. **Central backend** — Termux native Python runs the FastAPI backend. Centralizes
   network + presence + dashboard + MCP. Desktop/Mobile connect to it (multidevice
   HTTPS/pairing already built). Lightweight.

2. **CV — two camera sources** — (a) LAN IP cameras (the Tapo, via RTSP) and (b) the
   **phone's own camera**, exposed by an on-device IP-camera app as an MJPEG/RTSP stream
   on `127.0.0.1`, which Wavr's existing camera source reads like any IP camera — **zero
   new backend code**, full existing CV pipeline. YOLO runs in a proot Ubuntu guest
   (torch CPU, nano, duty-cycled). **ADR-0002 holds**: frames live in memory only, never
   persisted. Placing the phone at a strategic spot gives instant room coverage.

3. **Local agent** — llama.cpp with a 1–3B Q4 model (Llama-3.2-3B / Qwen2.5-3B),
   **on-demand only**. Becomes the "Ollama-local" provider for the narrator → the Core is
   a self-contained sovereign brain: sensing + fusion + dashboard + MCP + a local LLM,
   **zero egress**. Not always resident; loads when asked.

4. **Core runtime** — always-on (Termux:Boot autostart + `termux-wake-lock` + battery-opt
   disabled) and an **on-screen battery/thermal widget**: `termux-battery-status` →
   `/api/battery` → dashboard shows percentage + plugged state + **temperature** + a
   "return to the outlet at X%" alert. The Core is ideally always plugged; because this is
   a phone it may leave the outlet, so the meter tells the operator when to re-plug. The
   temperature readout doubles as the overload signal feeding the duty-cycler and the
   rollback decision.

## Phased build

- **Phase 0 — base, no root (today):** Termux (F-Droid) + Termux:Boot + Termux:API →
  `pkg install python git termux-api` → transfer the Wavr code to the phone **over LAN via
  a git bundle (no public push)** → backend up in Termux (CV off) → Desktop connects to
  it. Proves the Core centralizes. Do this near the PC for easy debugging, then move the
  phone to the strategic spot.
- **Phase 1 — CV:** proot-distro Ubuntu + torch (CPU) + ultralytics; phone-camera via
  IP-camera app; tune nano model / fps / duty-cycle against the thermal meter.
- **Phase 2 — local agent:** llama.cpp + 1–3B model, wired as the narrator's local
  provider.
- **Phase 3 — always-on + root-gated (parallel):** Termux:Boot + wake-lock; bootloader
  unlock → Magisk → root-gated ARP/DHCP recon (Xiaomi unlock has a waiting period and
  wipes the phone — run the timer in parallel). BLE bridge = research.

## Invariants (carried from the taxonomy + Wavr privacy model)

- **Core is admin by nature** — this device is the network authority.
- **No CV frame persistence (ADR-0002)** — the phone-camera path keeps frames in memory
  only, same as every other camera source.
- **Rollback discipline** — any module that overloads the device (RAM thrash, thermal
  throttle) gets duty-cycled down or disabled. The on-screen meter makes overload visible.
- **Local + mute by default** — the local agent means the Core can narrate/reason with
  zero cloud egress; the single outbound door stays the MCP/Connectors surface.

## Out of scope

Firmware for Wavr Nodes; the appliance-productization step (12T Pro is a prototype to
learn what a real Core needs); iOS/mac anything. Each gets its own spec when the trigger
arrives.
