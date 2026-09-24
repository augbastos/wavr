// A seeded mutation driver for the targets in targets.cpp, for toolchains
// without libFuzzer (GCC, MinGW). It is NOT coverage-guided: it mutates the
// seed corpus (real fixtures) with byte-level and structure-aware operators
// and runs every input under whatever sanitizers the build enabled. With clang,
// build with -fsanitize=fuzzer instead and this file is not used.
//
//   fuzz_<target> [--seconds N] [--runs N] [--max-len N] [--seed N] CORPUS_DIR...
//
// A failing input (sanitizer report, invariant abort, or an input slower than
// --timeout-ms) is written to ./crash-<pid>.bin before the process dies.
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <vector>

#if defined(__has_include)
#  if __has_include(<sanitizer/common_interface_defs.h>)
#    include <sanitizer/common_interface_defs.h>
#    define WAVR_HAVE_SAN_CALLBACK 1
#  endif
#endif
#if !defined(_WIN32)
#  include <unistd.h>
#endif

extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size);

namespace {

std::vector<uint8_t> g_current;

void save_current() {
  char name[64];
#if defined(_WIN32)
  std::snprintf(name, sizeof name, "crash-input.bin");
#else
  std::snprintf(name, sizeof name, "crash-%d.bin", static_cast<int>(getpid()));
#endif
  if (FILE* f = std::fopen(name, "wb")) {
    std::fwrite(g_current.data(), 1, g_current.size(), f);
    std::fclose(f);
    std::fprintf(stderr, "failing input (%zu bytes) written to %s\n", g_current.size(), name);
  }
}

void on_signal(int sig) {
  save_current();
  std::signal(sig, SIG_DFL);
  std::raise(sig);
}

// Tokens that break naive parsers: JSON edge values, HTTP framing, numbers at
// the limits of every integer width, non-finite spellings, surrogates.
const std::vector<std::string> kDictionary = {
    "null", "true", "false", "0", "-0", "-1", "1e309", "-1e309", "1e-400", "NaN", "Infinity",
    "9223372036854775807", "9223372036854775808", "-9223372036854775809",
    "18446744073709551615", "18446744073709551616", "4294967296", "2147483648",
    "\"\"", "{}", "[]", "\"\\u0000\"", "\"\\ud800\"", "\"\\udfff\"", "\xff\xfe", "\xc0\xaf",
    "\"state\":", "\"total\":", "\"rooms\":", "\"sources\":", "\"confidence\":", "\"watch\":true",
    "\"could_not_check\":[", "\"unavailable\"", "\"healthy\"", "\"degraded\"",
    "HTTP/1.1 200 OK\r\n", "\r\n\r\n", "Content-Length: 99999999999999999999\r\n",
    "Content-Length: -1\r\n", "Transfer-Encoding: chunked\r\n", "ffffffffffffffff\r\n",
    "0\r\n\r\n", "https://", "http://", "[::1]", "[::ffff:127.0.0.1]", "127.0.0.1.",
    "localhost.", "@", ":0", ":65536", ":99999999999", "\xaa\xff\x03\x00", "\x55\xcc",
    "\"command\":\"sleep\"", "\"state\":\"revoked\""};

struct Mutator {
  std::mt19937_64 rng;
  size_t max_len;
  const std::vector<std::vector<uint8_t>>& corpus;

  size_t pick(size_t n) { return n ? static_cast<size_t>(rng() % n) : 0; }

