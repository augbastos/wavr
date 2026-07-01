# Wavr — Extension seams

- **Sub-plan B (real sources):** add `NetworkSource` / `RuViewSource` as more `SensorSource`
  implementations, then register them in `create_app`'s `sources` list (or via
  `SourceManager.register`). The manager already runs one task per source and fans them into the
  shared `_ingest` → FusionEngine → storage → hub — no merge code needed. FusionEngine and
  dashboard are unchanged.
- **Sub-plan C (camera + CV):** `CameraSource` (RTSP + YOLO) registered with `enabled=False` so it
  never starts at boot (safe default). Enabling is runtime via `POST /api/sources/{name}/toggle`
  (already CSRF-guarded by the `X-Wavr-Local` header). The camera MUST release RTSP + stop YOLO in
  its generator's `finally` — `SourceManager._run` triggers it via `agen.aclose()` on disable — and
  should read frames in a cancellation-responsive worker it can join, so a stalled read can't
  outlive a disable. Only derived events persist; never frames. Toggle state is in-memory: cameras
  always boot OFF (safe), no persisted ON.
- **Camada 2/3 (rules, away):** subscribe via `Hub.subscribe()` and register the subscriber as a
  task inside `create_app`'s `lifespan`. React to RoomState; emit MQTT to localhost:1883.
- **Camada 4 (AI narration):** read `GET /api/state` (latest RoomState per room) + `GET /api/history`.
- **Network granularity:** `network` is a house-level signal (a "casa" pseudo-room) — a weak hint
  that does NOT corroborate specific rooms in A/B; folding it as a per-room prior is a later refinement.
- **Deferred:** Supabase history for Plano B (intentionally not built — less surface, safer).
