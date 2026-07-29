#include "bridge.hpp"
#include "json.hpp"

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <system_error>
#include <unistd.h>

namespace {

namespace fs = std::filesystem;

class Fixture final {
 public:
  explicit Fixture(fs::path path) : path_(std::move(path)) {}
  ~Fixture() {
    std::error_code ignored;
    fs::remove(path_, ignored);
  }

  const fs::path& path() const { return path_; }

 private:
  fs::path path_;
};

void WriteLittleEndian16(std::ostream& output, std::uint16_t value) {
  const char bytes[] = {
      static_cast<char>(value & 0xFFU),
      static_cast<char>((value >> 8U) & 0xFFU),
  };
  output.write(bytes, sizeof(bytes));
}

void WriteLittleEndian32(std::ostream& output, std::uint32_t value) {
  const char bytes[] = {
      static_cast<char>(value & 0xFFU),
      static_cast<char>((value >> 8U) & 0xFFU),
      static_cast<char>((value >> 16U) & 0xFFU),
      static_cast<char>((value >> 24U) & 0xFFU),
  };
  output.write(bytes, sizeof(bytes));
}

void Write48kFixture(const fs::path& path, std::uint16_t channels) {
  constexpr std::uint32_t kSampleRate = 48000;
  constexpr std::uint16_t kBitsPerSample = 16;
  constexpr std::uint32_t kFrameCount = 480;
  const std::uint16_t block_align =
      channels * (kBitsPerSample / 8U);
  const std::uint32_t payload_bytes = kFrameCount * block_align;

  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output) {
    throw std::runtime_error("cannot create temporary WAV fixture");
  }
  output.write("RIFF", 4);
  WriteLittleEndian32(output, 36U + payload_bytes);
  output.write("WAVE", 4);
  output.write("fmt ", 4);
  WriteLittleEndian32(output, 16U);
  WriteLittleEndian16(output, 1U);
  WriteLittleEndian16(output, channels);
  WriteLittleEndian32(output, kSampleRate);
  WriteLittleEndian32(output, kSampleRate * block_align);
  WriteLittleEndian16(output, block_align);
  WriteLittleEndian16(output, kBitsPerSample);
  output.write("data", 4);
  WriteLittleEndian32(output, payload_bytes);
  for (std::uint32_t index = 0;
       index < kFrameCount * static_cast<std::uint32_t>(channels);
       ++index) {
    WriteLittleEndian16(output, 0U);
  }
  if (!output) {
    throw std::runtime_error("cannot finish temporary WAV fixture");
  }
}

const voz_neural::json::Value& Require(
    const voz_neural::json::Object& object, std::string_view key) {
  const auto match = object.find(key);
  if (match == object.end()) {
    throw std::runtime_error("missing JSON key: " + std::string(key));
  }
  return match->second;
}

void ExpectNumber(const voz_neural::json::Object& object,
                  std::string_view key, double expected) {
  const auto& value = Require(object, key);
  if (value.kind != voz_neural::json::Value::Kind::kNumber ||
      std::abs(value.number_value - expected) > 0.0001) {
    throw std::runtime_error("unexpected JSON number: " + std::string(key));
  }
}

}  // namespace

void CheckFixture(std::uint16_t channels) {
  const fs::path fixture_path =
      fs::temp_directory_path() /
      ("voz-neural-audio-probe-" + std::to_string(::getpid()) + "-" +
       std::to_string(channels) + "ch.wav");
  Fixture fixture(fixture_path);
  Write48kFixture(fixture.path(), channels);

  const std::uintmax_t size_before = fs::file_size(fixture.path());
  const fs::file_time_type timestamp_before =
      fs::last_write_time(fixture.path());

  const std::string success =
      voz_neural::ProbeAudioTopology(fixture.path().string());
  const auto success_object = voz_neural::json::ParseObject(success);
  const auto& ok = Require(success_object, "ok");
  if (ok.kind != voz_neural::json::Value::Kind::kBoolean ||
      !ok.boolean_value) {
    throw std::runtime_error("probe did not report success: " + success);
  }
  ExpectNumber(success_object, "channels", channels);
  ExpectNumber(success_object, "sampleRate", 48000.0);
  ExpectNumber(success_object, "audioTrackCount", 1.0);

  if (fs::file_size(fixture.path()) != size_before ||
      fs::last_write_time(fixture.path()) != timestamp_before) {
    throw std::runtime_error("probe modified the input fixture");
  }
  std::cout << success << '\n';
}

void CheckExternalPath(const fs::path& path) {
  const std::string response =
      voz_neural::ProbeAudioTopology(path.string());
  const auto object = voz_neural::json::ParseObject(response);
  const auto& ok = Require(object, "ok");
  if (ok.kind != voz_neural::json::Value::Kind::kBoolean ||
      !ok.boolean_value) {
    throw std::runtime_error("external probe failed: " + response);
  }
  ExpectNumber(object, "audioTrackCount", 1.0);
  std::cout << response << '\n';
}

int main(int argc, char** argv) {
  try {
    CheckFixture(1);
    CheckFixture(2);
    for (int index = 1; index < argc; ++index) {
      CheckExternalPath(argv[index]);
    }

    const std::string failure =
        voz_neural::ProbeAudioTopology(
            (fs::temp_directory_path() /
             ("voz-neural-missing-" + std::to_string(::getpid())))
                .string());
    if (failure.find("\"ok\":false") == std::string::npos ||
        failure.find("\"error\":{\"code\":") == std::string::npos ||
        failure.find("\"message\":") == std::string::npos) {
      throw std::runtime_error("invalid error envelope: " + failure);
    }

    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