  std::vector<uint8_t> mutate(std::vector<uint8_t> in) {
    const int rounds = 1 + static_cast<int>(pick(6));
    for (int r = 0; r < rounds; ++r) {
      switch (pick(10)) {
        case 0:   // bit flip
          if (!in.empty()) in[pick(in.size())] ^= static_cast<uint8_t>(1u << pick(8));
          break;
        case 1: {   // interesting byte
          static const uint8_t kBytes[] = {0x00, 0x01, 0x7f, 0x80, 0xff, '"', '\\', '{', '}',
                                           '[', ']', ',', ':'};
          if (!in.empty()) in[pick(in.size())] = kBytes[pick(sizeof kBytes)];
          break;
        }
        case 2: {   // insert random bytes
          size_t at = pick(in.size() + 1), n = 1 + pick(16);
          for (size_t i = 0; i < n; ++i) in.insert(in.begin() + at, static_cast<uint8_t>(rng()));
          break;
        }
        case 3:   // delete a range
          if (!in.empty()) {
            size_t at = pick(in.size()), n = 1 + pick(in.size() - at);
            in.erase(in.begin() + at, in.begin() + at + n);
          }
          break;
        case 4: {   // insert a dictionary token
          const std::string& t = kDictionary[pick(kDictionary.size())];
          in.insert(in.begin() + pick(in.size() + 1), t.begin(), t.end());
          break;
        }
        case 5:   // duplicate a range
          if (!in.empty() && in.size() < max_len) {
            size_t at = pick(in.size()), n = 1 + pick(std::min<size_t>(in.size() - at, 256));
            std::vector<uint8_t> dup(in.begin() + at, in.begin() + at + n);
            in.insert(in.begin() + pick(in.size() + 1), dup.begin(), dup.end());
          }
          break;
        case 6:   // splice another seed
          if (!corpus.empty()) {
            const auto& other = corpus[pick(corpus.size())];
            if (!other.empty()) {
              size_t at = pick(other.size());
              in.insert(in.begin() + pick(in.size() + 1), other.begin() + at, other.end());
            }
          }
          break;
        case 7:   // truncate
          if (!in.empty()) in.resize(pick(in.size()));
          break;
        case 8: {   // deep nesting: the classic recursion bomb
          size_t depth = 1 + pick(4000);
          std::string open(depth, pick(2) ? '[' : '{');
          in.insert(in.begin() + pick(in.size() + 1), open.begin(), open.end());
          break;
        }
        default: {   // a NUL separator: moves bytes between the target's parts
          in.insert(in.begin() + pick(in.size() + 1), uint8_t{0});
          break;
        }
      }
    }
    if (in.size() > max_len) in.resize(max_len);
    return in;
  }
};

}  // namespace

int main(int argc, char** argv) {
  double seconds = 60;
  long long runs = -1;
  size_t max_len = 65536;
  unsigned long long seed = 1;
  long long timeout_ms = 2000;
  std::vector<std::vector<uint8_t>> corpus;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&] { return i + 1 < argc ? std::string(argv[++i]) : std::string("0"); };
    if (a == "--seconds") seconds = std::stod(next());
    else if (a == "--runs") runs = std::stoll(next());
    else if (a == "--max-len") max_len = std::stoull(next());
    else if (a == "--seed") seed = std::stoull(next());
    else if (a == "--timeout-ms") timeout_ms = std::stoll(next());
    else if (std::filesystem::is_directory(a)) {
      for (const auto& e : std::filesystem::recursive_directory_iterator(a)) {
        if (!e.is_regular_file()) continue;
        std::ifstream f(e.path(), std::ios::binary);
        corpus.emplace_back(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
      }
    }
  }
  if (corpus.empty()) corpus.emplace_back();
#if defined(WAVR_HAVE_SAN_CALLBACK)
  __sanitizer_set_death_callback(save_current);
#endif
  std::signal(SIGABRT, on_signal);
  std::signal(SIGSEGV, on_signal);
  std::signal(SIGFPE, on_signal);

  // Every seed as-is first: the corpus itself must pass.
  for (const auto& c : corpus) {
    g_current = c;
    LLVMFuzzerTestOneInput(c.data(), c.size());
  }
  Mutator m{std::mt19937_64(seed), max_len, corpus};
  const auto start = std::chrono::steady_clock::now();
  long long n = 0, slowest_us = 0;
  for (;; ++n) {
    if (runs >= 0 && n >= runs) break;
    const auto now = std::chrono::steady_clock::now();
    if (runs < 0 && std::chrono::duration<double>(now - start).count() >= seconds) break;
    g_current = m.mutate(corpus[m.pick(corpus.size())]);
    const auto t0 = std::chrono::steady_clock::now();
    LLVMFuzzerTestOneInput(g_current.data(), g_current.size());
    const long long us = std::chrono::duration_cast<std::chrono::microseconds>(
                             std::chrono::steady_clock::now() - t0).count();
    if (us > slowest_us) slowest_us = us;
    if (us > timeout_ms * 1000) {
      std::fprintf(stderr, "input took %lld ms (limit %lld)\n", us / 1000, timeout_ms);
      save_current();
      return 2;
    }
  }
  std::printf("%lld inputs, %zu seeds, slowest %lld us, no failure\n", n, corpus.size(),
              slowest_us);
  return 0;
}
