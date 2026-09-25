// The native client command contract: backend/wavr/client_commands.py, held to
// it by conformance/client_commands.json. The command table itself is the
// Python's, embedded at build time (client_commands_table.inc, generated), so
// this file interprets rows; it does not define them. See docs/NATIVE-CLIENT.md.
#pragma once

#include <string>

#include "semantics.h"

namespace wavr {

constexpr int kClientCommandsSchema = 1;

// The embedded table: {"schema", "commands": {name: {method, path, auth, args}}}.
const json& command_table();

// {"request": {method, path, body, auth}} or {"error": "<code>"} -- the same
// codes as the Python CommandError.
json command_request(const json& name, const json& args);

// What the Core's answer means: {ok, status, error, detail, data}.
json command_result(int status, const std::string& body);
json command_transport_failure(const std::string& message);

// Run one command against a Core. Never throws; a malformed call comes back as
// {ok: false, error: "bad_call", detail: "<code>"} without touching the network.
// Onboarding commands (auth "none") never carry the token.
json command_run(const std::string& url, const std::string& token, const std::string& pin,
                 const std::string& name, const std::string& args_json, int timeout_ms = 6000);

// TOFU: the certificate fingerprint a Core presents, for a person to compare
// before pairing. {ok, https, fingerprint, status, error}.
json probe_core(const std::string& url, int timeout_ms = 6000);

}  // namespace wavr
