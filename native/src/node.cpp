#include "node.h"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <memory>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "net.h"
#include "semantics.h"
#include "serial.h"

#if !defined(_WIN32)
#  include <sys/stat.h>
#endif

namespace wavr {

namespace {

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

std::atomic<bool> g_stop{false};
void on_signal(int) { g_stop = true; }

long long unix_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

struct State {
  std::string url, node_id, token, pin, state = "active";
  long long seq = 0;
  long long press_count = 0;
};

bool load(const std::string& path, State* s, std::string* error) {
  std::ifstream f(path);
  if (!f) {
    *error = "no node state at " + path + " -- enrol first (wavr node enroll)";
    return false;
  }
  json j = json::parse(f, nullptr, false);
  if (str_field(j, "token").empty() || str_field(j, "url").empty()) {
    *error = path + " is not a node state file";
    return false;
  }
  s->url = str_field(j, "url");
  s->node_id = str_field(j, "node_id");
  s->token = str_field(j, "token");
  s->pin = str_field(j, "pin");
  s->state = str_field(j, "state", "active");
  s->seq = int_field(j, "seq");
  s->press_count = int_field(j, "press_count");
  return true;
}

// Written to a temporary file and moved into place, and readable by the owner
// only: it holds the node's bearer token.
bool save(const std::string& path, const State& s) {
  json j = {{"url", s.url},     {"node_id", s.node_id}, {"token", s.token},
            {"pin", s.pin},     {"state", s.state},     {"seq", s.seq},
            {"press_count", s.press_count}};
  const std::string tmp = path + ".tmp";
  {
    std::ofstream f(tmp, std::ios::trunc);
    if (!f) return false;
    f << j.dump(1) << "\n";
    if (!f) return false;
  }
#if !defined(_WIN32)
  chmod(tmp.c_str(), 0600);
#endif
  std::remove(path.c_str());
  return std::rename(tmp.c_str(), path.c_str()) == 0;
}

net::Result call(const State& s, const std::string& path, const std::string& body) {
  auto url = net::parse_url(s.url);
  net::Result r;
  if (!url) {
    r.error = "bad Core URL in state file";
    return r;
  }
  net::Request req;
  req.method = "POST";
  req.path = path;
  req.headers = {{"Authorization", "Bearer " + s.token},
                 {"Content-Type", "application/json"}};
  req.body = body;
  return net::send(*url, req, net::Tls::Pin, s.pin);
}

// A sensor produces raw LD2450 frames. The node forwards them untouched: the
// Core parses them with the same parse_ld2450_frame as a wired radar, so there
// is no on-device parser to drift (NODE_PROTOCOL.md).
class FrameSource {
 public:
  virtual ~FrameSource() = default;
  virtual void collect(std::vector<std::string>* hex_frames, int budget_ms) = 0;
};

std::string to_hex(const uint8_t* p, size_t n) {
  static const char* k = "0123456789abcdef";
  std::string out;
  out.reserve(n * 2);
  for (size_t i = 0; i < n; ++i) {
    out += k[p[i] >> 4];
    out += k[p[i] & 0xF];
  }
  return out;
}

class SerialFrames : public FrameSource {
 public:
  SerialFrames(std::string port) : port_(std::move(port)) {}
  void collect(std::vector<std::string>* out, int budget_ms) override {
    auto deadline = Clock::now() + std::chrono::milliseconds(budget_ms);
    if (!port_open()) {
      std::this_thread::sleep_for(std::chrono::milliseconds(budget_ms));
      return;
    }
    uint8_t buf[256];
    while (Clock::now() < deadline && out->size() < 32) {
      int left = static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                      deadline - Clock::now()).count());
      long n = serial_->read(buf, sizeof buf, std::max(1, left));
      if (n < 0) {                       // unplugged: the Core sees silence, and decays
        std::cerr << "wavr node: lost " << port_ << "; will retry\n";
        serial_.reset();
        return;
      }
      framer_.feed(buf, static_cast<size_t>(n));
      while (auto f = framer_.next()) out->push_back(to_hex(f->data(), f->size()));
    }
  }

