// `wavr` -- the native command line. No GUI, no browser, no Python:
//
//   wavr status [--url URL] [--token T] [--pin FP] [--json] [-q] [--ascii]
//   wavr snapshot [--url URL] [--token T] [--pin FP]      (docs/NATIVE-CLIENT.md)
//   wavr doctor [--url URL] [--token T] [--pin FP]
//   wavr capabilities
//   wavr node enroll --url https://CORE:8000 --code CODE [--state FILE]
//   wavr node run --sensor ld2450:PORT|replay:FILE|stdin [--state FILE] [--seconds N]
//   wavr node reactivate [--state FILE]
//   wavr node status [--state FILE]
//   wavr version
//
// `status` renders and exits exactly as backend/wavr/status.py does (checked
// against conformance/status.json); the Core decides every answer.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "capabilities.h"
#include "client_view.h"
#include "generated/unreachable.h"
#include "net.h"
#include "node.h"
#include "semantics.h"
#include "wavr/wavr.h"

#if defined(_WIN32)
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <io.h>
#  include <windows.h>
#else
#  include <langinfo.h>
#  include <locale.h>
#  include <unistd.h>
#endif

#ifndef WAVR_NATIVE_VERSION
#  define WAVR_NATIVE_VERSION "0.0.0"
#endif

