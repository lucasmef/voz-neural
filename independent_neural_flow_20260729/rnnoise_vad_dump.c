/*
 * Dump RNNoise voice probabilities for normalized 48 kHz mono float audio.
 *
 * Usage:
 *   rnnoise_vad_dump input.f32 probabilities.f32
 *
 * Input samples are little-endian float32 in the range [-1, 1].  Output is
 * one little-endian float32 probability per RNNoise frame (480 samples /
 * 10 ms).  The last input frame is zero-padded.
 *
 * RNNoise is distributed under its BSD 3-Clause license.  The complete
 * copyright list and license text ship in licenses/RNNOISE_BSD-3-Clause.txt.
 */

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "rnnoise.h"

int main(int argc, char **argv) {
  DenoiseState *state;
  FILE *input;
  FILE *output;
  float *frame_in;
  float *frame_out;
  int frame_size;

  if (argc != 3) {
    fprintf(stderr, "usage: %s input.f32 probabilities.f32\n", argv[0]);
    return 2;
  }
  input = fopen(argv[1], "rb");
  if (input == NULL) {
    fprintf(stderr, "cannot open input: errno=%d\n", errno);
    return 3;
  }
  output = fopen(argv[2], "wb");
  if (output == NULL) {
    fprintf(stderr, "cannot open output: errno=%d\n", errno);
    fclose(input);
    return 4;
  }
  frame_size = rnnoise_get_frame_size();
  frame_in = (float *)calloc((size_t)frame_size, sizeof(float));
  frame_out = (float *)calloc((size_t)frame_size, sizeof(float));
  state = rnnoise_create(NULL);
  if (frame_in == NULL || frame_out == NULL || state == NULL) {
    fprintf(stderr, "allocation failed\n");
    free(frame_in);
    free(frame_out);
    if (state != NULL) rnnoise_destroy(state);
    fclose(input);
    fclose(output);
    return 5;
  }

  for (;;) {
    size_t count = fread(
        frame_in, sizeof(float), (size_t)frame_size, input);
    size_t index;
    float probability;
    if (count == 0) break;
    for (index = 0; index < count; ++index) frame_in[index] *= 32768.0f;
    for (; index < (size_t)frame_size; ++index) frame_in[index] = 0.0f;
    probability = rnnoise_process_frame(state, frame_out, frame_in);
    if (fwrite(&probability, sizeof(float), 1, output) != 1) {
      fprintf(stderr, "cannot write output\n");
      rnnoise_destroy(state);
      free(frame_in);
      free(frame_out);
      fclose(input);
      fclose(output);
      return 6;
    }
  }

  rnnoise_destroy(state);
  free(frame_in);
  free(frame_out);
  fclose(input);
  fclose(output);
  return 0;
}
