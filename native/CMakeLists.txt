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
# The product has one version, in the repository's VERSION file.
file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/../VERSION" _wavr_version LIMIT_COUNT 1)
project(wavr_native VERSION ${_wavr_version} LANGUAGES C CXX)

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
  # No build-machine paths in the binary: __FILE__ (mbedTLS uses it) otherwise
  # embeds wherever this was built. Set before the dependencies are fetched so
  # it reaches them too; also what makes two builds of one commit comparable.
  get_filename_component(_wavr_root "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
  add_compile_options(-ffile-prefix-map=${CMAKE_BINARY_DIR}=build
                      -ffile-prefix-map=${_wavr_root}=wavr)
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
  src/client_view.cpp
  src/client_commands.cpp
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

# Android: the JNI library the Kotlin app loads (dev.wavr.core.nativeui.WavrNative).
# One .so with the runtime inside, so the app ships a single native file per ABI.
if(ANDROID)
  add_library(wavr_native_jni SHARED jni/wavr_jni.cpp src/abi.cpp)
  target_link_libraries(wavr_native_jni PRIVATE wavr_core)
  target_compile_definitions(wavr_native_jni PRIVATE WAVR_BUILDING_SHARED)
  set_target_properties(wavr_native_jni PROPERTIES C_VISIBILITY_PRESET hidden
                        CXX_VISIBILITY_PRESET hidden)
  # Google Play requires 16 KB page alignment for native code on Android 15+.
  target_link_options(wavr_native_jni PRIVATE -Wl,-z,max-page-size=16384)
endif()

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
  # Anything linking the instrumented core needs the sanitizer runtimes too.
  target_link_options(wavr_core PUBLIC ${_san})
endif()
add_test(NAME conformance COMMAND wavr_conformance)

# An outside consumer: C, the public header only, linked against the shared
# library. What JNI, Swift and P/Invoke will see.
add_executable(wavr_c_consumer tests/c_consumer.c)
if(ZIG_TARGET MATCHES "^mips")
  # Zig's compiler-rt leaves __udivmoddi4 unexported from a MIPS shared library,
  # so an executable linked against it fails; the library itself is fine, and
  # nothing here can run a MIPS test anyway (no device, no emulator).
  set_target_properties(wavr_c_consumer PROPERTIES EXCLUDE_FROM_ALL ON)
endif()
target_include_directories(wavr_c_consumer PRIVATE include)
target_link_libraries(wavr_c_consumer PRIVATE wavr_native)
set_target_properties(wavr_c_consumer PROPERTIES C_STANDARD 99 C_STANDARD_REQUIRED ON)
if(WAVR_SANITIZE)
  # ASan's runtime must come first in the executable, not arrive with the .so.
  target_link_options(wavr_c_consumer PRIVATE ${_san})
endif()
add_test(NAME c_consumer COMMAND wavr_c_consumer)

# -- Fuzzing (native/fuzz) -----------------------------------------------------
# One binary per target. WAVR_LIBFUZZER=ON (clang): real libFuzzer. Otherwise the
# seeded mutation driver in fuzz/driver.cpp. Combine with WAVR_SANITIZE=ON.
option(WAVR_FUZZ "Build the fuzz targets" OFF)
option(WAVR_LIBFUZZER "Link the fuzz targets with -fsanitize=fuzzer (clang only)" OFF)
if(WAVR_FUZZ)
  foreach(_t http_response snapshot status framer node_state url heartbeat)
    set(_srcs fuzz/targets.cpp src/abi.cpp)
    if(NOT WAVR_LIBFUZZER)
      list(APPEND _srcs fuzz/driver.cpp)
    endif()
    add_executable(fuzz_${_t} ${_srcs})
    target_compile_definitions(fuzz_${_t} PRIVATE WAVR_FUZZ_TARGET=fuzz_${_t})
    target_link_libraries(fuzz_${_t} PRIVATE wavr_core)
    if(WAVR_LIBFUZZER)
      target_compile_options(fuzz_${_t} PRIVATE -fsanitize=fuzzer)
      target_link_options(fuzz_${_t} PRIVATE -fsanitize=fuzzer)
    endif()
    if(WAVR_SANITIZE)
      target_compile_options(fuzz_${_t} PRIVATE ${_san} -g)
      target_link_options(fuzz_${_t} PRIVATE ${_san})
    endif()
  endforeach()
endif()

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
  if(MINGW AND NOT ZIG_TARGET)
    # Everything, winpthread included: the DLL must load on a Windows that has
    # never seen MinGW (it depended on libwinpthread-1.dll before). Not under
    # Zig: there -static turns the DLL into a static library (its runtime is
    # linked in anyway).
    target_link_options(wavr_native PRIVATE -static)
  elseif(NOT ZIG_TARGET MATCHES "windows")
    target_link_options(wavr_native PRIVATE -static-libgcc -static-libstdc++)
  endif()
endif()
