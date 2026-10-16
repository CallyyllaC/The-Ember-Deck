#!/usr/bin/env bash
# Report what the unpacked Discord Social SDK actually contains, so the bridge
# backend (native or x86_64 under Box64) is chosen from the binaries, not from
# folder names or documentation. Usage: ./inspect_sdk.sh [/path/to/discord_social_sdk]
set -euo pipefail
SDK="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/third_party/discord_social_sdk}"
echo "SDK directory: $SDK"
HEADER="$SDK/include/discordpp.h"
if [ -f "$HEADER" ]; then
  echo "header: include/discordpp.h present"
else
  echo "header: include/discordpp.h MISSING"
fi

echo "--- native libraries (format / OS / CPU from the binary itself)"
linux_arches=""
while IFS= read -r -d '' lib; do
  rel="${lib#$SDK/}"
  desc="$(file -bL "$lib" | cut -c1-90)"
  os=""
  if [[ "$desc" == ELF* ]]; then
    needed="$(readelf -d "$lib" 2>/dev/null | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p' | tr '\n' ' ')"
    machine="$(readelf -h "$lib" 2>/dev/null | sed -n 's/.*Machine:[[:space:]]*//p')"
    if [[ " $needed" == *" libandroid.so"* || " $needed" == *" liblog.so"* ]]; then
      os="Android (bionic) - not usable on Raspberry Pi OS"
    elif [[ " $needed" == *" libc.so.6"* ]]; then
      os="Linux glibc"
      linux_arches+="$machine; "
    fi
    echo "$rel: $machine | $os"
    [ "$os" = "Linux glibc" ] && echo "    needs: $needed"
  else
    echo "$rel: $desc"
  fi
done < <(find "$SDK" -type f \( -name '*.so' -o -name '*.so.*' -o -name '*.dll' -o -name '*.dylib' \) -print0)
for aar in "$SDK"/lib/*/*.aar; do
  [ -f "$aar" ] && echo "${aar#$SDK/}: Android package ($(unzip -Z1 "$aar" 'jni/*' 2>/dev/null | tr '\n' ' '))"
done
echo "Linux glibc architectures: ${linux_arches:-none}"
case "$linux_arches" in
  *AArch64*) echo "=> native ARM64 bridge possible (preferred)";;
  *X86-64*) echo "=> Linux is x86_64 only: build the x86_64 bridge and run it under Box64";;
  *) echo "=> no usable Linux library found";;
esac

echo "--- desktop sign-in API used by the bridge (Authorize + GetToken with PKCE)"
if [ -f "$HEADER" ]; then
  grep -n -E 'CreateAuthorizationCodeVerifier\(\);|void Authorize\(|void GetToken\(|void AbortAuthorize\(|using AuthorizationCallback' \
    "$HEADER" | head -10 || true
fi
echo "host: $(uname -m), page size $(getconf PAGESIZE)"
command -v box64 >/dev/null && echo "box64: $(command -v box64)" || echo "box64: not installed"
