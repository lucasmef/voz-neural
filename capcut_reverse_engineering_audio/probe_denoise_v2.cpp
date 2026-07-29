#include <dlfcn.h>

#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
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

static_assert(sizeof(ExecutorContextCreateParameter) == 24);
static_assert(offsetof(ExecutorContextCreateParameter, model_buffer) == 8);
static_assert(offsetof(ExecutorContextCreateParameter, model_len) == 16);
static_assert(offsetof(ExecutorContextCreateParameter, num_channels) == 20);

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

std::vector<uint8_t> read_file(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    std::cerr << "Não foi possível abrir o modelo: " << path << '\n';
    std::exit(2);
  }
  input.seekg(0, std::ios::end);
  const auto size = input.tellg();
  input.seekg(0, std::ios::beg);
  std::vector<uint8_t> data(static_cast<size_t>(size));
  input.read(reinterpret_cast<char*>(data.data()), size);
  return data;
}

void dump_words(const char* title, const void* pointer, size_t bytes) {
  std::cout << title << " @" << pointer << '\n';
  const auto* words = static_cast<const uint32_t*>(pointer);
  for (size_t byte_offset = 0; byte_offset < bytes; byte_offset += 16) {
    std::cout << "  +" << std::hex << std::setw(3) << std::setfill('0')
              << byte_offset << ":";
    for (size_t column = 0; column < 4; ++column) {
      const size_t index = byte_offset / 4 + column;
      std::cout << ' ' << std::setw(8) << words[index];
    }
    std::cout << '\n';
  }
  std::cout << std::dec << std::setfill(' ');
}

}  // namespace

int main(int argc, char** argv) {
  const std::string model_path = argc > 1 ? argv[1] : kDefaultModelPath;
  const int32_t sample_rate = argc > 2 ? std::stoi(argv[2]) : 48000;
  const int32_t channels = argc > 3 ? std::stoi(argv[3]) : 1;
  const int32_t max_block_size = argc > 4 ? std::stoi(argv[4]) : 1024;

  void* library = dlopen(kSamiCorePath, RTLD_NOW | RTLD_LOCAL);
  if (!library) {
    std::cerr << "dlopen falhou: " << dlerror() << '\n';
    return 1;
  }

  using Constructor = void* (*)(void*);
  using CreateWithType =
      int32_t (*)(void*, const ExecutorContextCreateParameter*, int32_t);
  using GetLatency = int64_t (*)(void*);

  const auto constructor = load_symbol<Constructor>(
      library, "_ZN17DenoiseV2ExecutorC1Ev");
  const auto create = load_symbol<CreateWithType>(
      library,
      "_ZN17DenoiseV2Executor6CreateEPK30executorContextCreateParameter"
      "NS_11DenoiseTypeE");
  const auto get_latency = load_symbol<GetLatency>(
      library, "_ZN17DenoiseV2Executor10GetLatencyEv");

  const std::vector<uint8_t> model = read_file(model_path);
  alignas(16) uint8_t executor_storage[32] = {};
  constructor(executor_storage);

  ExecutorContextCreateParameter parameter{
      sample_rate,
      max_block_size,
      model.data(),
      static_cast<int32_t>(model.size()),
      channels,
  };

  std::cout << "modelo=" << model_path << '\n'
            << "bytes=" << model.size() << " sample_rate=" << sample_rate
            << " canais=" << channels
            << " max_block_size=" << max_block_size << '\n';

  const int32_t result = create(executor_storage, &parameter, 2);
  std::cout << "Create(type=2)=" << result << '\n';
  dump_words("executor", executor_storage, sizeof(executor_storage));

  void* implementation = nullptr;
  std::memcpy(&implementation, executor_storage + 8, sizeof(implementation));
  if (implementation) {
    dump_words("implementação", implementation, 0x178);
  }

  if (result == 0) {
    std::cout << "GetLatency=" << get_latency(executor_storage) << " amostras\n";
  }

  dlclose(library);
  return result == 0 ? 0 : 4;
}
