#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

struct DenoiseState;

namespace VoiceMatch {

struct DspParameters
{
	float amount {1.0f};
	float noiseReduction {1.0f};
	float enhancement {1.0f};
	float voiceFocus {1.0f};
	float inputGainDb {7.8f};
	bool bypass {false};
	// Offline diagnostic only. The plug-in always leaves this at the full chain.
	// 1=dry, 2=denoise blend, 3=+focus, 4=+static EQ, 5=+adaptive EQ,
	// 6=+auto level/dynamics, 7=new natural chain.
	// 8=correction measured after Premiere Enhance Speech + Auto-Match.
	int diagnosticStage {8};
};

struct AdaptiveBandSpec
{
	double lowHz;
	double highHz;
};

inline constexpr std::size_t kAdaptiveBandCount = 8;
inline constexpr std::array<AdaptiveBandSpec, kAdaptiveBandCount> kAdaptiveBands {{
	{90.0, 180.0},
	{180.0, 350.0},
	{350.0, 700.0},
	{700.0, 1400.0},
	{1400.0, 2800.0},
	{2800.0, 4800.0},
	{4800.0, 8500.0},
	{8500.0, 16000.0},
}};

class Biquad
{
public:
	void reset ();
	void setHighPass (double sampleRate, double frequency, double q);
	void setBandPass (double sampleRate, double frequency, double q);
	void setPeak (double sampleRate, double frequency, double q, double gainDb);
	void setHighShelf (double sampleRate, double frequency, double q, double gainDb);
	float process (float input);

private:
	void normalize (double b0, double b1, double b2, double a0, double a1, double a2);

	double b0_ {1.0};
	double b1_ {0.0};
	double b2_ {0.0};
	double a1_ {0.0};
	double a2_ {0.0};
	double z1_ {0.0};
	double z2_ {0.0};
};

class VoiceDsp
{
public:
	static constexpr std::uint32_t kRnnoiseFrameSize = 480;
	// RNNoise delivers speech one extra 480-sample frame behind the block
	// scheduler. The dry signal must include that frame before parallel mixing.
	static constexpr std::uint32_t kRnnoisePipelineLatency = 1440;
	static constexpr std::uint32_t kLimiterLookahead = 240;
	static constexpr std::uint32_t kTotalLatency = kLimiterLookahead;

	VoiceDsp ();
	~VoiceDsp ();

	VoiceDsp (const VoiceDsp&) = delete;
	VoiceDsp& operator= (const VoiceDsp&) = delete;

	bool initialize (double sampleRate);
	void reset ();
	void setParameters (const DspParameters& parameters);
	float processMonoSample (float input);

private:
	static float clamp01 (float value);
	static float dbToLinear (float db);
	void configureFilters ();
	void processCollectedFrame ();
	float processAdaptiveEq (float sample, float speechConfidence);
	void updateAdaptiveTargets ();
	float processDynamics (float sample);
	float processNaturalChain (float dryDelayed, float denoised, float vad);
	void updateNaturalTargets ();
	float processNaturalDynamics (float sample);
	float processLimiter (float sample, bool bypass);

	double sampleRate_ {48000.0};
	DspParameters params_ {};
	DenoiseState* rnnoise_ {nullptr};

	std::array<float, kRnnoiseFrameSize> rnInput_ {};
	std::array<float, kRnnoiseFrameSize> rnOutput_ {};
	std::array<float, kRnnoiseFrameSize> rnOutputVad_ {};
	std::array<float, kRnnoisePipelineLatency> dryDelay_ {};
	std::array<float, kLimiterLookahead> limiterDelay_ {};

	std::uint32_t inputFramePos_ {0};
	std::uint32_t outputFramePos_ {kRnnoiseFrameSize};
	std::uint32_t dryDelayPos_ {0};
	std::uint32_t limiterDelayPos_ {0};
	float previousVad_ {0.0f};
	float focusGain_ {1.0f};
	float compressorEnvelope_ {0.0f};
	float limiterGain_ {1.0f};
	float autoGainDb_ {18.3f};
	double voiceEnergy_ {0.0};
	std::uint64_t voicedAnalysisSamples_ {0};
	std::uint32_t adaptiveUpdateCounter_ {0};
	std::array<double, kAdaptiveBandCount> adaptiveBandEnergy_ {};
	std::array<double, kAdaptiveBandCount> adaptiveGainDb_ {};
	std::array<Biquad, kAdaptiveBandCount> adaptiveAnalyzers_ {};
	std::array<Biquad, kAdaptiveBandCount> adaptiveCorrections_ {};
	double naturalLevelEnergy_ {0.0};
	double naturalLowEnergy_ {0.0};
	double naturalMidEnergy_ {0.0};
	double naturalSibilanceEnergy_ {0.0};
	float naturalGainDb_ {15.3f};
	double naturalProximityCutDb_ {0.0};
	double naturalSibilanceCutDb_ {0.0};
	std::uint64_t naturalVoicedSamples_ {0};
	std::uint32_t naturalUpdateCounter_ {0};
	float naturalCompressorEnvelope_ {0.0f};
	Biquad naturalHighPass_;
	Biquad naturalBody_;
	Biquad naturalMud_;
	Biquad naturalPresence_;
	Biquad naturalAir_;
	Biquad naturalLowAnalyzer_;
	Biquad naturalMidAnalyzer_;
	Biquad naturalSibilanceAnalyzer_;
	Biquad naturalProximityControl_;
	Biquad naturalSibilanceControl_;
	Biquad matchHighPass_;
	Biquad matchHighPass2_;
	Biquad matchMid_;
	Biquad matchClarity_;
	Biquad matchSibilance_;
	Biquad matchAir_;

	Biquad highPass_;
	Biquad highPass2_;
	Biquad lowCut_;
	Biquad weight120_;
	Biquad weight200Cut_;
	Biquad weight250_;
	Biquad weight315Cut_;
	Biquad lowMid_;
	Biquad mid_;
	Biquad oneK_;
	Biquad presence_;
	Biquad clarity_;
	Biquad presenceNotch_;
	Biquad sibilance_;
	Biquad airNotch_;
	Biquad highShelf_;
};

} // namespace VoiceMatch
