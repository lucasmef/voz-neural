#include <dlfcn.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace {

constexpr const char* kSamiCorePath =
    "/Applications/CapCut.app/Contents/Frameworks/libsamicore.dylib";
constexpr const char* kDefaultModelPath =
    "/Applications/CapCut.app/Contents/Resources/audiosami/"
    "unet_denoise_44k_music_model_v1.0.model";

struct ExecutorContextCreateParameter {
  int32_t sample_rate;
  int32_t max_block_size;
  const uint8_t* model_buffer;
  int32_t model_len;
  int32_t num_channels;
};

struct AudioBuffer {
  uint32_t number_channels;
  uint32_t number_samples;
  int32_t is_interleave;
  int32_t reserved;
  void* data;
};

struct AudioBlock {
  int32_t data_type;
  uint32_t number_audio_data;
  void* audio_data;
  void* user_context;
};

static_assert(sizeof(ExecutorContextCreateParameter) == 24);
static_assert(sizeof(AudioBuffer) == 24);
static_assert(sizeof(AudioBlock) == 24);

template <typename T>
T load_symbol(void* library, const char* name) {
  dlerror();
  void* symbol = dlsym(library, name);
  if (const char* error = dlerror()) {
    std::cerr << "dlsym falhou para " << name << ": " << error << '\n';
    std::exit(3);
  }
  return reinterpret_cast<T>(symbol);
}

std::vector<uint8_t> read_bytes(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    std::cerr << "Não foi possível abrir: " << path << '\n';
    std::exit(2);
  }
  input.seekg(0, std::ios::end);
  const auto size = input.tellg();
  input.seekg(0, std::ios::beg);
  std::vector<uint8_t> data(static_cast<size_t>(size));
  input.read(reinterpret_cast<char*>(data.data()), size);
  return data;
}

void write_float32(const std::string& path, const std::vector<float>& audio) {
  std::ofstream output(path, std::ios::binary);
  if (!output) {
    std::cerr << "Não foi possível escrever: " << path << '\n';
    std::exit(2);
  }
  output.write(reinterpret_cast<const char*>(audio.data()),
               static_cast<std::streamsize>(audio.size() * sizeof(float)));
}