namespace {

using ojson = nlohmann::ordered_json;   // keeps the Core's key order in --json

struct Args {
  std::vector<std::string> positional;
  std::map<std::string, std::string> opts;
  bool has(const std::string& k) const { return opts.count(k) > 0; }
  std::string get(const std::string& k, const std::string& d = "") const {
    auto it = opts.find(k);
    return it == opts.end() ? d : it->second;
  }
};

Args parse(int argc, char** argv, int from) {
  static const char* kFlags[] = {"--json", "-q", "--quiet", "--ascii"};
  Args a;
  for (int i = from; i < argc; ++i) {
    std::string s = argv[i];
    bool flag = false;
    for (const char* f : kFlags) flag = flag || s == f;
    if (flag) {
      a.opts[s == "-q" ? "--quiet" : s] = "1";
    } else if (s.rfind("--", 0) == 0 && i + 1 < argc) {
      a.opts[s] = argv[++i];
    } else {
      a.positional.push_back(s);
    }
  }
  return a;
}

std::string env(const char* k) {
  const char* v = std::getenv(k);
  return v ? v : "";
}

// status.default_url(): http unless LAN mode is on, on the Core's own port.
std::string default_url() {
  if (!env("WAVR_DOCTOR_URL").empty()) return env("WAVR_DOCTOR_URL");
  std::string md = env("WAVR_MULTIDEVICE");
  for (auto& c : md) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  bool https = md == "1" || md == "true" || md == "yes";
  std::string port = env("WAVR_PORT").empty() ? "8000" : env("WAVR_PORT");
  return std::string(https ? "https" : "http") + "://127.0.0.1:" + port;
}

// status.marks_for(): round marks only where the stream can show them, so that
// states which differ never collapse into one "?" on a legacy code page.
bool stdout_takes_utf8() {
#if defined(_WIN32)
  if (!_isatty(_fileno(stdout))) return false;    // a pipe: the cron job, the monitor
  return SetConsoleOutputCP(CP_UTF8) != 0;
#else
  setlocale(LC_CTYPE, "");
  const char* cs = nl_langinfo(CODESET);
  return cs && (std::strcmp(cs, "UTF-8") == 0 || std::strcmp(cs, "utf8") == 0);
#endif
}

wavr::net::Result get(const wavr::net::Url& url, const std::string& path,
                      const std::string& token, const std::string& pin) {
  wavr::net::Request req;
  req.path = path;
  req.headers = {{"X-Wavr-Local", "1"}};
  if (!token.empty()) req.headers.emplace_back("X-Wavr-Token", token);
  auto tls = pin.empty() ? wavr::net::Tls::Unverified : wavr::net::Tls::Pin;
  return wavr::net::send(url, req, tls, pin);
}

// A URL as it may be printed: anything before an '@' in the authority (a
// user name and password someone pasted) is replaced, never echoed.
std::string printable_url(const std::string& url) {
  const size_t scheme = url.find("://");
  const size_t start = scheme == std::string::npos ? 0 : scheme + 3;
  const size_t end = url.find_first_of("/?#", start);
  const size_t at = url.rfind('@', end == std::string::npos ? url.size() : end);
  if (at == std::string::npos || at < start) return url;
  return url.substr(0, start) + "***@" + url.substr(at + 1);
}

int cmd_status(const Args& a) {
  const std::string url_text = a.get("--url", default_url());
  const std::string token = a.get("--token", env("WAVR_LOCAL_TOKEN"));
  const bool as_json = a.has("--json"), quiet = a.has("--quiet");
  auto url = wavr::net::parse_url(url_text);

  wavr::net::Result r;
  ojson runtime;
  if (!url) {
    r.error = "not an http(s) URL";
  } else {
    r = get(*url, "/api/runtime", token, a.get("--pin"));
    if (r.ok && r.status != 200) r.error = "HTTP " + std::to_string(r.status);
    if (r.ok && r.status == 200) runtime = ojson::parse(r.body, nullptr, false);
    if (r.ok && r.status == 200 && !runtime.is_object()) r.error = "not a Wavr runtime answer";
  }
  if (!r.error.empty() || !runtime.is_object()) {
    if (as_json) {
      ojson out = {{"runtime", ojson::parse(WAVR_UNREACHABLE_RUNTIME_JSON)},
                   {"attention", nullptr}, {"error", r.error}, {"url", printable_url(url_text)}};
      std::cout << wavr::dump(out, 2, true) << "\n";
    } else if (!quiet) {
      std::cerr << "Wavr is not answering at " << printable_url(url_text) << ".\n  " << r.error
                << "\n";
      if (url && wavr::net::is_loopback_host(url->host)) {
        std::cerr << "  Start it with:  python -m wavr.serve\n";
      } else {
        std::cerr << "  This is not a loopback address, so the certificate must be\n"
                     "  pinned: --pin with the fingerprint the Core shows.\n";
      }
    }
    return wavr::kExitUnreachable;
  }
  ojson attention = nullptr;
  auto ra = get(*url, "/api/attention", token, a.get("--pin"));
  if (ra.ok && ra.status == 200) {
    attention = ojson::parse(ra.body, nullptr, false);
    // status.py: an answer of the wrong shape is "could not check".
    if (!attention.is_object()) attention = nullptr;
  }
  if (as_json) {
    std::cout << wavr::dump(ojson{{"runtime", runtime}, {"attention", attention}}, 2, true)
              << "\n";
  } else if (!quiet) {
    bool ascii = a.has("--ascii") || !stdout_takes_utf8();
    std::cout << wavr::render_status(wavr::json::parse(wavr::dump(runtime)),
                                     wavr::json::parse(wavr::dump(attention)), ascii)
              << "\n";
  }
  wavr::json rt = wavr::json::parse(wavr::dump(runtime)), at = wavr::json::parse(wavr::dump(attention));
  int code = wavr::status_exit_code(rt, at);
  const std::string st = wavr::str_field(rt, "state");
  if (code == wavr::kExitAttention && !quiet && !as_json && st != "degraded" &&
      st != "attention" && !(at.is_object() && wavr::truthy(at.value("total", wavr::json())))) {
    std::cerr << "Wavr could not read everything that might need you, so this is not "
                 "a clean bill of health.\n";
  }
  return code;
}

int cmd_node(const Args& a) {
  if (a.positional.empty()) {
    std::cerr << "usage: wavr node enroll|run|reactivate|status ...\n";
    return wavr::kNodeUsage;
  }
  wavr::NodeOptions opt;
  opt.state_path = a.get("--state", opt.state_path);
  opt.sensor = a.get("--sensor");
  if (a.has("--seconds")) opt.run_seconds = std::atof(a.get("--seconds").c_str());
  // Tuning knobs for tests and slow links. Floors, because a typo (atoi -> 0)
  // must not turn the loop into one that hammers the Core without pause.
  auto ms = [&](const char* k, int dflt, int floor) {
    return a.has(k) ? std::max(floor, std::atoi(a.get(k).c_str())) : dflt;
  };
  opt.telemetry_ms = ms("--telemetry-ms", opt.telemetry_ms, 50);
  opt.heartbeat_ms = ms("--heartbeat-ms", opt.heartbeat_ms, 100);
  opt.disabled_heartbeat_ms = ms("--disabled-heartbeat-ms", opt.disabled_heartbeat_ms, 100);
  const std::string& sub = a.positional[0];
  if (sub == "enroll") {
    if (!a.has("--url") || !a.has("--code")) {
      std::cerr << "usage: wavr node enroll --url https://CORE:PORT --code CODE\n";
      return wavr::kNodeUsage;
    }
    return wavr::node_enroll(a.get("--url"), a.get("--code"), opt);
  }
  if (sub == "run") {
    if (opt.sensor.empty()) {
      std::cerr << "usage: wavr node run --sensor ld2450:PORT|replay:FILE\n";
      return wavr::kNodeUsage;
    }
    return wavr::node_run(opt);
  }
  if (sub == "reactivate") return wavr::node_reactivate(opt);
  if (sub == "status") return wavr::node_status(opt);
  std::cerr << "wavr node: unknown command " << sub << "\n";
  return wavr::kNodeUsage;
}

// `wavr snapshot`: the native client view model as JSON. The exit code is the
// snapshot's own, i.e. `wavr status`'s: 0 / 1 / 2.
int cmd_snapshot(const Args& a) {
  wavr::json snap = wavr::client_fetch(a.get("--url", default_url()),
                                       a.get("--token", env("WAVR_LOCAL_TOKEN")), a.get("--pin"));
  std::cout << wavr::dump(snap, 2, true) << "\n";
  return snap["exit_code"].get<int>();
}

// `wavr doctor` (backend/wavr/doctor.py): print the Core's diagnostic report.
// Exit 0 printed, 2 could not reach the Core, 3 it answered without a report.
int cmd_doctor(const Args& a) {
  const std::string url_text =
      a.get("--url", env("WAVR_DOCTOR_URL").empty() ? "https://127.0.0.1:8000"
                                                    : env("WAVR_DOCTOR_URL"));
  const std::string token = a.get("--token", env("WAVR_LOCAL_TOKEN"));
  auto url = wavr::net::parse_url(url_text);
  wavr::net::Result r;
  if (url) {
    wavr::net::Request req;
    req.path = "/api/health/doctor";
    req.headers = {{"X-Wavr-Local", "1"}};
    if (!token.empty()) req.headers.emplace_back("Authorization", "Bearer " + token);
    const std::string pin = a.get("--pin");
    r = wavr::net::send(*url, req, pin.empty() ? wavr::net::Tls::Unverified : wavr::net::Tls::Pin,
                        pin, 40000);
  } else {
    r.error = "not an http(s) URL";
  }
  if (!r.ok || r.status != 200) {
    std::cerr << "wavr doctor: couldn't reach the Core at " << printable_url(url_text) << " ("
              << (r.ok ? "HTTP " + std::to_string(r.status) : r.error) << ").\n"
              << "Is it running? Start it with:  python -m wavr.serve\n";
    return 2;
  }
  wavr::json data = wavr::json::parse(r.body, nullptr, false);
  const std::string report = wavr::str_field(data, "report");
  if (report.empty()) {
    std::cerr << "wavr doctor: the Core responded but sent no report (is it up to date?).\n";
    return 3;
  }
  std::cout << report << "\n";
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  const std::string cmd = argc > 1 ? argv[1] : "";
  if (cmd == "status") return cmd_status(parse(argc, argv, 2));
  if (cmd == "node") return cmd_node(parse(argc, argv, 2));
  if (cmd == "snapshot") return cmd_snapshot(parse(argc, argv, 2));
  if (cmd == "doctor") return cmd_doctor(parse(argc, argv, 2));
  if (cmd == "capabilities") {
    std::cout << wavr::dump(wavr::probe_manifest(), 2, true) << "\n";
    return 0;
  }
  if (cmd == "version") {
    std::cout << "wavr native " << WAVR_NATIVE_VERSION << " (C ABI " << WAVR_ABI_VERSION_MAJOR
              << "." << WAVR_ABI_VERSION_MINOR << ")\n";
    return 0;
  }
  std::cerr << "usage: wavr status | snapshot | doctor | capabilities | node ... | version\n";
  return cmd.empty() || cmd == "help" || cmd == "--help" ? 0 : 64;
}
