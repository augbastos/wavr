#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace wavr {

// "windows" | "linux" | "macos" | "android" | "ios" | "unknown"
std::string detect_platform();

// How many CPUs a kernel cpu list names ("0-7", "0-3,6", "2"); 0 if malformed.
long cpu_list_count(const std::string& list);

// This host's capability manifest; see capabilities.cpp for the honesty rule.
nlohmann::json probe_manifest();

}  // namespace wavr
