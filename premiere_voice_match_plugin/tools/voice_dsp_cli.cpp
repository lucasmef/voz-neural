#include "voice_dsp.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <numeric>
#include <string>
#include <vector>

namespace {

float argument (int argc, char** argv, int index, float fallback)
{
	return argc > index ? std::strtof (argv[index], nullptr) : fallback;
}

bool readFloatFile (const std::string& path, std::vector<float>& samples)
{
	std::ifstream input (path, std::ios::binary | std::ios::ate);
	if (!input)
		return false;
	const auto byteCount = input.tellg ();
	if (byteCount < 0 || byteCount % static_cast<std::streamoff> (sizeof (float)) != 0)
		return false;
	samples.resize (static_cast<std::size_t> (byteCount) / sizeof (float));
	input.seekg (0);
	input.read (reinterpret_cast<char*> (samples.data ()), byteCount);
	return input.good ();
}

bool writeFloatFile (const std::string& path, const std::vector<float>& samples)
{
	std::ofstream output (path, std::ios::binary);
	if (!output)
		return false;
	output.write (
		reinterpret_cast<const char*> (samples.data ()),
		static_cast<std::streamsize> (samples.size () * sizeof (float)));
	return output.good ();
}

int printProfile (const std::string& path, double sampleRate)
{
	std::vector<float> input;
	if (!readFloatFile (path, input) || input.empty ())
	{
		std::cerr << "nao foi possivel ler a entrada do perfil\n";
		return 1;
	}

	const std::size_t frameSize = std::max<std::size_t> (
		1, static_cast<std::size_t> (std::lround (sampleRate * 0.010)));
	std::vector<double> frameRms;
	for (std::size_t start = 0; start + frameSize <= input.size (); start += frameSize)
	{
		double energy = 0.0;
		for (std::size_t i = 0; i < frameSize; ++i)
			energy += static_cast<double> (input[start + i]) * input[start + i];
		frameRms.push_back (std::sqrt (energy / frameSize));
	}
	auto sorted = frameRms;
	std::sort (sorted.begin (), sorted.end ());
	const double percentile55 = sorted[static_cast<std::size_t> (
		0.55 * static_cast<double> (sorted.size () - 1))];
	const double threshold = std::max (std::pow (10.0, -38.0 / 20.0), percentile55);

	std::array<VoiceMatch::Biquad, VoiceMatch::kAdaptiveBandCount> filters;
	for (std::size_t band = 0; band < VoiceMatch::kAdaptiveBandCount; ++band)
	{
		const auto spec = VoiceMatch::kAdaptiveBands[band];
		const double center = std::sqrt (spec.lowHz * spec.highHz);
		const double q = center / (spec.highHz - spec.lowHz);
		filters[band].setBandPass (sampleRate, center, q);
	}

	std::array<double, VoiceMatch::kAdaptiveBandCount> energies {};
	std::size_t activeSamples = 0;
	for (std::size_t index = 0; index < input.size (); ++index)
	{
		const std::size_t frame = std::min (index / frameSize, frameRms.size () - 1);
		const bool active = frameRms[frame] >= threshold;
		for (std::size_t band = 0; band < VoiceMatch::kAdaptiveBandCount; ++band)
		{
			const double filtered = filters[band].process (input[index]);
			if (active)
				energies[band] += filtered * filtered;
		}
		if (active)
			++activeSamples;
	}

	const double total =
		std::accumulate (energies.begin (), energies.end (), 0.0) + 1.0e-30;
	std::cout << std::fixed << std::setprecision (4)
	          << "{\"active_samples\":" << activeSamples << ",\"profile_db\":[";
	for (std::size_t band = 0; band < VoiceMatch::kAdaptiveBandCount; ++band)
	{
		if (band)
			std::cout << ',';
		std::cout << 10.0 * std::log10 ((energies[band] + 1.0e-30) / total);
	}
	std::cout << "]}\n";
	return 0;
}

} // namespace

int main (int argc, char** argv)
{
	if (argc >= 3 && std::string (argv[1]) == "--profile")
		return printProfile (argv[2], argument (argc, argv, 3, 44100.0f));

	if (argc < 3)
	{
		std::cerr
			<< "uso: voz_clara_cli entrada.f32 saida.f32 "
			   "[sample_rate] [ganho_db] [ruido] [foco] [aprimoramento] "
			   "[intensidade] [estagio_diagnostico_1_a_8]\n";
		return 2;
	}

	const double sampleRate = argument (argc, argv, 3, 48000.0f);
	VoiceMatch::DspParameters parameters;
	parameters.inputGainDb = argument (argc, argv, 4, 7.8f);
	parameters.noiseReduction = argument (argc, argv, 5, 1.0f);
	parameters.voiceFocus = argument (argc, argv, 6, 1.0f);
	parameters.enhancement = argument (argc, argv, 7, 1.0f);
	parameters.amount = argument (argc, argv, 8, 1.0f);
	parameters.diagnosticStage =
		static_cast<int> (argument (argc, argv, 9, 8.0f));

	std::vector<float> input;
	if (std::string (argv[1]) == "--impulse")
	{
		input.assign (4800, 0.0f);
		input[0] = 0.25f;
	}
	else if (!readFloatFile (argv[1], input))
	{
		std::cerr << "nao foi possivel ler a entrada\n";
		return 1;
	}

	VoiceMatch::VoiceDsp dsp;
	if (!dsp.initialize (sampleRate))
	{
		std::cerr << "nao foi possivel iniciar o RNNoise\n";
		return 1;
	}
	dsp.setParameters (parameters);

	std::vector<float> output;
	output.reserve (input.size () + VoiceMatch::VoiceDsp::kTotalLatency + 960);
	for (float sample : input)
		output.push_back (dsp.processMonoSample (sample));
	for (std::uint32_t i = 0; i < VoiceMatch::VoiceDsp::kTotalLatency + 960; ++i)
		output.push_back (dsp.processMonoSample (0.0f));

	if (!writeFloatFile (argv[2], output))
	{
		std::cerr << "nao foi possivel gravar a saida\n";
		return 1;
	}

	std::cout << "amostras_entrada=" << input.size ()
	          << " amostras_saida=" << output.size ()
	          << " latencia_declarada=" << VoiceMatch::VoiceDsp::kTotalLatency << '\n';
	return 0;
}
