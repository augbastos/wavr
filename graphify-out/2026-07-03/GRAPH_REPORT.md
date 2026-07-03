# Graph Report - wavr  (2026-07-03)

## Corpus Check
- 151 files · ~152,724 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 95 nodes · 172 edges · 10 communities (5 shown, 5 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 2 edges (avg confidence: 0.8)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `689a85c9`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_OrbitControls.js|OrbitControls.js]]
- [[_COMMUNITY_test_app.py|test_app.py]]
- [[_COMMUNITY_onTouchMove|onTouchMove]]
- [[_COMMUNITY_OrbitControls|OrbitControls]]
- [[_COMMUNITY_app.py|app.py]]
- [[_COMMUNITY_._handleMouseWheel|._handleMouseWheel]]
- [[_COMMUNITY_onMouseDown|onMouseDown]]
- [[_COMMUNITY_onMouseMove|onMouseMove]]

## God Nodes (most connected - your core abstractions)
1. `OrbitControls` - 49 edges
2. `build_client()` - 15 edges
3. `onTouchMove()` - 7 edges
4. `onTouchStart()` - 6 edges
5. `create_app()` - 5 edges
6. `onMouseDown()` - 4 edges
7. `onMouseMove()` - 4 edges
8. `_default_sources()` - 3 edges
9. `onPointerDown()` - 3 edges
10. `onMouseWheel()` - 3 edges

## Surprising Connections (you probably didn't know these)
- `build_client()` --calls--> `create_app()`  [INFERRED]
  backend/tests/test_app.py → backend/wavr/app.py
- `test_is_loopback_helper_rejects_non_loopback()` --calls--> `_is_loopback()`  [INFERRED]
  backend/tests/test_app.py → backend/wavr/app.py

## Import Cycles
- None detected.

## Communities (10 total, 5 thin omitted)

### Community 0 - "OrbitControls.js"
Cohesion: 0.10
Nodes (11): _changeEvent, _endEvent, onKeyDown(), onPointerDown(), onPointerUp(), _plane, _ray, _startEvent (+3 more)

### Community 1 - "test_app.py"
Cohesion: 0.26
Nodes (14): build_client(), test_bad_host_header_returns_400(), test_get_house_returns_rooms(), test_history_returns_roomstate_list(), test_non_loopback_http_peer_gets_403(), test_root_serves_dashboard_html(), test_source_toggle_disables_named_source(), test_state_change_without_local_header_is_rejected() (+6 more)

### Community 4 - "app.py"
Cohesion: 0.27
Nodes (9): test_is_loopback_helper_rejects_non_loopback(), _camera_factory(), create_app(), _default_sources(), _is_loopback(), _mask_rtsp(), Plano A real-source set: network always-on ($0), ruview always-on (harmless, Redact the password in an rtsp URL for API responses: rtsp://user:pw@host -> rts (+1 more)

## Knowledge Gaps
- **8 isolated node(s):** `_changeEvent`, `_startEvent`, `_endEvent`, `_ray`, `_plane` (+3 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **5 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `OrbitControls` connect `OrbitControls` to `OrbitControls.js`, `onTouchMove`, `.update`, `._handleMouseWheel`, `onMouseDown`, `onMouseMove`, `.pan`?**
  _High betweenness centrality (0.407) - this node is a cross-community bridge._
- **Why does `build_client()` connect `test_app.py` to `app.py`?**
  _High betweenness centrality (0.018) - this node is a cross-community bridge._
- **Why does `onTouchMove()` connect `onTouchMove` to `OrbitControls.js`, `.update`?**
  _High betweenness centrality (0.012) - this node is a cross-community bridge._
- **What connects `Plano A real-source set: network always-on ($0), ruview always-on (harmless`, `Redact the password in an rtsp URL for API responses: rtsp://user:pw@host -> rts`, `_changeEvent` to the rest of the system?**
  _10 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `OrbitControls.js` be split into smaller, more focused modules?**
  _Cohesion score 0.1 - nodes in this community are weakly interconnected._