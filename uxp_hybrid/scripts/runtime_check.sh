#!/bin/sh
# Offline-only bootstrap check for the UXP Hybrid runtime.
#
# This script never downloads, installs, copies, or modifies dependencies.
# It only delegates to the bit-exact contract's read-only preflight.

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
contract="$project_root/uxp_hybrid/tests/verify_golden_contract.py"
packaged_python="$project_root/uxp_hybrid/runtime/python/bin/python3"
development_python_default="${HOME}/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
allow_development_fallback=0

usage() {
    echo "Uso: $0 [--allow-development-fallback]"
    echo ""
    echo "Somente verifica arquivos, SHA-256, Python, NumPy e goldens."
    echo "Não baixa e não instala nada."
}

for argument in "$@"; do
    case "$argument" in
        --allow-development-fallback)
            allow_development_fallback=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Argumento desconhecido: $argument" >&2
            usage >&2
            exit 64
            ;;
    esac
done

if [ ! -f "$contract" ]; then
    echo "[FALTA/ERRO] verificador ausente: $contract" >&2
    exit 2
fi

bootstrap_python=""
if [ -x "$packaged_python" ]; then
    bootstrap_python="$packaged_python"
elif [ "$allow_development_fallback" -eq 1 ] \
    && [ -n "${VOZ_NEURAL_TEST_PYTHON:-}" ] \
    && [ -x "${VOZ_NEURAL_TEST_PYTHON}" ]; then
    bootstrap_python="${VOZ_NEURAL_TEST_PYTHON}"
elif [ "$allow_development_fallback" -eq 1 ] \
    && [ -x "$development_python_default" ]; then
    bootstrap_python="$development_python_default"
elif [ -x /usr/bin/python3 ]; then
    bootstrap_python=/usr/bin/python3
else
    bootstrap_python=$(command -v python3 2>/dev/null || true)
fi

if [ -z "$bootstrap_python" ] || [ ! -x "$bootstrap_python" ]; then
    echo "[FALTA/ERRO] nenhum Python 3 disponível para executar o diagnóstico." >&2
    echo "O script não instalará Python automaticamente." >&2
    exit 2
fi

echo "Diagnóstico offline do UXP Hybrid"
echo "Projeto: $project_root"
echo "Nenhum download ou instalação será realizado."
echo ""

if [ "$allow_development_fallback" -eq 1 ]; then
    exec "$bootstrap_python" "$contract" \
        --project-root "$project_root" \
        --allow-development-fallback \
        --preflight-only
fi

exec "$bootstrap_python" "$contract" \
    --project-root "$project_root" \
    --preflight-only