 private:
  bool port_open() {
    if (serial_) return true;
    auto now = Clock::now();
    if (now < retry_at_) return false;
    std::string err;
    serial_ = open_serial(port_, 256000, &err);   // LD2450: 256000 8N1
    if (!serial_) {
      std::cerr << "wavr node: " << err << "\n";
      retry_at_ = now + std::chrono::seconds(10);
    }
    return static_cast<bool>(serial_);
  }
  std::string port_;
  std::unique_ptr<SerialPort> serial_;
  Ld2450Framer framer_;
  Clock::time_point retry_at_{};
};

// Frames read from a file, one hex frame per line, played in a loop at the
// radar's own rate. For tests and demos: it exercises everything but the UART.
class ReplayFrames : public FrameSource {
 public:
  explicit ReplayFrames(std::vector<std::string> frames) : frames_(std::move(frames)) {}
  void collect(std::vector<std::string>* out, int budget_ms) override {
    std::this_thread::sleep_for(std::chrono::milliseconds(budget_ms));
    if (frames_.empty()) return;
    out->push_back(frames_[next_++ % frames_.size()]);
  }

 private:
  std::vector<std::string> frames_;
  size_t next_ = 0;
};

std::unique_ptr<FrameSource> open_sensor(const std::string& spec, std::string* error) {
  if (spec.rfind("ld2450:", 0) == 0) return std::make_unique<SerialFrames>(spec.substr(7));
  if (spec.rfind("replay:", 0) == 0) {
    std::ifstream f(spec.substr(7));
    if (!f) {
      *error = "cannot read " + spec.substr(7);
      return nullptr;
    }
    std::vector<std::string> frames;
    std::string line;
    while (std::getline(f, line)) {
      while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
      if (line.empty() || line[0] == '#') continue;
      if (line.size() != 60) {
        *error = "replay lines must be one 30-byte frame in hex: " + line;
        return nullptr;
      }
      frames.push_back(line);
    }
    return std::make_unique<ReplayFrames>(std::move(frames));
  }
  *error = "unknown sensor '" + spec + "' (use ld2450:<port> or replay:<file>)";
  return nullptr;
}

}  // namespace

int node_enroll(const std::string& url_text, const std::string& code, const NodeOptions& opt) {
  auto url = net::parse_url(url_text);
  if (!url || !url->https) {
    std::cerr << "wavr node: the Core URL must be https:// (nodes need LAN mode)\n";
    return kNodeUsage;
  }
  net::Request req;
  req.method = "POST";
  req.path = "/api/nodes/enroll";
  req.headers = {{"Content-Type", "application/json"}};
  req.body = json{{"code", code}, {"cert_fingerprint", ""}}.dump();
  // The one trust-on-first-use moment of NODE_PROTOCOL.md: no verification,
  // and the certificate presented here becomes the pin for every later call.
  auto r = net::send(*url, req, net::Tls::Capture);
  if (!r.ok) {
    std::cerr << "wavr node: " << r.error << "\n";
    return kNodeFailed;
  }
  if (r.status == 403) {
    std::cerr << "wavr node: the enrolment code is invalid or expired\n";
    return kNodeFailed;
  }
  json body = json::parse(r.body, nullptr, false);
  if (r.status != 200 || str_field(body, "token").empty()) {
    std::cerr << "wavr node: enrolment failed (HTTP " << r.status << ")\n";
    return kNodeFailed;
  }
  State s;
  s.url = url_text;
  s.node_id = str_field(body, "node_id");
  s.token = str_field(body, "token");
  s.pin = r.peer_fingerprint;
  if (!save(opt.state_path, s)) {
    std::cerr << "wavr node: enrolled, but could not write " << opt.state_path << "\n";
    return kNodeFailed;
  }
  std::cout << "Enrolled as node " << s.node_id << ".\nPinned the Core's certificate "
            << s.pin << "\n";
  return kNodeOk;
}

