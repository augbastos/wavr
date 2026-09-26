#include "node.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "net.h"
#include "semantics.h"
#include "serial.h"

#if !defined(_WIN32)
#  include <sys/stat.h>
#  include <unistd.h>
#else
#  include <fcntl.h>
#  include <io.h>
#  include <process.h>
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

using State = NodeState;

// A state file is a few hundred bytes; anything far larger is not one.
constexpr std::streamsize kMaxStateFile = 64 * 1024;

bool load(const std::string& path, State* s, std::string* error) {
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    *error = "no node state at " + path + " -- enrol first (wavr node enroll)";
    return false;
  }
  std::string text(static_cast<size_t>(kMaxStateFile) + 1, '\0');
  f.read(text.data(), kMaxStateFile + 1);
  text.resize(static_cast<size_t>(f.gcount()));
  if (f.gcount() > kMaxStateFile || !parse_node_state(text, s)) {
    *error = path + " is not a node state file";
    return false;
  }
  return true;
}

// Written to a temporary file and moved into place, and readable by the owner
// only: it holds the node's bearer token.
bool save(const std::string& path, const State& in) {
  // seq and press_count only ever go up (the Core rejects a repeat). A second
  // process -- `wavr node reactivate` while `run` holds an older copy -- may
  // have written a higher value; never write it back down.
  State s = in, disk;
  std::string ignored;
  if (load(path, &disk, &ignored)) {
    s.seq = std::max(s.seq, disk.seq);
    s.press_count = std::max(s.press_count, disk.press_count);
  }
  json j = {{"url", s.url},     {"node_id", s.node_id}, {"token", s.token},
            {"pin", s.pin},     {"state", s.state},     {"seq", s.seq},
            {"press_count", s.press_count}};
  // One temp file per process (run and reactivate may overlap), removed first
  // so it is always CREATED here -- an existing file keeps its old mode, and
  // umask only governs creation.
#if defined(_WIN32)
  const std::string tmp = path + ".tmp." + std::to_string(_getpid());
#else
  const std::string tmp = path + ".tmp." + std::to_string(getpid());
#endif
  std::remove(tmp.c_str());
  {
#if !defined(_WIN32)
    const mode_t old_mask = umask(077);   // born owner-only, not chmod-ed afterwards
#endif
    std::ofstream f(tmp, std::ios::trunc);
#if !defined(_WIN32)
    umask(old_mask);
#endif
    if (!f) return false;
    f << dump(j, 1) << "\n";
    if (!f) return false;
  }
#if defined(_WIN32)
  // rename() does not replace an existing file on Windows. There the file
  // inherits its directory's ACL (no 0600): run the node, or point --state,
  // inside a per-user directory, never a shared one. native/README.md says so.
  std::remove(path.c_str());
#else
  chmod(tmp.c_str(), 0600);   // belt and braces: it was created 0600 already
#endif
  return std::rename(tmp.c_str(), path.c_str()) == 0;   // atomic replace on POSIX
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

// Raw LD2450 bytes on standard input, from any program that can reach the radar
// (a serial bridge, `nc`, a script on another bus). Framed here exactly like a
// local UART; the Core stays the only parser. A reader thread fills a bounded
// buffer (oldest bytes dropped past 64 KiB -- a stalled Core never grows it).
class StdinFrames : public FrameSource {
 public:
  // The reader thread cannot be joined (a blocking fread has no portable
  // cancel), so it must never touch this object: it owns a shared_ptr to the
  // state instead, which outlives the StdinFrames that created it.
  StdinFrames() : shared_(std::make_shared<Shared>()) {
    std::thread([st = shared_] { run(st); }).detach();
  }
  void collect(std::vector<std::string>* out, int budget_ms) override {
    std::vector<uint8_t> bytes;
    {
      std::unique_lock<std::mutex> lock(shared_->mu);
      shared_->cv.wait_for(lock, std::chrono::milliseconds(budget_ms),
                           [this] { return !shared_->pending.empty(); });
      bytes.swap(shared_->pending);
    }
    framer_.feed(bytes.data(), bytes.size());
    // Drain every complete frame so the framer never grows; keep the newest
    // 32 (live radar data: a backlog is worth less than the latest reading).
    std::vector<std::string> frames;
    while (auto f = framer_.next()) {
      frames.push_back(to_hex(f->data(), f->size()));
      if (frames.size() > kMaxFramesPerCollect) frames.erase(frames.begin());
    }
    out->insert(out->end(), frames.begin(), frames.end());
  }

 private:
  static constexpr size_t kMaxPending = 64 * 1024;
  static constexpr size_t kMaxFramesPerCollect = 32;
  struct Shared {
    std::mutex mu;
    std::condition_variable cv;
    std::vector<uint8_t> pending;
  };
  static void run(std::shared_ptr<Shared> st) {
    uint8_t buf[512];
    for (;;) {
      size_t n = std::fread(buf, 1, sizeof buf, stdin);
      if (n == 0) {   // EOF or error: the Core sees silence, and decays
        std::cerr << "wavr node: standard input closed; no more sensor data\n";
        return;
      }
      std::lock_guard<std::mutex> lock(st->mu);
      st->pending.insert(st->pending.end(), buf, buf + n);
      if (st->pending.size() > kMaxPending) {
        st->pending.erase(st->pending.begin(),
                          st->pending.end() - static_cast<long>(kMaxPending));
      }
      st->cv.notify_one();
    }
  }
  std::shared_ptr<Shared> shared_;
  Ld2450Framer framer_;
};

std::unique_ptr<FrameSource> open_sensor(const std::string& spec, std::string* error) {
  if (spec.rfind("ld2450:", 0) == 0) return std::make_unique<SerialFrames>(spec.substr(7));
  if (spec == "stdin") {
#if defined(_WIN32)
    _setmode(_fileno(stdin), _O_BINARY);   // bytes, not text: no CRLF translation
#endif
    return std::make_unique<StdinFrames>();
  }
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

bool parse_node_state(const std::string& text, NodeState* s) {
  json j = json::parse(text, nullptr, false);
  if (str_field(j, "token").empty() || str_field(j, "url").empty()) return false;
  s->url = str_field(j, "url");
  s->node_id = str_field(j, "node_id");
  s->token = str_field(j, "token");
  s->pin = str_field(j, "pin");
  s->state = str_field(j, "state", "active");
  s->seq = int_field(j, "seq");
  s->press_count = int_field(j, "press_count");
  return true;
}

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
  req.body = dump(json{{"code", code}, {"cert_fingerprint", ""}});
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

// Failure lines, rate-limited. A node on a flaky link used to write one line
// per failed request -- ten a second in a soak, one a second in normal use --
// into whatever collects stderr (syslog on flash, a pipe nobody drains). A new
// message prints at once; the same message again prints at most once a minute,
// with how many times it happened in between.
class ErrorLog {
 public:
  void report(const std::string& line) {
    auto now = Clock::now();
    auto it = seen_.find(line);
    if (it != seen_.end() && now - it->second.printed < std::chrono::seconds(60)) {
      ++it->second.suppressed;   // heartbeat and telemetry failures interleave:
      return;                    // one entry per message, not just the last one
    }
    if (it != seen_.end() && it->second.suppressed) {
      std::cerr << "wavr node: " << line << " (" << it->second.suppressed
                << " more times in the last minute)\n";
    } else {
      std::cerr << "wavr node: " << line << "\n";
    }
    if (it == seen_.end()) {
      if (seen_.size() >= kMaxMessages) seen_.clear();   // bounded: never grows
      it = seen_.emplace(line, Entry{}).first;
    }
    it->second.printed = now;
    it->second.suppressed = 0;
  }
  void flush() {
    for (const auto& [line, e] : seen_) {
      if (e.suppressed) {
        std::cerr << "wavr node: " << line << " (" << e.suppressed << " more times)\n";
      }
    }
    seen_.clear();
  }

 private:
  static constexpr size_t kMaxMessages = 32;
  struct Entry {
    Clock::time_point printed{};
    long long suppressed = 0;
  };
  std::map<std::string, Entry> seen_;
};

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
  ErrorLog errors;
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
      if (!r.ok) errors.report("heartbeat: " + r.error);
      if (next == NodeNext::Revoked) {
        errors.flush();
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
      auto r = call(s, "/api/nodes/telemetry", dump(body));
      if (!r.ok) {
        errors.report("telemetry: " + r.error);
      } else if (r.status == 423) {
        s.state = "disabled";            // the heartbeat will say so too
        save(opt.state_path, s);
      } else if (r.status == 409) {
        s.seq = std::max(s.seq, unix_ms());   // stale seq: jump past it
      } else if (r.status == 401 || r.status == 403) {
        // Probably revoked. The heartbeat is what decides that (as in the
        // firmware); ask it now instead of sending on with a dead token.
        next_heartbeat = Clock::now();
        errors.report("telemetry answered HTTP " + std::to_string(r.status));
      } else if (r.status != 200) {
        errors.report("telemetry answered HTTP " + std::to_string(r.status));
      }
    }
    if (Clock::now() >= next_save) {
      save(opt.state_path, s);
      next_save = Clock::now() + std::chrono::seconds(60);
    }
  }
  errors.flush();
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
  auto r = call(s, "/api/nodes/reactivate", dump(json{{"press_count", s.press_count}}));
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
