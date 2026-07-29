#!/bin/zsh
set -euo pipefail

workspace="${0:A:h:h}"
pack="$workspace/capcut_blackbox_audio_pack_female_v3"
build="$workspace/capcut_reverse_engineering_audio/build"
output="$build/blackbox_denoise"
decoder="$build/decode_audio_f32"
runner="$build/capcut_denoise_v2_runner"
treated="$workspace/analysis_capcut_blackbox_combined/split_treated"

mkdir -p "$output"

for category in \
  10_speech_quality \
  20_noise_only \
  30_speech_noise_mixes \
  40_voice_focus
do
  for source in "$pack/$category"/*.wav
  do
    filename="${source:t}"
    clip_id="${filename%%_*}"
    input_f32="$output/${clip_id}_input.f32"
    denoised_f32="$output/${clip_id}_denoise_v2.f32"
    "$decoder" "$source" "$input_f32"
    # decode_audio_f32 faz downmix explícito para mono, inclusive nas
    # calibrações estéreo F03–F05.
    "$runner" "$input_f32" "$denoised_f32" 48000 1 0.85 1
    print -- "$clip_id concluído"
  done
done

for source in "$treated"/*.wav
do
  filename="${source:t}"
  without_order="${filename#*_}"
  clip_id="${without_order%%_*}"
  case "$clip_id" in
    S*|N*|M*|F*)
      "$decoder" "$source" "$output/${clip_id}_capcut_full.f32"
      ;;
  esac
done
