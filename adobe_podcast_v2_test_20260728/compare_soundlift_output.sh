#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
STEM_DIR=${1:-"$SCRIPT_DIR/soundlift_uxp_output"}
RESULT_DIR=${2:-"$SCRIPT_DIR/results"}
CLIP_IDS=${SOUNDLIFT_CLIP_IDS:-"S01,S03,S06,M04,F02"}

PYTHON_BIN=${SOUNDLIFT_PYTHON:-}
if [[ -z "$PYTHON_BIN" ]]; then
  EAGLE_PYTHON="/Users/lucas/Library/Application Support/Eagle/Plugins/ai-search/python-env/bin/python3"
  if [[ -x "$EAGLE_PYTHON" ]]; then
    PYTHON_BIN=$EAGLE_PYTHON
  else
    PYTHON_BIN=$(command -v python3)
  fi
fi

if [[ ! -d "$STEM_DIR" ]]; then
  print -u2 "Pasta de stems não encontrada: $STEM_DIR"
  exit 2
fi

if ! "$PYTHON_BIN" -c 'import numpy' 2>/dev/null; then
  print -u2 "O Python selecionado não contém NumPy: $PYTHON_BIN"
  print -u2 "Defina SOUNDLIFT_PYTHON para um Python 3 com NumPy."
  exit 3
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/compare_sound_lift_to_capcut.py" \
  --soundlift-dir "$STEM_DIR" \
  --clip-ids "$CLIP_IDS" \
  --output-dir "$RESULT_DIR" \
  --write-top 3
