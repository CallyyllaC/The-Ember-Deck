# discord_bridge: Ember Deck's native helper for the Discord Social SDK.
#
#   cmake -S . -B build -DDISCORD_SOCIAL_SDK_DIR=/path/to/discord_social_sdk
#   cmake --build build
#
# Without the SDK only the protocol library and its tests are built, so the
# protocol can be tested on any machine. The SDK is proprietary and is not
# part of this repository; see README.md.
cmake_minimum_required(VERSION 3.22)  # SYSTEM includes map to /external:I on MSVC
project(discord_bridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)          # required by the Discord Social SDK C++ headers
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(bridge_protocol STATIC src/protocol.cpp)
target_include_directories(bridge_protocol PUBLIC src)
if(MSVC)
  target_compile_options(bridge_protocol PRIVATE /W4 /utf-8)
else()
  target_compile_options(bridge_protocol PRIVATE -Wall -Wextra -Wpedantic)
endif()

option(BRIDGE_BUILD_TESTS "Build SDK-independent protocol tests" ON)
if(BRIDGE_BUILD_TESTS)
  enable_testing()
  add_executable(protocol_tests tests/protocol_tests.cpp)
  target_link_libraries(protocol_tests PRIVATE bridge_protocol)
  if(MSVC)
    target_compile_options(protocol_tests PRIVATE /utf-8)
  endif()
  add_test(NAME protocol_tests COMMAND protocol_tests)
endif()

set(DISCORD_SOCIAL_SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/discord_social_sdk"
    CACHE PATH "Unpacked Discord Social SDK (contains include/discordpp.h and lib/release/)")

option(BRIDGE_WARNINGS_AS_ERRORS "Treat warnings in bridge sources as errors" OFF)

if(EXISTS "${DISCORD_SOCIAL_SDK_DIR}/include/discordpp.h")
  find_library(DISCORD_PARTNER_SDK_LIBRARY
    NAMES discord_partner_sdk
    PATHS "${DISCORD_SOCIAL_SDK_DIR}/lib/release"
    NO_DEFAULT_PATH
    NO_CMAKE_FIND_ROOT_PATH)
  if(NOT DISCORD_PARTNER_SDK_LIBRARY)
    message(FATAL_ERROR "discord_partner_sdk library not found in ${DISCORD_SOCIAL_SDK_DIR}/lib/release")
  endif()

  # Linux: refuse early, with a clear message, when the SDK library was built
  # for a different CPU than this build targets. SDK 1.10.19337 ships Linux
  # x86_64 only, so a native Raspberry Pi (aarch64) build cannot link; use
  # cmake/x86_64-linux-gnu.cmake and Box64 instead (see README.md).
  if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND DISCORD_PARTNER_SDK_LIBRARY MATCHES "\.so$")
    file(READ "${DISCORD_PARTNER_SDK_LIBRARY}" _elf_machine OFFSET 18 LIMIT 2 HEX)
    if(_elf_machine STREQUAL "3e00")
      set(_sdk_arch x86_64)
    elseif(_elf_machine STREQUAL "b700")
      set(_sdk_arch aarch64)
    else()
      set(_sdk_arch "unknown (e_machine ${_elf_machine})")
    endif()
    string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" _target_arch)
    if(_target_arch STREQUAL "arm64")
      set(_target_arch aarch64)
    elseif(_target_arch STREQUAL "amd64")
      set(_target_arch x86_64)
    endif()
    if(NOT _sdk_arch STREQUAL _target_arch)
      message(FATAL_ERROR
        "The Discord Social SDK library is ${_sdk_arch} but this build targets ${_target_arch}. "
        "For an x86_64 SDK on a Raspberry Pi, configure with "
        "-DCMAKE_TOOLCHAIN_FILE=cmake/x86_64-linux-gnu.cmake and run the bridge under Box64.")
    endif()
  endif()

  find_package(Threads REQUIRED)
  add_executable(discord_bridge src/main.cpp)
  # SYSTEM: warnings inside Discord's generated headers are not ours to fix.
  target_include_directories(discord_bridge SYSTEM PRIVATE "${DISCORD_SOCIAL_SDK_DIR}/include")
  target_link_libraries(discord_bridge PRIVATE bridge_protocol "${DISCORD_PARTNER_SDK_LIBRARY}" Threads::Threads)
  if(MSVC)
    target_compile_options(discord_bridge PRIVATE /W4 /utf-8 /external:W0)
    if(BRIDGE_WARNINGS_AS_ERRORS)
      target_compile_options(discord_bridge PRIVATE /WX)
    endif()
  else()
    target_compile_options(discord_bridge PRIVATE -Wall -Wextra -Wpedantic)
    if(BRIDGE_WARNINGS_AS_ERRORS)
      target_compile_options(discord_bridge PRIVATE -Werror)
    endif()
  endif()
  if(WIN32)
    # Windows development builds: the import library links against
    # bin/release/discord_partner_sdk.dll, copied next to the executable.
    add_custom_command(TARGET discord_bridge POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
              "${DISCORD_SOCIAL_SDK_DIR}/bin/release/discord_partner_sdk.dll" "$<TARGET_FILE_DIR:discord_bridge>")
  else()
    # Find the SDK library next to where it was linked, without LD_LIBRARY_PATH.
    set_target_properties(discord_bridge PROPERTIES
      BUILD_RPATH "${DISCORD_SOCIAL_SDK_DIR}/lib/release"
      INSTALL_RPATH "${DISCORD_SOCIAL_SDK_DIR}/lib/release")
  endif()
  message(STATUS "discord_bridge: linking ${DISCORD_PARTNER_SDK_LIBRARY}")
else()
  message(STATUS "discord_bridge: Discord Social SDK not found at ${DISCORD_SOCIAL_SDK_DIR}; "
                 "building protocol tests only")
endif()
