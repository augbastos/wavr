# Cross-compile the native runtime with Zig's bundled clang and libcs.
#
#   cmake -S native -B build/native-aarch64-linux -G Ninja \
#     -DCMAKE_TOOLCHAIN_FILE="$PWD/native/cmake/zig-toolchain.cmake" \
#     -DZIG=/path/to/zig -DZIG_TARGET=aarch64-linux-musl
#
# One compiler for every target below, from one download, with no sysroot to
# install: that is the whole reason to use it for a build matrix. A successful
# cross build proves the code is portable to that target -- it does NOT prove it
# runs there; results record the two separately.
#
# Targets used by native/ (see native/README.md for what each was checked with):
#   x86_64-linux-musl   aarch64-linux-musl   arm-linux-musleabihf
#   x86_64-windows-gnu  aarch64-macos        x86_64-macos

# CMake re-reads this file inside every try_compile scratch project; without
# this, those projects never see the two settings and configuration fails.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES ZIG ZIG_TARGET)
if(NOT ZIG_TARGET)
  message(FATAL_ERROR "set -DZIG_TARGET=<zig target triple>")
endif()
if(NOT ZIG)
  find_program(ZIG zig REQUIRED)
endif()

if(ZIG_TARGET MATCHES "linux")
  set(CMAKE_SYSTEM_NAME Linux)
elseif(ZIG_TARGET MATCHES "macos")
  set(CMAKE_SYSTEM_NAME Darwin)
elseif(ZIG_TARGET MATCHES "windows")
  set(CMAKE_SYSTEM_NAME Windows)
endif()
string(REGEX MATCH "^[^-]+" CMAKE_SYSTEM_PROCESSOR "${ZIG_TARGET}")

# CMake wants one executable per tool; zig wants a subcommand. Tiny wrappers.
set(_wrap ${CMAKE_BINARY_DIR}/zig-wrappers)
file(MAKE_DIRECTORY ${_wrap})
if(CMAKE_HOST_WIN32)
  foreach(_tool cc c++ ar ranlib)
    string(REPLACE "+" "x" _name ${_tool})
    if(_tool STREQUAL "cc" OR _tool STREQUAL "c++")
      file(WRITE ${_wrap}/zig-${_name}.cmd "@\"${ZIG}\" ${_tool} -target ${ZIG_TARGET} %*\r\n")
    else()
      file(WRITE ${_wrap}/zig-${_name}.cmd "@\"${ZIG}\" ${_tool} %*\r\n")
    endif()
    set(_zig_${_name} ${_wrap}/zig-${_name}.cmd)
  endforeach()
else()
  foreach(_tool cc c++ ar ranlib)
    string(REPLACE "+" "x" _name ${_tool})
    if(_tool STREQUAL "cc" OR _tool STREQUAL "c++")
      file(WRITE ${_wrap}/zig-${_name} "#!/bin/sh\nexec \"${ZIG}\" ${_tool} -target ${ZIG_TARGET} \"$@\"\n")
    else()
      file(WRITE ${_wrap}/zig-${_name} "#!/bin/sh\nexec \"${ZIG}\" ${_tool} \"$@\"\n")
    endif()
    file(CHMOD ${_wrap}/zig-${_name} PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE)
    set(_zig_${_name} ${_wrap}/zig-${_name})
  endforeach()
endif()

set(CMAKE_C_COMPILER ${_zig_cc})
set(CMAKE_CXX_COMPILER ${_zig_cxx})
set(CMAKE_AR ${_zig_ar} CACHE FILEPATH "" FORCE)
set(CMAKE_RANLIB ${_zig_ranlib} CACHE FILEPATH "" FORCE)
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)
