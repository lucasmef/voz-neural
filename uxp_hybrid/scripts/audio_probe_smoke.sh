#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
hybrid_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
probe_test_dir=$(mktemp -d "${TMPDIR:-/tmp}/voz-neural-probe.XXXXXX")
probe_test_binary="$probe_test_dir/audio-probe-smoke"

cleanup() {
    rm -rf -- "$probe_test_dir"
}
trap cleanup EXIT HUP INT TERM

/usr/bin/clang++ \
    -x objective-c++ \
    -std=c++20 \
    -O2 \
    -DNDEBUG \
    -arch arm64 \
    -mmacosx-version-min=12.0 \
    -fobjc-arc \
    -Wall \
    -Wextra \
    -Wpedantic \
    -I"$hybrid_root/native" \
    "$hybrid_root/native/audio_probe.mm" \
    "$hybrid_root/native/json.cpp" \
    "$hybrid_root/tests/audio_probe_smoke.cpp" \
    -framework AVFoundation \
    -framework AudioToolbox \
    -framework CoreMedia \
    -framework Foundation \
    -o "$probe_test_binary"

"$probe_test_binary" "$@"
