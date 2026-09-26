// This host's capability manifest (docs/WAVR-PROTOCOL.md section 5), probed
// natively. Same honesty rule as backend/wavr/capabilities.py: a key this code
// did not actually determine is ABSENT (unknown), never false. The tier and the
// Core decision come from semantics.cpp, which is checked against the Python.
#include "capabilities.h"

#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <thread>

#include "semantics.h"

#if defined(_WIN32)
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#else
#  include <sys/utsname.h>
#  include <unistd.h>
#  include <dirent.h>
#  if defined(__APPLE__)
#    include <TargetConditionals.h>
#    include <sys/sysctl.h>
#    include <sys/types.h>
#  endif
#endif

namespace wavr {

std::string detect_platform() {
#if defined(_WIN32)
  return "windows";
#elif defined(__ANDROID__)
  return "android";
#elif defined(__APPLE__) && (TARGET_OS_IOS || TARGET_OS_TV)
  return "ios";
#elif defined(__APPLE__)
  return "macos";
#elif defined(__linux__)
  // capabilities.detect_platform: Android is asked at RUN time, because one
  // static Linux binary runs on both, and a phone treated as a Linux box is
  // how a Core ends up ignoring Doze and dying overnight.
  if (std::getenv("ANDROID_ROOT") || std::getenv("ANDROID_DATA")) return "android";
  utsname u{};
  if (uname(&u) == 0) {
    std::string rel = u.release;
    for (auto& c : rel) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (rel.find("android") != std::string::npos) return "android";
  }
  return "linux";
#else
  return "unknown";
#endif
}

long cpu_list_count(const std::string& list) {
  long total = 0;
  std::stringstream ss(list);
  std::string part;
  while (std::getline(ss, part, ',')) {
    while (!part.empty() && std::isspace(static_cast<unsigned char>(part.back()))) part.pop_back();
    if (part.empty()) continue;
    char* end = nullptr;
    long lo = std::strtol(part.c_str(), &end, 10);
    long hi = lo;
    if (end == part.c_str() || lo < 0) return 0;
    if (*end == '-') {
      const char* rest = end + 1;
      hi = std::strtol(rest, &end, 10);
      if (end == rest || hi < lo) return 0;
    }
    if (*end != '\0') return 0;
    total += hi - lo + 1;
  }
  return total;
}

namespace {

std::optional<long> ram_mb() {
#if defined(_WIN32)
  MEMORYSTATUSEX m{};
  m.dwLength = sizeof m;
  if (GlobalMemoryStatusEx(&m)) return static_cast<long>(m.ullTotalPhys / (1024ull * 1024ull));
  return std::nullopt;
#else
  std::ifstream f("/proc/meminfo");
  std::string key;
  long kb = 0;
  while (f >> key >> kb) {
    if (key == "MemTotal:") return kb / 1024;
    f.ignore(256, '\n');
  }
#  if defined(__APPLE__)
  int64_t bytes = 0;
  size_t len = sizeof bytes;
  if (sysctlbyname("hw.memsize", &bytes, &len, nullptr, 0) == 0 && bytes > 0) {
    return static_cast<long>(bytes / (1024 * 1024));
  }
#  endif
  long pages = sysconf(_SC_PHYS_PAGES);
  long size = sysconf(_SC_PAGE_SIZE);
  if (pages > 0 && size > 0) {
    return static_cast<long>((static_cast<long long>(pages) * size) / (1024 * 1024));
  }
  return std::nullopt;
#endif
}

std::optional<long> cpu_count() {
#if defined(__linux__)
  // os.cpu_count() counts the machine's online CPUs. musl's answer (behind
  // hardware_concurrency) is this PROCESS's affinity instead: on a phone, a
  // 32-bit build was allowed 2 of 8 cores and reported a smaller tier.
  std::ifstream f("/sys/devices/system/cpu/online");
  std::string list;
  if (std::getline(f, list)) {
    if (long n = cpu_list_count(list)) return n;
  }
#endif
  unsigned n = std::thread::hardware_concurrency();   // 0 means "cannot tell"
  if (n == 0) return std::nullopt;
  return static_cast<long>(n);
}

std::string arch() {
#if defined(_WIN32)
  // What Python's platform.machine() says here, so two manifests of one
  // machine agree: AMD64 / ARM64 / x86.
  SYSTEM_INFO si{};
  GetNativeSystemInfo(&si);
  switch (si.wProcessorArchitecture) {
    case PROCESSOR_ARCHITECTURE_AMD64: return "AMD64";
    case PROCESSOR_ARCHITECTURE_ARM64: return "ARM64";
    case PROCESSOR_ARCHITECTURE_INTEL: return "x86";
    default: return "";
  }
#else
  utsname u{};
  return uname(&u) == 0 ? std::string(u.machine) : std::string();
#endif
}

std::string os_version() {
#if defined(_WIN32)
  // RtlGetVersion tells the truth where GetVersionEx lies to unmanifested apps.
  using RtlGetVersionFn = LONG(WINAPI*)(PRTL_OSVERSIONINFOW);
  HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
  if (!ntdll) return "";
  auto fn = reinterpret_cast<RtlGetVersionFn>(
      reinterpret_cast<void*>(GetProcAddress(ntdll, "RtlGetVersion")));
  RTL_OSVERSIONINFOW v{};
  v.dwOSVersionInfoSize = sizeof v;
  if (!fn || fn(&v) != 0) return "";
  return std::to_string(v.dwMajorVersion) + "." + std::to_string(v.dwMinorVersion) + "." +
         std::to_string(v.dwBuildNumber);
#else
  utsname u{};
  return uname(&u) == 0 ? std::string(u.release) : std::string();
#endif
}

std::string pi_model() {
#if defined(__linux__)
  for (const char* path : {"/sys/firmware/devicetree/base/model", "/proc/device-tree/model"}) {
    std::ifstream f(path, std::ios::binary);
    if (!f) continue;
    std::string s((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    while (!s.empty() && (s.back() == '\0' || s.back() == '\n' || s.back() == ' ')) s.pop_back();
    if (!s.empty()) return s;
  }
#endif
  return "";
}

// True / false / unknown, exactly as capabilities._has_battery decides.
std::optional<bool> has_battery() {
#if defined(_WIN32)
  SYSTEM_POWER_STATUS s{};
  if (!GetSystemPowerStatus(&s)) return std::nullopt;
  if (s.BatteryFlag == 255) return std::nullopt;        // "unknown status"
  return (s.BatteryFlag & 128) == 0;                     // 128 = "no system battery"
#elif defined(__linux__) && !defined(__ANDROID__)
  DIR* d = opendir("/sys/class/power_supply");
  if (!d) return std::nullopt;
  bool found = false, unreadable = false;
  while (dirent* e = readdir(d)) {
    if (e->d_name[0] == '.') continue;
    std::ifstream f(std::string("/sys/class/power_supply/") + e->d_name + "/type");
    std::string type;
    if (f >> type) {
      for (auto& c : type) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
      if (type == "battery") found = true;
    } else {
      unreadable = true;   // Android's SELinux lists these and refuses every read
    }
  }
  closedir(d);
  if (found) return true;
  // No Battery-type supply -- which is "no battery" only if every one was read.
  if (unreadable) return std::nullopt;
  return false;
#else
  return std::nullopt;   // Android restricts /sys; Apple needs IOKit: not probed
#endif
}

}  // namespace

json probe_manifest() {
  const std::string plat = detect_platform();
  const auto ram = ram_mb();
  const auto cpus = cpu_count();
  const std::string pi = pi_model();

  std::string tier = compute_tier(ram, cpus, plat);
  // capabilities._scan_host: a Pi is MEDIUM at >= 900 MB, for its permanent power.
  if (!pi.empty() && tier == "low" && ram.value_or(0) >= 900) tier = "medium";

  json caps = json::object();
  if (auto b = has_battery()) {
    caps["battery"] = *b;
    caps["permanent_power"] = !*b;   // only the inverse of a KNOWN battery answer
  }
  // A software ability of this runtime, known by construction where serial.cpp
  // implements it. NOT a claim that a radar is attached -- and elsewhere
  // (Apple, for now) a claim this build cannot keep, so it is left unknown.
#if defined(_WIN32) || defined(__linux__)
  caps["serial_port_support"] = true;
#endif

  json functions = json::array();
  if (can_be_core(plat, ram, tier)) functions.push_back("core");
  functions.push_back("node");       // anything that runs this is a Node

  json m = {
      {"platform", plat},
      {"arch", arch()},
      {"os_version", os_version()},
      {"functions_supported", functions},
      {"capabilities", caps},
      {"ram_mb", ram ? json(*ram) : json(nullptr)},
      {"cpu_count", cpus ? json(*cpus) : json(nullptr)},
      {"compute_tier", tier},
      {"model", pi.empty() ? arch() : pi},
      {"protocol_version", 1},
  };
  return m;
}

}  // namespace wavr
