#!/bin/zsh
set -euo pipefail

pacote_dir="${0:A:h}"
entrada="${1:-}"

if [[ -z "$entrada" ]]; then
  entrada="$(osascript -e '
    set escolhido to choose file with prompt "Escolha um arquivo WAV de voz"
    return POSIX path of escolhido
  ')"
fi

if [[ ! -f "$entrada" ]]; then
  print -u2 "Arquivo não encontrado: $entrada"
  exit 2
fi

nome="${entrada:t:r}"
pasta="${entrada:h}"
if [[ -n "${2:-}" ]]; then
  saida="$2"
else
  base_saida="${pasta}/${nome}_VOZ_CAPCUT75"
  saida="${base_saida}.wav"
  numero=2
  while [[ -e "$saida" ]]; do
    saida="${base_saida}_${numero}.wav"
    (( numero += 1 ))
  done
fi

tipos_python=(
  "${CODEX_VOICE_PYTHON:-}"
  "/Users/lucas/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
  "$(command -v python3 2>/dev/null || true)"
)
python_ok=""
for candidato in "${tipos_python[@]}"; do
  if [[ -n "$candidato" && -x "$candidato" ]] \
      && "$candidato" -c "import numpy" >/dev/null 2>&1; then
    python_ok="$candidato"
    break
  fi
done

if [[ -z "$python_ok" ]]; then
  print -u2 "Não encontrei Python 3 com NumPy."
  print -u2 "Defina CODEX_VOICE_PYTHON ou instale NumPy no Python 3."
  exit 3
fi

print "Processando voz em estéreo dual-mono para o Premiere (sem reprodução)…"
"$python_ok" "$pacote_dir/independent_voice_runner.py" \
  "$entrada" "$saida"

print ""
print "Concluído:"
print "$saida"
