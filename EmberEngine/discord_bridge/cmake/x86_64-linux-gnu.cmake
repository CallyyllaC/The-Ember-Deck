# Cross-compile discord_bridge as Linux x86_64 on the Raspberry Pi, for use
# under Box64 when Discord ships only an x86_64 Linux SDK.
#   sudo apt install g++-x86-64-linux-gnu
#   cmake -S . -B build-x86_64 -DCMAKE_TOOLCHAIN_FILE=cmake/x86_64-linux-gnu.cmake \
#         -DDISCORD_SOCIAL_SDK_DIR=/path/to/discord_social_sdk
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_CXX_COMPILER x86_64-linux-gnu-g++)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY BOTH)  # the SDK lives outside the sysroot
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-linux-gnu)

# libdiscord_partner_sdk.so lists ordinary Linux desktop libraries as
# dependencies (libasound.so.2, libpulse.so.0, libX11.so.6). An ARM64 host's
# x86_64 cross toolchain has no x86_64 copies of them, so a normal link fails
# while checking the SDK's own undefined symbols. The bridge does not use
# those libraries directly, and at run time Box64 resolves them by wrapping
# the Pi's native ARM64 libraries (confirmed on a Raspberry Pi 5 with SDK
# 1.10.19337). Allow undefined symbols inside shared libraries for this
# cross-build only; undefined symbols in the bridge itself are still errors.
set(CMAKE_EXE_LINKER_FLAGS_INIT "-Wl,--allow-shlib-undefined")
