#include <rnnoise.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace {

constexpr std::size_t kFrameSize = 480;

bool readFloatFile (const std::string& path, std::vector<float>& samples)
{
	std::ifstream input (path, std::ios::binary | std::ios::ate);
	if (!input)
		return false;
	const auto byteCount = input.tellg ();
	if (byteCount < 0 ||
	    byteCount % static_cast<std::streamoff> (sizeof (float)) != 0)
		return false;
	samples.resize (
		static_cast<std::size_t> (byteCount) / sizeof (float));
	input.seekg (0);
	input.read (
		reinterpret_cast<char*> (samples.data ()),
		static_cast<std::streamsize> (byteCount));
	return input.good ();
}

bool writeFloatFile (const std::string& path, const std::vector<float>& values)
{
	std::ofstream output (path, std::ios::binary);
	if (!output)
		return false;
	output.write (
		reinterpret_cast<const char*> (values.data ()),
		static_cast<std::streamsize> (
			values.size () * sizeof (float)));
	return output.good ();
}

} // namespace

int main (int argc, char** argv)
{
	if (argc != 3)
	{
		std::cerr << "uso: rnnoise_vad_cli entrada_mono_48k.f32 vad_por_quadro.f32\n";
		return 2;
	}

	std::vector<float> input;
	if (!readFloatFile (argv[1], input))
	{
		std::cerr << "nao foi possivel ler a entrada\n";
		return 1;
	}

	DenoiseState* state = rnnoise_create (nullptr);
	if (!state)
	{
		std::cerr << "nao foi possivel iniciar o RNNoise\n";
		return 1;
	}

	std::array<float, kFrameSize> source {};
	std::array<float, kFrameSize> ignored {};
	std::vector<float> vad;
	vad.reserve ((input.size () + kFrameSize - 1) / kFrameSize);
	for (std::size_t start = 0; start < input.size (); start += kFrameSize)
	{
		source.fill (0.0f);
		const auto count = std::min (kFrameSize, input.size () - start);
		for (std::size_t index = 0; index < count; ++index)
			source[index] = input[start + index] * 32768.0f;
		const float probability =
			rnnoise_process_frame (state, ignored.data (), source.data ());
		vad.push_back (std::clamp (probability, 0.0f, 1.0f));
	}
	rnnoise_destroy (state);

	if (!writeFloatFile (argv[2], vad))
	{
		std::cerr << "nao foi possivel gravar a saida\n";
		return 1;
	}

	std::cout << "quadros=" << vad.size ()
	          << " tamanho_quadro=" << kFrameSize << '\n';
	return 0;
}