void usage(const char* executable) {
  std::cerr
      << "Uso: " << executable
      << " INPUT.f32 OUTPUT.f32 SAMPLE_RATE CHANNELS"
         " [DENOISE_RATE=0.85] [DENOISE_MODE=1] [MODEL]"
         " [MAX_BLOCK_SIZE=1024]\n"
      << "Entrada e saída: float32 little-endian intercalado. A saída tem o "
         "mesmo número de frames; a latência informada pelo SAMI é "
         "compensada.\n";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 5 || argc > 9) {
    usage(argv[0]);
    return 1;
  }

  const std::string input_path = argv[1];
  const std::string output_path = argv[2];
  const int32_t sample_rate = std::stoi(argv[3]);
  const int32_t channels = std::stoi(argv[4]);
  const float denoise_rate = argc > 5 ? std::stof(argv[5]) : 0.85F;
  const float denoise_mode = argc > 6 ? std::stof(argv[6]) : 1.0F;
  const std::string model_path = argc > 7 ? argv[7] : kDefaultModelPath;
  const int32_t max_block_size = argc > 8 ? std::stoi(argv[8]) : 1024;

  if (sample_rate < 16000 || sample_rate > 48000 ||
      (channels != 1 && channels != 2) || max_block_size < 64 ||
      max_block_size > 8192) {
    std::cerr << "Formato inválido: sample rate 16–48 kHz, 1 ou 2 canais.\n";
    return 1;
  }

  const std::vector<uint8_t> input_bytes = read_bytes(input_path);
  if (input_bytes.size() % (sizeof(float) * channels) != 0) {
    std::cerr << "O tamanho do raw não é múltiplo do frame float32.\n";
    return 1;
  }

  std::vector<float> input(input_bytes.size() / sizeof(float));
  std::memcpy(input.data(), input_bytes.data(), input_bytes.size());
  const size_t input_frames = input.size() / static_cast<size_t>(channels);
  const std::vector<uint8_t> model = read_bytes(model_path);

  void* library = dlopen(kSamiCorePath, RTLD_NOW | RTLD_LOCAL);
  if (!library) {
    std::cerr << "dlopen falhou: " << dlerror() << '\n';
    return 2;
  }

  using Constructor = void* (*)(void*);
  using CreateWithType =
      int32_t (*)(void*, const ExecutorContextCreateParameter*, int32_t);
  using SetFloatParameter =
      int32_t (*)(void*, const std::string&, float);
  using ProcessAudio = int32_t (*)(void*, AudioBlock*, AudioBlock*);
  using GetLatency = int64_t (*)(void*);

  const auto constructor = load_symbol<Constructor>(
      library, "_ZN17DenoiseV2ExecutorC1Ev");
  const auto create = load_symbol<CreateWithType>(
      library,
      "_ZN17DenoiseV2Executor6CreateEPK30executorContextCreateParameter"
      "NS_11DenoiseTypeE");
  const auto set_parameter = load_symbol<SetFloatParameter>(
      library,
      "_ZN17DenoiseV2Executor12SetParameterERKNSt3__112basic_stringIc"
      "NS0_11char_traitsIcEENS0_9allocatorIcEEEEf");
  const auto process_audio = load_symbol<ProcessAudio>(
      library, "_ZN17DenoiseV2Executor12ProcessAudioEP10audioBlockS1_");
  const auto get_latency = load_symbol<GetLatency>(
      library, "_ZN17DenoiseV2Executor10GetLatencyEv");

  alignas(16) uint8_t executor_storage[32] = {};
  constructor(executor_storage);

  const ExecutorContextCreateParameter create_parameter{
      sample_rate,
      max_block_size,
      model.data(),
      static_cast<int32_t>(model.size()),
      channels,
  };
  const int32_t create_result =
      create(executor_storage, &create_parameter, 2);
  if (create_result != 0) {
    std::cerr << "Create(type=2) falhou: " << create_result << '\n';
    return 4;
  }

  int32_t result =
      set_parameter(executor_storage, std::string("denoiserate"), denoise_rate);
  if (result == 0) {
    result = set_parameter(executor_storage, std::string("denoisemode"),
                           denoise_mode);
  }
  if (result != 0) {
    std::cerr << "SetParameter falhou: " << result << '\n';
    return 5;
  }

  const size_t latency_frames =
      static_cast<size_t>(get_latency(executor_storage));
  const size_t frames_to_process = input_frames + latency_frames;
  std::vector<float> delayed_output(frames_to_process * channels, 0.0F);

  std::vector<std::vector<float>> input_planes(
      channels, std::vector<float>(max_block_size));
  std::vector<std::vector<float>> output_planes(
      channels, std::vector<float>(max_block_size));
  std::vector<float*> input_plane_pointers(channels);
  std::vector<float*> output_plane_pointers(channels);
  for (int32_t channel = 0; channel < channels; ++channel) {
    input_plane_pointers[channel] = input_planes[channel].data();
    output_plane_pointers[channel] = output_planes[channel].data();
  }

  for (size_t position = 0; position < frames_to_process;) {
    const uint32_t block_frames = static_cast<uint32_t>(
        std::min<size_t>(max_block_size, frames_to_process - position));
    for (int32_t channel = 0; channel < channels; ++channel) {
      std::fill_n(input_planes[channel].data(), block_frames, 0.0F);
      std::fill_n(output_planes[channel].data(), block_frames, 0.0F);
      for (uint32_t frame = 0; frame < block_frames; ++frame) {
        const size_t source_frame = position + frame;
        if (source_frame < input_frames) {
          input_planes[channel][frame] =
              input[source_frame * channels + channel];
        }
      }
    }

    AudioBuffer input_buffer{
        static_cast<uint32_t>(channels),
        block_frames,
        0,
        0,
        input_plane_pointers.data(),
    };
    AudioBuffer output_buffer{
        static_cast<uint32_t>(channels),
        block_frames,
        0,
        0,
        output_plane_pointers.data(),
    };
    AudioBlock input_block{50, 1, &input_buffer, nullptr};
    AudioBlock output_block{50, 1, &output_buffer, nullptr};

    result = process_audio(executor_storage, &input_block, &output_block);
    if (result != 0) {
      std::cerr << "ProcessAudio falhou no frame " << position << ": "
                << result << '\n';
      return 6;
    }

    for (uint32_t frame = 0; frame < block_frames; ++frame) {
      for (int32_t channel = 0; channel < channels; ++channel) {
        delayed_output[(position + frame) * channels + channel] =
            output_planes[channel][frame];
      }
    }
    position += block_frames;
  }

  std::vector<float> compensated_output(input.size(), 0.0F);
  const size_t available_frames =
      frames_to_process > latency_frames
          ? std::min(input_frames, frames_to_process - latency_frames)
          : 0;
  for (size_t frame = 0; frame < available_frames; ++frame) {
    for (int32_t channel = 0; channel < channels; ++channel) {
      compensated_output[frame * channels + channel] =
          delayed_output[(frame + latency_frames) * channels + channel];
    }
  }
  write_float32(output_path, compensated_output);

  std::cerr << "OK frames=" << input_frames
            << " latency_frames=" << latency_frames
            << " denoise_rate=" << denoise_rate
            << " denoise_mode=" << denoise_mode << '\n';
  return 0;
}
