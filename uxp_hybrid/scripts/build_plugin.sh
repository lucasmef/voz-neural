#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
hybrid_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
project_root=$(CDPATH= cd -- "$hybrid_root/.." && pwd)
default_sdk="$project_root/.deps/uxp-hybrid-plugin-sdk-6.5.0/uxp-hybrid-plugin-sdk-main"
sdk_dir=${UXP_HYBRID_SDK_DIR:-$default_sdk}
node_executable=${VOZ_NEURAL_NODE:-}

if [ -z "$node_executable" ]; then
    node_executable=$(command -v node 2>/dev/null || true)
fi
if [ -z "$node_executable" ] \
    && [ -x "${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node" ]; then
    node_executable="${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
fi
if [ -z "$node_executable" ] || [ ! -x "$node_executable" ]; then
    echo "Node.js não encontrado para executar os testes do painel." >&2
    echo "Defina VOZ_NEURAL_NODE com o executável do Node.js." >&2
    exit 2
fi

if [ ! -f "$sdk_dir/src/api/UxpAddonShared.h" ] \
    || [ ! -f "$sdk_dir/src/utilities/UxpAddon.h" ]; then
    echo "SDK híbrido oficial da Adobe não encontrado em: $sdk_dir" >&2
    echo "Defina UXP_HYBRID_SDK_DIR com a pasta descompactada do SDK." >&2
    exit 2
fi

python3 "$script_dir/prepare_runtime.py"
"$node_executable" "$hybrid_root/uxp/tests/static-check.js"
"$node_executable" "$hybrid_root/uxp/tests/workflow-check.js"
for javascript in \
    "$hybrid_root/uxp/main.js" \
    "$hybrid_root/uxp/js/"*.js \
    "$hybrid_root/uxp/tests/"*.js; do
    "$node_executable" --check "$javascript"
done
"$script_dir/runtime_check.sh"
"$hybrid_root/runtime/python/bin/python3" \
    "$hybrid_root/tests/verify_golden_contract.py" \
    --project-root "$project_root"
"$hybrid_root/runtime/python/bin/python3" \
    "$hybrid_root/tests/verify_premiere_mono_frontend.py" \
    --project-root "$project_root"
"$script_dir/audio_probe_smoke.sh"

if command -v cmake >/dev/null 2>&1; then
    cmake -S "$hybrid_root" -B "$hybrid_root/build" \
        -DUXP_HYBRID_SDK_DIR="$sdk_dir" \
        -DCMAKE_BUILD_TYPE=Release
    cmake --build "$hybrid_root/build" --config Release
else
    addon_output="$hybrid_root/build/mac/arm64/voz-neural.uxpaddon"
    mkdir -p "$(dirname "$addon_output")"
    /usr/bin/clang++ \
        -x objective-c++ \
        -std=c++20 \
        -O3 \
        -DNDEBUG \
        -arch arm64 \
        -mmacosx-version-min=12.0 \
        -dynamiclib \
        -fobjc-arc \
        -fvisibility=hidden \
        -fvisibility-inlines-hidden \
        -Wall \
        -Wextra \
        -Wpedantic \
        -DVOZ_NEURAL_ADDON_VERSION='"0.1.0"' \
        -DVOZ_NEURAL_RUNTIME_RELATIVE_PATH='"runtime"' \
        -DVOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE=0 \
        -I"$hybrid_root/native" \
        -I"$sdk_dir/src/utilities" \
        -I"$sdk_dir/src/api" \
        "$hybrid_root/native/audio_probe.mm" \
        "$hybrid_root/native/bridge.cpp" \
        "$hybrid_root/native/json.cpp" \
        "$hybrid_root/native/module.cpp" \
        -framework AVFoundation \
        -framework AudioToolbox \
        -framework CoreMedia \
        -framework Foundation \
        -o "$addon_output"
fi

"$hybrid_root/runtime/python/bin/python3" \
    "$script_dir/package_plugin.py"

echo ""
echo "Plugin pronto para carregar no UXP Developer Tool:"
echo "$project_root/uxp_hybrid/dist/Voz Neural"
