#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace {

constexpr double kPi = 3.14159265358979323846;

struct Coefficients {
  double b0;
  double b1;
  double b2;
  double a1;
  double a2;
};

struct State {
  double z1 = 0.0;
  double z2 = 0.0;
};

enum class FilterType { kLowShelf, kPeak, kHighShelf };

Coefficients make_coefficients(FilterType type, double sample_rate,
                               double frequency, double q, double gain_db) {
  const double amplitude = std::pow(10.0, gain_db / 40.0);
  const double w0 = 2.0 * kPi * frequency / sample_rate;
  const double cosine = std::cos(w0);
  const double sine = std::sin(w0);
  const double alpha = sine / (2.0 * q);
  double b0 = 0.0;
  double b1 = 0.0;
  double b2 = 0.0;
  double a0 = 1.0;
  double a1 = 0.0;
  double a2 = 0.0;

  if (type == FilterType::kPeak) {
    b0 = 1.0 + alpha * amplitude;
    b1 = -2.0 * cosine;
    b2 = 1.0 - alpha * amplitude;
    a0 = 1.0 + alpha / amplitude;
    a1 = -2.0 * cosine;
    a2 = 1.0 - alpha / amplitude;
  } else {
    const double beta = 2.0 * std::sqrt(amplitude) * alpha;
    if (type == FilterType::kLowShelf) {
      b0 = amplitude *
           ((amplitude + 1.0) - (amplitude - 1.0) * cosine + beta);
      b1 = 2.0 * amplitude *
           ((amplitude - 1.0) - (amplitude + 1.0) * cosine);
      b2 = amplitude *
           ((amplitude + 1.0) - (amplitude - 1.0) * cosine - beta);
      a0 = (amplitude + 1.0) + (amplitude - 1.0) * cosine + beta;
      a1 = -2.0 *
           ((amplitude - 1.0) + (amplitude + 1.0) * cosine);
      a2 = (amplitude + 1.0) + (amplitude - 1.0) * cosine - beta;
    } else {
      b0 = amplitude *
           ((amplitude + 1.0) + (amplitude - 1.0) * cosine + beta);
      b1 = -2.0 * amplitude *
           ((amplitude - 1.0) + (amplitude + 1.0) * cosine);
      b2 = amplitude *
           ((amplitude + 1.0) + (amplitude - 1.0) * cosine - beta);
      a0 = (amplitude + 1.0) - (amplitude - 1.0) * cosine + beta;
      a1 = 2.0 *
           ((amplitude - 1.0) - (amplitude + 1.0) * cosine);
      a2 = (amplitude + 1.0) - (amplitude - 1.0) * cosine - beta;
    }
  }
  return {b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0};
}

double process_biquad(double sample, const Coefficients& coefficient,
                      State& state) {
  const double output = coefficient.b0 * sample + state.z1;
  state.z1 =
      coefficient.b1 * sample - coefficient.a1 * output + state.z2;
  state.z2 = coefficient.b2 * sample - coefficient.a2 * output;
  return output;
}

std::vector<float> read_float32(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    std::cerr << "Não foi possível abrir: " << path << '\n';
    std::exit(2);
  }
  input.seekg(0, std::ios::end);
  const auto size = input.tellg();
  input.seekg(0, std::ios::beg);
  if (size < 0 || size % static_cast<std::streamoff>(sizeof(float)) != 0) {
    std::cerr << "Entrada float32 inválida.\n";
    std::exit(2);
  }
  std::vector<float> data(static_cast<size_t>(size) / sizeof(float));
  input.read(reinterpret_cast<char*>(data.data()), size);
  return data;
}

void write_float32(const std::string& path, const std::vector<float>& data) {
  std::ofstream output(path, std::ios::binary);
  if (!output) {
    std::cerr << "Não foi possível escrever: " << path << '\n';
    std::exit(2);
  }
  output.write(reinterpret_cast<const char*>(data.data()),
               static_cast<std::streamsize>(data.size() * sizeof(float)));
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 5) {
    std::cerr << "Uso: " << argv[0]
              << " INPUT.f32 OUTPUT.f32 SAMPLE_RATE CHANNELS\n";
    return 1;
  }
  const std::string input_path = argv[1];
  const std::string output_path = argv[2];
  const double sample_rate = std::stod(argv[3]);
  const int channels = std::stoi(argv[4]);
  if (sample_rate < 8000.0 || sample_rate > 192000.0 || channels < 1 ||
      channels > 8) {
    std::cerr << "Formato inválido.\n";
    return 1;
  }

  std::vector<float> audio = read_float32(input_path);
  if (audio.size() % static_cast<size_t>(channels) != 0) {
    std::cerr << "Número de amostras incompatível com canais.\n";
    return 1;
  }

  const std::vector<Coefficients> coefficients{
      make_coefficients(FilterType::kLowShelf, sample_rate, 150.0, 1.1, 6.8),
      make_coefficients(FilterType::kPeak, sample_rate, 250.0, 1.0, 9.24),
      make_coefficients(FilterType::kPeak, sample_rate, 500.0, 1.0, 5.26),
      make_coefficients(FilterType::kPeak, sample_rate, 1000.0, 1.0, 2.92),
      make_coefficients(FilterType::kPeak, sample_rate, 2000.0, 1.0, 3.12),
      make_coefficients(FilterType::kPeak, sample_rate, 3000.0, 1.0, 1.92),
      make_coefficients(FilterType::kHighShelf, sample_rate, 8000.0, 1.0,
                        3.56),
  };
  std::vector<std::vector<State>> states(
      static_cast<size_t>(channels),
      std::vector<State>(coefficients.size()));
  std::vector<double> limiter_gain(static_cast<size_t>(channels), 1.0);
  const double ceiling = std::pow(10.0, -0.3 / 20.0);
  const double release = std::exp(-1.0 / (sample_rate * 0.5));
  const size_t frames = audio.size() / static_cast<size_t>(channels);

  for (size_t frame = 0; frame < frames; ++frame) {
    for (int channel = 0; channel < channels; ++channel) {
      const size_t index = frame * static_cast<size_t>(channels) + channel;
      double sample = audio[index];
      for (size_t filter = 0; filter < coefficients.size(); ++filter) {
        sample = process_biquad(sample, coefficients[filter],
                                states[channel][filter]);
      }
      const double required =
          std::min(1.0, ceiling / std::max(std::abs(sample), 1e-12));
      double& gain = limiter_gain[channel];
      if (required < gain) {
        gain = required;
      } else {
        gain = release * gain + (1.0 - release) * required;
      }
      audio[index] = static_cast<float>(sample * gain);
    }
  }
  write_float32(output_path, audio);
  return 0;
}
