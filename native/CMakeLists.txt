# Wavr native runtime: the `wavr` CLI (status, capabilities, the Node role) and
# the stable C ABI (libwavr_native) that platform apps bind to.
#
#   cmake -S native -B build/native -G Ninja -DCMAKE_BUILD_TYPE=Release
#   cmake --build build/native && ctest --test-dir build/native
#
# Dependencies are fetched at configure time, pinned by version AND hash:
# mbedTLS 3.6 LTS (Apache-2.0 OR GPL-2.0-or-later) and nlohmann/json
# (MIT, one header). Offline: -DWAVR_MBEDTLS_TARBALL=/path/mbedtls-3.6.7.tar.bz2
# -DWAVR_JSON_HPP=/path/json.hpp.
cmake_minimum_required(VERSION 3.20)
project(wavr_native VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(WAVR_TLS "HTTPS to a LAN Core (needed by the Node role)" ON)
option(WAVR_STATIC "Link the C/C++ runtime statically into the executables" ON)
option(WAVR_SANITIZE "Build tests with UBSan (+ASan where the toolchain has it)" OFF)
option(WAVR_ASAN "Add AddressSanitizer to WAVR_SANITIZE" ON)

set(WAVR_REPO ${CMAKE_CURRENT_SOURCE_DIR}/..)

# A release binary is what gets copied onto a router or an old phone, where
# every megabyte counts: no symbols (Zig's clang otherwise keeps debug info).
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_link_options($<$<CONFIG:Release,MinSizeRel>:-s>)
endif()

# -- nlohmann/json (single header) ---------------------------------------------
set(WAVR_JSON_HPP "" CACHE FILEPATH "Local json.hpp v3.12.0 (optional)")
set(_json_dir ${CMAKE_BINARY_DIR}/_deps/nlohmann_json/include)
set(_json_hpp ${_json_dir}/nlohmann/json.hpp)
set(_json_sha256 aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63)
if(NOT EXISTS ${_json_hpp})
  if(WAVR_JSON_HPP)
    file(SHA256 ${WAVR_JSON_HPP} _have)
    if(NOT _have STREQUAL _json_sha256)
      message(FATAL_ERROR "WAVR_JSON_HPP is not json.hpp v3.12.0 (hash mismatch)")
    endif()
    configure_file(${WAVR_JSON_HPP} ${_json_hpp} COPYONLY)
  else()
    file(DOWNLOAD https://github.com/nlohmann/json/releases/download/v3.12.0/json.hpp
         ${_json_hpp} EXPECTED_HASH SHA256=${_json_sha256} TLS_VERIFY ON)
  endif()
endif()

# -- mbedTLS 3.6 LTS -------------------------------------------------------------
if(WAVR_TLS)
  include(FetchContent)
  set(WAVR_MBEDTLS_TARBALL "" CACHE FILEPATH "Local mbedtls-3.6.7.tar.bz2 (optional)")
  set(_mbed_url https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-3.6.7/mbedtls-3.6.7.tar.bz2)
  if(WAVR_MBEDTLS_TARBALL)
    set(_mbed_url ${WAVR_MBEDTLS_TARBALL})
  endif()
  set(ENABLE_PROGRAMS OFF CACHE BOOL "" FORCE)
  set(ENABLE_TESTING OFF CACHE BOOL "" FORCE)
  set(MBEDTLS_FATAL_WARNINGS OFF CACHE BOOL "" FORCE)
  set(USE_SHARED_MBEDTLS_LIBRARY OFF CACHE BOOL "" FORCE)
  set(USE_STATIC_MBEDTLS_LIBRARY ON CACHE BOOL "" FORCE)
  set(GEN_FILES OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(mbedtls
    URL ${_mbed_url}
    # The hash published beside the release (mbedtls-3.6.7-sha256sum.txt).
    URL_HASH SHA256=a7e8bcbec0e6f761b4af24f25677626b35f762f68eef79c08677a363212d11f6
    DOWNLOAD_EXTRACT_TIMESTAMP ON)
  FetchContent_MakeAvailable(mbedtls)
endif()

# -- The one "not responding" answer, taken from the canonical Python -----------
# status.py prints runtime_status.unreachable() when the Core does not answer;
# conformance/status.json carries it, and the CLI embeds exactly that.
file(READ ${WAVR_REPO}/conformance/status.json _status_json)
string(JSON _unreachable GET "${_status_json}" unreachable_runtime)
file(WRITE ${CMAKE_BINARY_DIR}/generated/generated/unreachable.h
"// Generated from conformance/status.json by CMake. Do not edit.\n#pragma once\n#define WAVR_UNREACHABLE_RUNTIME_JSON R\"__wavr__(${_unreachable})__wavr__\"\n")

# -- Libraries and programs ------------------------------------------------------
add_library(wavr_core STATIC
  src/semantics.cpp
  src/capabilities.cpp
  src/net.cpp
  src/serial.cpp
  src/node.cpp
  # Shared with the ESP32 firmware: ONE implementation of the certificate
  # fingerprint format an operator compares by eye (firmware/wavr_node).
  ${WAVR_REPO}/firmware/wavr_node/src/fingerprint_fmt.cpp)
target_include_directories(wavr_core PUBLIC
  include src ${_json_dir} ${WAVR_REPO}/firmware/wavr_node/src)
target_compile_definitions(wavr_core PUBLIC
  WAVR_NATIVE_VERSION="${PROJECT_VERSION}" WAVR_TLS=$<BOOL:${WAVR_TLS}>)
if(WAVR_TLS)
  target_link_libraries(wavr_core PUBLIC mbedtls mbedx509 mbedcrypto)
endif()
if(WIN32)
  target_link_libraries(wavr_core PUBLIC ws2_32)
endif()
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(wavr_core PRIVATE -Wall -Wextra)
endif()

# The C ABI as a shared library: what JNI, Swift and P/Invoke load.
add_library(wavr_native SHARED src/abi.cpp)
target_link_libraries(wavr_native PRIVATE wavr_core)
target_include_directories(wavr_native PUBLIC include)
target_compile_definitions(wavr_native PRIVATE WAVR_BUILDING_SHARED)
set_target_properties(wavr_native PROPERTIES C_VISIBILITY_PRESET hidden
                      CXX_VISIBILITY_PRESET hidden)

add_executable(wavr src/main.cpp)
target_include_directories(wavr PRIVATE ${CMAKE_BINARY_DIR}/generated)
target_link_libraries(wavr PRIVATE wavr_core)


# -- Tests: the conformance answer key ----------------------------------------
enable_testing()
add_executable(wavr_conformance tests/conformance.cpp src/abi.cpp)
target_link_libraries(wavr_conformance PRIVATE wavr_core)
target_compile_definitions(wavr_conformance PRIVATE
  WAVR_CONFORMANCE_DIR="${WAVR_REPO}/conformance")
if(WAVR_SANITIZE)
  set(_san -fsanitize=undefined -fno-sanitize-recover=all)
  if(WAVR_ASAN)
    list(APPEND _san -fsanitize=address)
  endif()
  target_compile_options(wavr_conformance PRIVATE ${_san} -g)
  target_link_options(wavr_conformance PRIVATE ${_san})
  target_compile_options(wavr_core PRIVATE ${_san} -g)
endif()
add_test(NAME conformance COMMAND wavr_conformance)

# Static where that is safe: one file to copy onto a device, and no picking up
# whichever libstdc++ happens to be first on PATH (on Windows that was a
# different compiler's, and the test crashed inside it). Linux only with musl:
# a static glibc binary still loads NSS at run time for getaddrinfo, from
# whatever glibc the device has.
if(WAVR_STATIC AND (MINGW OR ZIG_TARGET MATCHES "musl"))
  target_link_options(wavr PRIVATE -static)
  if(NOT WAVR_SANITIZE)
    target_link_options(wavr_conformance PRIVATE -static)
  endif()
  target_link_options(wavr_native PRIVATE -static-libgcc -static-libstdc++)
endif()
