#include "client_commands.h"

#include <cstdio>
#include <exception>
#include <string>

#include "net.h"

namespace wavr {

namespace {

const char* const kTable =
#include "client_commands_table.inc"
    ;

json error_code(const char* code) { return json{{"error", code}}; }

bool type_ok(const std::string& type, const json& v) {
  if (type == "string") return v.is_string();
  if (type == "bool") return v.is_boolean();
  if (type == "int") return v.is_number_integer();
  if (type == "strings") {
    if (!v.is_array()) return false;
    for (const auto& x : v)
      if (!x.is_string()) return false;
    return true;
  }
  return true;  // "any"
}

// RFC 3986 unreserved characters stay; every other byte of the UTF-8 is
// %XX-escaped (uppercase), exactly like Python's quote(v, safe="").
std::string segment(const std::string& v) {
  static const char* hex = "0123456789ABCDEF";
  std::string out;
  for (unsigned char c : v) {
    bool keep = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
                c == '-' || c == '.' || c == '_' || c == '~';
    if (keep) {
      out += static_cast<char>(c);
    } else {
      out += '%';
      out += hex[c >> 4];
      out += hex[c & 15];
    }
  }
  return out;
}

const char* error_kind(int status) {
  switch (status) {
    case 400: return "invalid";
    case 401: return "unauthorized";
    case 403: return "forbidden";
    case 404: return "not_found";
    case 409: return "conflict";
    case 422: return "invalid";
    case 423: return "locked";
    case 429: return "throttled";
    default: return status >= 500 ? "server" : "refused";
  }
}

}  // namespace

const json& command_table() {
  static const json table = json::parse(kTable);
  return table;
}

json command_request(const json& name, const json& args) {
  if (!name.is_string()) return error_code("unknown_command");
  const auto& commands = command_table()["commands"];
  auto it = commands.find(name.get<std::string>());
  if (it == commands.end()) return error_code("unknown_command");
  const json& spec = *it;
  if (!args.is_object()) return error_code("not_object");
  const json& specs = spec["args"];
  for (auto a = args.begin(); a != args.end(); ++a)   // ordered map: sorted keys
    if (!specs.contains(a.key())) return error_code("unknown_argument");

  json body = json::object();
  json path_values = json::object();
  for (auto s = specs.begin(); s != specs.end(); ++s) {   // sorted, as the Python
    const std::string& key = s.key();
    const json& a = s.value();
    auto v = args.find(key);
    if (v == args.end() || v->is_null()) {
      if (a["required"].get<bool>()) return error_code("missing_argument");
      continue;
    }
    if (!type_ok(a["type"].get<std::string>(), *v)) return error_code("wrong_type");
    if (a["in"] == "path") {
      const std::string text = v->get<std::string>();
      if (text.empty() || text == "." || text == "..") return error_code("bad_path_segment");
      path_values[key] = segment(text);
    } else {
      body[key] = *v;
    }
  }
  std::string tmpl = spec["path"].get<std::string>();
  std::string path;
  for (size_t i = 0; i < tmpl.size();) {
    if (tmpl[i] == '{') {
      size_t close = tmpl.find('}', i);
      path += path_values[tmpl.substr(i + 1, close - i - 1)].get<std::string>();
      i = close + 1;
    } else {
      path += tmpl[i++];
    }
  }
  const std::string method = spec["method"].get<std::string>();
  return json{{"request",
               {{"method", method},
                {"path", path},
                {"body", method == "GET" ? json(nullptr) : body},
                {"auth", spec["auth"]}}}};
}

json command_result(int status, const std::string& body_text) {
  json data = body_text.empty() ? json(nullptr) : json::parse(body_text, nullptr, false);
  if (data.is_discarded()) data = nullptr;
  if (status >= 200 && status < 300)
    return json{{"ok", true}, {"status", status}, {"error", nullptr}, {"detail", nullptr},
                {"data", data}};
  json detail = nullptr;
  if (data.is_object() && data.contains("detail") && data["detail"].is_string())
    detail = data["detail"];
  return json{{"ok", false}, {"status", status}, {"error", error_kind(status)},
              {"detail", detail}, {"data", data}};
}

json command_transport_failure(const std::string& message) {
  return json{{"ok", false}, {"status", nullptr}, {"error", "unreachable"},
              {"detail", message}, {"data", nullptr}};
}

json command_run(const std::string& url_text, const std::string& token, const std::string& pin,
                 const std::string& name, const std::string& args_json, int timeout_ms) {
  try {
    json args = args_json.empty() ? json::object() : json::parse(args_json, nullptr, false);
    json call = args.is_discarded() ? error_code("not_object") : command_request(name, args);
    if (call.contains("error"))
      return json{{"ok", false}, {"status", nullptr}, {"error", "bad_call"},
                  {"detail", call["error"]}, {"data", nullptr}};
    const json& r = call["request"];
    auto url = net::parse_url(url_text);
    if (!url) return command_transport_failure("not an http(s) URL");
    net::Request req;
    req.method = r["method"].get<std::string>();
    req.path = r["path"].get<std::string>();
    // X-Wavr-Local is the loopback dashboard's CSRF proof; a LAN peer ignores it.
    req.headers = {{"X-Wavr-Local", "1"}};
    // Onboarding routes are reached before a token exists; never send one there.
    if (r["auth"] == "token" && !token.empty())
      req.headers.emplace_back("Authorization", "Bearer " + token);
    if (!r["body"].is_null()) {
      req.headers.emplace_back("Content-Type", "application/json");
      req.body = dump(r["body"]);
    }
    auto res = net::send(*url, req, pin.empty() ? net::Tls::Unverified : net::Tls::Pin, pin,
                         timeout_ms > 0 ? timeout_ms : 6000);
    if (!res.ok) return command_transport_failure(res.error);
    return command_result(res.status, res.body);
  } catch (const std::exception& e) {
    return command_transport_failure(std::string("internal error: ") + e.what());
  }
}

json probe_core(const std::string& url_text, int timeout_ms) {
  json out = {{"ok", false}, {"https", false}, {"fingerprint", nullptr}, {"status", nullptr},
              {"error", nullptr}};
  auto url = net::parse_url(url_text);
  if (!url) {
    out["error"] = "not an http(s) URL";
    return out;
  }
  out["https"] = url->https;
  if (!url->https) {
    // Nothing to pin; net::send refuses plain HTTP off this machine anyway.
    out["error"] = "not HTTPS: there is no certificate to compare";
    return out;
  }
  net::Request req;
  req.path = "/api/health";
  auto res = net::send(*url, req, net::Tls::Capture, "", timeout_ms > 0 ? timeout_ms : 6000);
  if (!res.peer_fingerprint.empty()) out["fingerprint"] = res.peer_fingerprint;
  if (res.ok) out["status"] = res.status;
  out["ok"] = !res.peer_fingerprint.empty();
  if (!out["ok"].get<bool>()) out["error"] = res.error.empty() ? "no certificate seen" : res.error;
  return out;
}

}  // namespace wavr
