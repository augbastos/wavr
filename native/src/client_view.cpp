#include "client_view.h"

#include <cmath>

namespace wavr {

namespace {

// Each helper is its Python namesake in client_view.py: the value when it has
// exactly the right type, otherwise the "unknown" form. Never coerced.
json str_or_null(const json& o, const char* k) {
  auto it = o.find(k);
  return it != o.end() && it->is_string() ? *it : json(nullptr);
}

json num_or_null(const json& o, const char* k) {
  auto it = o.find(k);
  if (it == o.end() || !it->is_number()) return nullptr;   // bool is not a number here
  if (it->is_number_float() && !std::isfinite(it->get<double>())) return nullptr;
  return *it;
}

json int_or_null(const json& o, const char* k) {
  auto it = o.find(k);
  return it != o.end() && it->is_number_integer() ? *it : json(nullptr);
}

json bool_or_null(const json& o, const char* k) {
  auto it = o.find(k);
  return it != o.end() && it->is_boolean() ? *it : json(nullptr);
}

bool is_true(const json& o, const char* k) {
  auto it = o.find(k);
  return it != o.end() && it->is_boolean() && it->get<bool>();
}

// The elements of o[k] that are objects (or strings), when o[k] is a list.
json objects(const json& o, const char* k) {
  json out = json::array();
  auto it = o.find(k);
  if (it != o.end() && it->is_array()) {
    for (const auto& x : *it) {
      if (x.is_object()) out.push_back(x);
    }
  }
  return out;
}

json strings(const json& o, const char* k) {
  json out = json::array();
  auto it = o.find(k);
  if (it != o.end() && it->is_array()) {
    for (const auto& x : *it) {
      if (x.is_string()) out.push_back(x);
    }
  }
  return out;
}

json runtime_view(const json& rt) {
  if (!rt.is_object() || !str_or_null(rt, "state").is_string()) return nullptr;
  json findings = json::array();
  for (const auto& f : objects(rt, "findings")) {
    findings.push_back({{"key", str_or_null(f, "key")}, {"state", str_or_null(f, "state")},
                        {"text", str_or_null(f, "text")}, {"detail", str_or_null(f, "detail")}});
  }
  return {{"state", str_or_null(rt, "state")},       {"headline", str_or_null(rt, "headline")},
          {"space", str_or_null(rt, "space")},       {"role", str_or_null(rt, "role")},
          {"uptime_s", num_or_null(rt, "uptime_s")},
          {"last_state_age_s", num_or_null(rt, "last_state_age_s")},
          {"findings", findings}};
}

json attention_view(const json& a) {
  if (!a.is_object() || int_or_null(a, "total").is_null()) return nullptr;
  json items = json::array();
  for (const auto& i : objects(a, "items")) {
    items.push_back({{"key", str_or_null(i, "key")},     {"band", str_or_null(i, "band")},
                     {"title", str_or_null(i, "title")}, {"detail", str_or_null(i, "detail")},
                     {"where", str_or_null(i, "where")}, {"action", str_or_null(i, "action")},
                     {"since", str_or_null(i, "since")}, {"count", int_or_null(i, "count")}});
  }
  return {{"total", int_or_null(a, "total")},       {"blocking", int_or_null(a, "blocking")},
          {"degraded", int_or_null(a, "degraded")}, {"info", int_or_null(a, "info")},
          {"headline", str_or_null(a, "headline")},
          {"could_not_check", strings(a, "could_not_check")},
          {"items", items}};
}

json room_view(const std::string& name, const json& r) {
  json sources = json::array();
  for (const auto& s : objects(r, "sources")) {
    sources.push_back({{"modality", str_or_null(s, "modality")},
                       {"sensor_id", str_or_null(s, "sensor_id")},
                       {"presence", bool_or_null(s, "presence")},
                       {"confidence", num_or_null(s, "confidence")},
                       {"age_s", num_or_null(s, "age_s")},
                       {"health", str_or_null(s, "health")},
                       {"count", int_or_null(s, "count")}});
  }
  return {{"room", name},
          {"occupied", bool_or_null(r, "occupied")},
          {"confidence", num_or_null(r, "confidence")},
          {"person_count", int_or_null(r, "person_count")},
          {"precision_level", str_or_null(r, "precision_level")},
          {"explanation", str_or_null(r, "explanation")},
          {"ts", str_or_null(r, "ts")},
          {"watch", is_true(r, "watch")},
          {"unrecognized", is_true(r, "unrecognized")},
          {"sources", sources}};
}

}  // namespace

json client_snapshot(const json& runtime, const json& attention, const json& state,
                     bool reachable, const std::string& error) {
  json rt = reachable ? runtime_view(runtime) : json(nullptr);
  json att = reachable ? attention_view(attention) : json(nullptr);
  const bool rooms_readable = reachable && state.is_object();
  json rooms = json::array();
  bool watch = false;
  if (rooms_readable) {
    for (const auto& [name, room] : state.items()) {   // std::map: sorted, like sorted()
      if (!room.is_object()) continue;
      rooms.push_back(room_view(name, room));
      watch = watch || rooms.back()["watch"].get<bool>();
    }
  }
  int code;
  if (rt.is_null()) {
    code = kExitUnreachable;
  } else {
    json raw_att = att.is_null() ? json(nullptr)
                                 : json{{"total", att["total"]},
                                        {"could_not_check", att["could_not_check"]}};
    code = status_exit_code(json{{"state", rt["state"]}}, raw_att);
  }
  return {{"schema", kClientViewSchema},
          {"reachable", reachable && !rt.is_null()},
          {"error", error.empty() ? json(nullptr) : json(error)},
          {"runtime", rt},
          {"attention", att},
          {"rooms", rooms},
          {"rooms_readable", rooms_readable},
          {"privacy", {{"watch", watch}}},
          {"exit_code", code}};
}

}  // namespace wavr

#include "net.h"

namespace wavr {

json client_fetch(const std::string& url_text, const std::string& token, const std::string& pin,
                  int timeout_ms) {
  auto url = net::parse_url(url_text);
  if (!url) return client_snapshot(nullptr, nullptr, nullptr, false, "not an http(s) URL");
  auto get = [&](const char* path) {
    net::Request req;
    req.path = path;
    req.headers = {{"X-Wavr-Local", "1"}};
    if (!token.empty()) req.headers.emplace_back("X-Wavr-Token", token);
    return net::send(*url, req, pin.empty() ? net::Tls::Unverified : net::Tls::Pin, pin,
                     timeout_ms);
  };
  auto body = [](const net::Result& r) {
    return r.ok && r.status == 200 ? json::parse(r.body, nullptr, false) : json(nullptr);
  };
  net::Result rt = get("/api/runtime");
  if (!rt.ok) return client_snapshot(nullptr, nullptr, nullptr, false, rt.error);
  if (rt.status != 200) {
    return client_snapshot(nullptr, nullptr, nullptr, false,
                           "the Core answered HTTP " + std::to_string(rt.status));
  }
  json runtime = body(rt);
  json attention = body(get("/api/attention"));
  json state = body(get("/api/state"));
  // A discarded parse is "not read", the same as no answer.
  auto clean = [](json j) { return j.is_discarded() ? json(nullptr) : j; };
  return client_snapshot(clean(runtime), clean(attention), clean(state), true, "");
}

}  // namespace wavr
