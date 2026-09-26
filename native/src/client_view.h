// The native client view model: backend/wavr/client_view.py::snapshot, held
// to it by conformance/client_view.json. It projects the Core's answers; it
// never decides. See docs/NATIVE-CLIENT.md.
#pragma once

#include <string>

#include "semantics.h"

namespace wavr {

constexpr int kClientViewSchema = 1;

// runtime / attention / state: the parsed bodies of GET /api/runtime,
// /api/attention and /api/state (null where not read). `reachable` false means
// the Core did not answer; `error` is the transport's sentence, or empty.
json client_snapshot(const json& runtime, const json& attention, const json& state,
                     bool reachable, const std::string& error);

}  // namespace wavr

namespace wavr {

// Fetch the three answers from a Core and build the snapshot. `pin` empty =
// no pinning, which net::send allows only for a loopback host. Blocking; the
// timeout applies to each of the three requests.
json client_fetch(const std::string& url, const std::string& token, const std::string& pin,
                  int timeout_ms = 6000);

}  // namespace wavr