int node_run(const NodeOptions& opt) {
  State s;
  std::string err;
  if (!load(opt.state_path, &s, &err)) {
    std::cerr << "wavr node: " << err << "\n";
    return kNodeUsage;
  }
  auto sensor = open_sensor(opt.sensor, &err);
  if (!sensor) {
    std::cerr << "wavr node: " << err << "\n";
    return kNodeUsage;
  }
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);

  const auto started = Clock::now();
  auto next_heartbeat = started;          // heartbeat first: learn the state
  auto next_save = started + std::chrono::seconds(60);
  auto next_seq = [&] { return s.seq = std::max(s.seq + 1, unix_ms()); };
  int exit_code = kNodeOk;

  while (!g_stop) {
    auto now = Clock::now();
    if (opt.run_seconds > 0 &&
        std::chrono::duration<double>(now - started).count() >= opt.run_seconds) {
      break;
    }
    if (now >= next_heartbeat) {
      auto r = call(s, "/api/nodes/heartbeat", "");
      auto next = heartbeat_next_state(r.ok ? std::optional<int>(r.status) : std::nullopt,
                                       r.body);
      if (!r.ok) std::cerr << "wavr node: heartbeat: " << r.error << "\n";
      if (next == NodeNext::Revoked) {
        std::cerr << "wavr node: this node was revoked. Its state is erased; "
                     "enrol it again to bring it back.\n";
        std::remove(opt.state_path.c_str());
        return kNodeRevoked;
      }
      if (next == NodeNext::Disabled && s.state != "disabled") {
        std::cerr << "wavr node: disabled by the Core; sensor off until reactivated\n";
        s.state = "disabled";
        save(opt.state_path, s);
      } else if (next == NodeNext::Active && s.state != "active") {
        s.state = "active";
        save(opt.state_path, s);
      }
      next_heartbeat = now + std::chrono::milliseconds(
          s.state == "disabled" ? opt.disabled_heartbeat_ms : opt.heartbeat_ms);
    }
    if (s.state == "disabled") {        // sensor off, radio still heartbeats
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
      continue;
    }
    std::vector<std::string> frames;
    sensor->collect(&frames, opt.telemetry_ms);
    if (!frames.empty()) {
      json body = {{"seq", next_seq()}, {"ld2450_frames", frames}};
      auto r = call(s, "/api/nodes/telemetry", body.dump());
      if (!r.ok) {
        std::cerr << "wavr node: telemetry: " << r.error << "\n";
      } else if (r.status == 423) {
        s.state = "disabled";            // the heartbeat will say so too
        save(opt.state_path, s);
      } else if (r.status == 409) {
        s.seq = std::max(s.seq, unix_ms());   // stale seq: jump past it
      } else if (r.status != 200) {
        std::cerr << "wavr node: telemetry answered HTTP " << r.status << "\n";
      }
    }
    if (Clock::now() >= next_save) {
      save(opt.state_path, s);
      next_save = Clock::now() + std::chrono::seconds(60);
    }
  }
  save(opt.state_path, s);
  return exit_code;
}

int node_reactivate(const NodeOptions& opt) {
  State s;
  std::string err;
  if (!load(opt.state_path, &s, &err)) {
    std::cerr << "wavr node: " << err << "\n";
    return kNodeUsage;
  }
  // NODE_PROTOCOL: the count is bumped and persisted BEFORE the round trip, so
  // it can never restart below what the Core last accepted. Running this
  // command at the machine is this node's physical button.
  ++s.press_count;
  if (!save(opt.state_path, s)) {
    std::cerr << "wavr node: cannot write " << opt.state_path << "\n";
    return kNodeFailed;
  }
  auto r = call(s, "/api/nodes/reactivate", json{{"press_count", s.press_count}}.dump());
  if (!r.ok) {
    std::cerr << "wavr node: " << r.error << "\n";
    return kNodeFailed;
  }
  if (r.status == 429) {
    std::cerr << "wavr node: too many reactivations; wait and try again\n";
    return kNodeFailed;
  }
  json body = json::parse(r.body, nullptr, false);
  if (r.status == 200 && str_field(body, "state") == "active") {
    s.state = "active";
    save(opt.state_path, s);
    std::cout << "Reactivated.\n";
    return kNodeOk;
  }
  std::cerr << "wavr node: reactivation refused (HTTP " << r.status << ")\n";
  return kNodeFailed;
}

int node_status(const NodeOptions& opt) {
  State s;
  std::string err;
  if (!load(opt.state_path, &s, &err)) {
    std::cerr << "wavr node: " << err << "\n";
    return kNodeUsage;
  }
  // Never the token: it is the node's credential.
  std::cout << "Node      " << s.node_id << "\nState     " << s.state << "\nCore      "
            << s.url << "\nPinned    " << s.pin << "\n";
  return kNodeOk;
}

}  // namespace wavr
