#include "voice_dsp.h"

#include "rnnoise.h"

#include <algorithm>
#include <cmath>
#include <cstring>

namespace VoiceMatch {

namespace {
constexpr double kPi = 3.14159265358979323846;
constexpr std::array<double, kAdaptiveBandCount> kTargetFemaleVoiceProfileDb {
	-11.0011,
	-5.3057,
	-4.8488,
	-6.7694,
	-11.9676,
	-19.0887,
	-24.4395,
	-30.7931,
};
}

void Biquad::reset ()
{
	z1_ = 0.0;
	z2_ = 0.0;
}

void Biquad::normalize (
	double b0, double b1, double b2, double a0, double a1, double a2)
{
	b0_ = b0 / a0;
	b1_ = b1 / a0;
	b2_ = b2 / a0;
	a1_ = a1 / a0;
	a2_ = a2 / a0;
}

void Biquad::setHighPass (double sampleRate, double frequency, double q)
{
	const double w0 = 2.0 * kPi * frequency / sampleRate;
	const double cosine = std::cos (w0);
	const double sine = std::sin (w0);
	const double alpha = sine / (2.0 * q);
	normalize ((1.0 + cosine) * 0.5, -(1.0 + cosine),
	           (1.0 + cosine) * 0.5, 1.0 + alpha, -2.0 * cosine, 1.0 - alpha);
}

void Biquad::setBandPass (double sampleRate, double frequency, double q)
{
	const double w0 = 2.0 * kPi * frequency / sampleRate;
	const double cosine = std::cos (w0);
	const double sine = std::sin (w0);
	const double alpha = sine / (2.0 * q);
	normalize (alpha, 0.0, -alpha, 1.0 + alpha, -2.0 * cosine, 1.0 - alpha);
}

void Biquad::setPeak (
	double sampleRate, double frequency, double q, double gainDb)
{
	const double a = std::pow (10.0, gainDb / 40.0);
	const double w0 = 2.0 * kPi * frequency / sampleRate;
	const double cosine = std::cos (w0);
	const double sine = std::sin (w0);
	const double alpha = sine / (2.0 * q);
	normalize (1.0 + alpha * a, -2.0 * cosine, 1.0 - alpha * a,
	           1.0 + alpha / a, -2.0 * cosine, 1.0 - alpha / a);
}

void Biquad::setHighShelf (
	double sampleRate, double frequency, double q, double gainDb)
{
	const double a = std::pow (10.0, gainDb / 40.0);
	const double w0 = 2.0 * kPi * frequency / sampleRate;
	const double cosine = std::cos (w0);
	const double sine = std::sin (w0);
	const double alpha = sine / (2.0 * q);
	const double beta = 2.0 * std::sqrt (a) * alpha;

	normalize (
		a * ((a + 1.0) + (a - 1.0) * cosine + beta),
		-2.0 * a * ((a - 1.0) + (a + 1.0) * cosine),
		a * ((a + 1.0) + (a - 1.0) * cosine - beta),
		(a + 1.0) - (a - 1.0) * cosine + beta,
		2.0 * ((a - 1.0) - (a + 1.0) * cosine),
		(a + 1.0) - (a - 1.0) * cosine - beta);
}

float Biquad::process (float input)
{
	const double output = b0_ * input + z1_;
	z1_ = b1_ * input - a1_ * output + z2_;
	z2_ = b2_ * input - a2_ * output;
	return static_cast<float> (output);
}

VoiceDsp::VoiceDsp () = default;

VoiceDsp::~VoiceDsp ()
{
	if (rnnoise_)
		rnnoise_destroy (rnnoise_);
}

bool VoiceDsp::initialize (double sampleRate)
{
	sampleRate_ = sampleRate > 1000.0 ? sampleRate : 48000.0;
	if (!rnnoise_)
		rnnoise_ = rnnoise_create (nullptr);
	configureFilters ();
	reset ();
	return rnnoise_ != nullptr;
}

void VoiceDsp::reset ()
{
	if (rnnoise_)
		rnnoise_init (rnnoise_, nullptr);
	rnInput_.fill (0.0f);
	rnOutput_.fill (0.0f);
	rnOutputVad_.fill (0.0f);
	dryDelay_.fill (0.0f);
	limiterDelay_.fill (0.0f);
	inputFramePos_ = 0;
	outputFramePos_ = kRnnoiseFrameSize;
	dryDelayPos_ = 0;
	limiterDelayPos_ = 0;
	previousVad_ = 0.0f;
	focusGain_ = 1.0f;
	compressorEnvelope_ = 0.0f;
	limiterGain_ = 1.0f;
	autoGainDb_ = params_.inputGainDb;
	voiceEnergy_ = 0.0;
	voicedAnalysisSamples_ = 0;
	adaptiveUpdateCounter_ = 0;
	adaptiveBandEnergy_.fill (0.0);
	adaptiveGainDb_.fill (0.0);
	naturalLevelEnergy_ = 0.0;
	naturalLowEnergy_ = 0.0;
	naturalMidEnergy_ = 0.0;
	naturalSibilanceEnergy_ = 0.0;
	naturalGainDb_ = params_.inputGainDb - 3.0f;
	naturalProximityCutDb_ = 0.0;
	naturalSibilanceCutDb_ = 0.0;
	naturalVoicedSamples_ = 0;
	naturalUpdateCounter_ = 0;
	naturalCompressorEnvelope_ = 0.0f;
	for (std::size_t band = 0; band < kAdaptiveBandCount; ++band)
	{
		const auto spec = kAdaptiveBands[band];
		const double center = std::sqrt (spec.lowHz * spec.highHz);
		const double q = center / (spec.highHz - spec.lowHz);
		adaptiveAnalyzers_[band].setBandPass (sampleRate_, center, q);
		adaptiveAnalyzers_[band].reset ();
		adaptiveCorrections_[band].setPeak (sampleRate_, center, q, 0.0);
		adaptiveCorrections_[band].reset ();
	}
	highPass_.reset ();
	highPass2_.reset ();
	lowCut_.reset ();
	weight120_.reset ();
	weight200Cut_.reset ();
	weight250_.reset ();
	weight315Cut_.reset ();
	lowMid_.reset ();
	mid_.reset ();
	oneK_.reset ();
	presence_.reset ();
	clarity_.reset ();
	presenceNotch_.reset ();
	sibilance_.reset ();
	airNotch_.reset ();
	highShelf_.reset ();
	naturalHighPass_.reset ();
	naturalBody_.reset ();
	naturalMud_.reset ();
	naturalPresence_.reset ();
	naturalAir_.reset ();
	naturalLowAnalyzer_.reset ();
	naturalMidAnalyzer_.reset ();
	naturalSibilanceAnalyzer_.reset ();
	naturalProximityControl_.setPeak (sampleRate_, 210.0, 0.70, 0.0);
	naturalProximityControl_.reset ();
	naturalSibilanceControl_.setPeak (sampleRate_, 6200.0, 0.85, 0.0);
	naturalSibilanceControl_.reset ();
	matchHighPass_.reset ();
	matchHighPass2_.reset ();
	matchMid_.reset ();
	matchClarity_.reset ();
	matchSibilance_.reset ();
	matchAir_.reset ();
}

void VoiceDsp::setParameters (const DspParameters& parameters)
{
	params_ = parameters;
	params_.amount = clamp01 (params_.amount);
	params_.noiseReduction = clamp01 (params_.noiseReduction);
	params_.enhancement = clamp01 (params_.enhancement);
	params_.voiceFocus = clamp01 (params_.voiceFocus);
	params_.inputGainDb = std::clamp (params_.inputGainDb, -6.0f, 24.0f);
	params_.diagnosticStage = std::clamp (params_.diagnosticStage, 1, 8);
	configureFilters ();
}

float VoiceDsp::clamp01 (float value)
{
	return std::clamp (value, 0.0f, 1.0f);
}

float VoiceDsp::dbToLinear (float db)
{
	return std::pow (10.0f, db / 20.0f);
}

void VoiceDsp::configureFilters ()
{
	const double enhance = params_.enhancement;
	highPass_.setHighPass (sampleRate_, 72.0, 0.707);
	highPass2_.setHighPass (sampleRate_, 78.0, 0.707);
	lowCut_.setPeak (sampleRate_, 145.0, 0.80, -1.7 * enhance);
	weight120_.setPeak (sampleRate_, 120.0, 2.20, 3.8 * enhance);
	weight200Cut_.setPeak (sampleRate_, 200.0, 5.00, -1.5 * enhance);
	weight250_.setPeak (sampleRate_, 250.0, 5.00, 4.5 * enhance);
	weight315Cut_.setPeak (sampleRate_, 315.0, 5.00, -1.6 * enhance);
	lowMid_.setPeak (sampleRate_, 330.0, 0.80, 4.3 * enhance);
	mid_.setPeak (sampleRate_, 820.0, 0.90, -1.2 * enhance);
	oneK_.setPeak (sampleRate_, 1000.0, 1.50, -1.2 * enhance);
	presence_.setPeak (sampleRate_, 2100.0, 0.80, -0.4 * enhance);
	clarity_.setPeak (sampleRate_, 3400.0, 1.20, 2.3 * enhance);
	presenceNotch_.setPeak (sampleRate_, 4000.0, 1.30, -1.2 * enhance);
	sibilance_.setPeak (sampleRate_, 5900.0, 1.20, -5.0 * enhance);
	airNotch_.setPeak (sampleRate_, 8000.0, 1.60, -3.4 * enhance);
	highShelf_.setHighShelf (sampleRate_, 11000.0, 0.707, 11.3 * enhance);

	// Natural baseline: broad, intentionally small moves. Unlike the rejected
	// chain, these filters do not attempt to impose a fixed female formant curve.
	naturalHighPass_.setHighPass (sampleRate_, 72.0, 0.707);
	naturalBody_.setPeak (sampleRate_, 190.0, 0.65, 1.6 * enhance);
	naturalMud_.setPeak (sampleRate_, 520.0, 0.70, -0.7 * enhance);
	naturalPresence_.setPeak (sampleRate_, 2850.0, 0.75, 0.8 * enhance);
	naturalAir_.setHighShelf (sampleRate_, 10500.0, 0.707, 0.8 * enhance);
	naturalLowAnalyzer_.setBandPass (sampleRate_, 205.0, 0.80);
	naturalMidAnalyzer_.setBandPass (sampleRate_, 1150.0, 0.65);
	naturalSibilanceAnalyzer_.setBandPass (sampleRate_, 6200.0, 0.85);

	// Correction measured after Premiere Enhance Speech at 7.0 and Dialogue
	// Auto-Match at -23 LUFS. It intentionally leaves denoising to Premiere.
	const double highPassFrequency = 55.0 + 20.0 * params_.noiseReduction;
	matchHighPass_.setHighPass (sampleRate_, highPassFrequency, 0.707);
	matchHighPass2_.setHighPass (sampleRate_, highPassFrequency, 0.707);
	matchMid_.setPeak (
		sampleRate_, 800.0, 0.75, 1.0 * params_.enhancement);
	matchClarity_.setPeak (
		sampleRate_, 3200.0, 0.80, -4.0 * params_.enhancement);
	matchSibilance_.setPeak (
		sampleRate_, 6500.0, 0.90, 1.8 * params_.voiceFocus);
	matchAir_.setHighShelf (
		sampleRate_, 9000.0, 0.707, 4.8 * params_.voiceFocus);
}

void VoiceDsp::processCollectedFrame ()
{
	if (!rnnoise_)
	{
		rnOutput_ = rnInput_;
		rnOutputVad_.fill (1.0f);
		return;
	}

	std::array<float, kRnnoiseFrameSize> scaledInput {};
	std::array<float, kRnnoiseFrameSize> scaledOutput {};
	for (std::size_t i = 0; i < kRnnoiseFrameSize; ++i)
		scaledInput[i] = rnInput_[i] * 32768.0f;

	const float currentVad =
		clamp01 (rnnoise_process_frame (rnnoise_, scaledOutput.data (), scaledInput.data ()));

	for (std::size_t i = 0; i < kRnnoiseFrameSize; ++i)
	{
		rnOutput_[i] = scaledOutput[i] / 32768.0f;
		rnOutputVad_[i] = previousVad_;
	}
	previousVad_ = currentVad;
}

float VoiceDsp::processAdaptiveEq (float sample, float speechConfidence)
{
	std::array<float, kAdaptiveBandCount> analyzed {};
	for (std::size_t band = 0; band < kAdaptiveBandCount; ++band)
		analyzed[band] = adaptiveAnalyzers_[band].process (sample);

	if (speechConfidence >= 0.48f && std::abs (sample) >= 1.0e-5f)
	{
		const double coefficient = std::exp (-1.0 / (1.8 * sampleRate_));
		voiceEnergy_ =
			coefficient * voiceEnergy_ +
			(1.0 - coefficient) * static_cast<double> (sample) * sample;
		for (std::size_t band = 0; band < kAdaptiveBandCount; ++band)
		{
			const double value = analyzed[band];
			adaptiveBandEnergy_[band] =
				coefficient * adaptiveBandEnergy_[band] +
				(1.0 - coefficient) * value * value;
		}
		++voicedAnalysisSamples_;
	}

	if (++adaptiveUpdateCounter_ >= kRnnoiseFrameSize)
	{
		adaptiveUpdateCounter_ = 0;
		updateAdaptiveTargets ();
	}

	float output = sample;
	for (auto& correction : adaptiveCorrections_)
		output = correction.process (output);
	return output;
}

void VoiceDsp::updateAdaptiveTargets ()
{
	const auto minimumVoicedSamples =
		static_cast<std::uint64_t> (std::max (sampleRate_ * 0.12, 1.0));
	if (voicedAnalysisSamples_ < minimumVoicedSamples)
		return;

	double totalEnergy = 0.0;
	for (double energy : adaptiveBandEnergy_)
		totalEnergy += energy;
	if (totalEnergy <= 1.0e-18)
		return;

	const double maximumCorrection = 4.0 * params_.enhancement;
	for (std::size_t band = 0; band < kAdaptiveBandCount; ++band)
	{
		const double currentProfileDb =
			10.0 * std::log10 ((adaptiveBandEnergy_[band] + 1.0e-20) / totalEnergy);
		const double errorDb = kTargetFemaleVoiceProfileDb[band] - currentProfileDb;
		const double desiredGain =
			std::clamp (errorDb * 0.62, -maximumCorrection, maximumCorrection);
		adaptiveGainDb_[band] += 0.035 * (desiredGain - adaptiveGainDb_[band]);

		const auto spec = kAdaptiveBands[band];
		const double center = std::sqrt (spec.lowHz * spec.highHz);
		const double q = center / (spec.highHz - spec.lowHz);
		adaptiveCorrections_[band].setPeak (
			sampleRate_, center, q, adaptiveGainDb_[band]);
	}

	const double measuredVoiceDb =
		10.0 * std::log10 (std::max (voiceEnergy_, 1.0e-18));
	const double userOffsetDb = params_.inputGainDb - 18.3;
	const double desiredAutoGain =
		std::clamp (-17.1 - measuredVoiceDb + userOffsetDb, -6.0, 24.0);
	autoGainDb_ += static_cast<float> (0.012 * (desiredAutoGain - autoGainDb_));
}

float VoiceDsp::processDynamics (float sample)
{
	const float absolute = std::abs (sample);
	const float attack = std::exp (-1.0f / static_cast<float> (0.010 * sampleRate_));
	const float release = std::exp (-1.0f / static_cast<float> (0.130 * sampleRate_));
	const float coefficient = absolute > compressorEnvelope_ ? attack : release;
	compressorEnvelope_ = coefficient * compressorEnvelope_ + (1.0f - coefficient) * absolute;

	const float thresholdDb = -19.0f;
	const float ratio = 1.0f + 0.08f * params_.enhancement;
	const float levelDb = 20.0f * std::log10 (std::max (compressorEnvelope_, 1.0e-8f));
	float gainDb = 0.0f;
	if (levelDb > thresholdDb)
		gainDb = thresholdDb + (levelDb - thresholdDb) / ratio - levelDb;
	return sample * dbToLinear (gainDb);
}

void VoiceDsp::updateNaturalTargets ()
{
	const auto minimumSamples =
		static_cast<std::uint64_t> (std::max (sampleRate_ * 0.18, 1.0));
	if (naturalVoicedSamples_ < minimumSamples ||
	    naturalMidEnergy_ <= 1.0e-18 ||
	    naturalLevelEnergy_ <= 1.0e-18)
		return;

	const double lowToMidDb =
		10.0 * std::log10 (
			(naturalLowEnergy_ + 1.0e-20) /
			(naturalMidEnergy_ + 1.0e-20));
	const double sibilanceToMidDb =
		10.0 * std::log10 (
			(naturalSibilanceEnergy_ + 1.0e-20) /
			(naturalMidEnergy_ + 1.0e-20));

	// Only control clear excesses. Missing bands are never boosted to a target,
	// preserving the speaker's formants and identity.
	const double desiredProximityCut =
		std::clamp (-0.55 * (lowToMidDb - 3.0), -3.0, 0.0);
	const double desiredSibilanceCut =
		std::clamp (-0.65 * (sibilanceToMidDb + 11.0), -4.0, 0.0);
	naturalProximityCutDb_ +=
		0.025 * (desiredProximityCut - naturalProximityCutDb_);
	naturalSibilanceCutDb_ +=
		0.050 * (desiredSibilanceCut - naturalSibilanceCutDb_);

	naturalProximityControl_.setPeak (
		sampleRate_, 210.0, 0.70, naturalProximityCutDb_);
	naturalSibilanceControl_.setPeak (
		sampleRate_, 6200.0, 0.85, naturalSibilanceCutDb_);

	const double measuredDb =
		10.0 * std::log10 (std::max (naturalLevelEnergy_, 1.0e-18));
	const double userOffsetDb = params_.inputGainDb - 18.3;
	const double desiredGain =
		std::clamp (-18.0 - measuredDb + userOffsetDb, 3.0, 21.0);
	// Approximately 2.5 seconds to settle: slow enough to avoid word pumping.
	naturalGainDb_ +=
		static_cast<float> (0.004 * (desiredGain - naturalGainDb_));
}

float VoiceDsp::processNaturalDynamics (float sample)
{
	const float absolute = std::abs (sample);
	const float attack =
		std::exp (-1.0f / static_cast<float> (0.018 * sampleRate_));
	const float release =
		std::exp (-1.0f / static_cast<float> (0.180 * sampleRate_));
	const float coefficient =
		absolute > naturalCompressorEnvelope_ ? attack : release;
	naturalCompressorEnvelope_ =
		coefficient * naturalCompressorEnvelope_ +
		(1.0f - coefficient) * absolute;

	const float thresholdDb = -12.0f;
	const float ratio = 1.35f;
	const float levelDb =
		20.0f * std::log10 (std::max (naturalCompressorEnvelope_, 1.0e-8f));
	const float gainDb = levelDb > thresholdDb
		? thresholdDb + (levelDb - thresholdDb) / ratio - levelDb
		: 0.0f;
	return sample * dbToLinear (gainDb);
}

float VoiceDsp::processNaturalChain (
	float dryDelayed, float denoised, float vad)
{
	const float reduction = 0.55f * params_.noiseReduction;
	float processed = dryDelayed + reduction * (denoised - dryDelayed);

	processed = naturalHighPass_.process (processed);
	processed = naturalBody_.process (processed);
	processed = naturalMud_.process (processed);
	processed = naturalPresence_.process (processed);
	processed = naturalAir_.process (processed);

	const float low = naturalLowAnalyzer_.process (processed);
	const float mid = naturalMidAnalyzer_.process (processed);
	const float sibilance = naturalSibilanceAnalyzer_.process (processed);
	const float speechConfidence = clamp01 ((vad - 0.12f) / 0.48f);
	if (speechConfidence >= 0.35f && std::abs (processed) >= 1.0e-5f)
	{
		const double spectralCoefficient =
			std::exp (-1.0 / (0.55 * sampleRate_));
		const double levelCoefficient =
			std::exp (-1.0 / (2.5 * sampleRate_));
		naturalLowEnergy_ =
			spectralCoefficient * naturalLowEnergy_ +
			(1.0 - spectralCoefficient) * low * low;
		naturalMidEnergy_ =
			spectralCoefficient * naturalMidEnergy_ +
			(1.0 - spectralCoefficient) * mid * mid;
		naturalSibilanceEnergy_ =
			spectralCoefficient * naturalSibilanceEnergy_ +
			(1.0 - spectralCoefficient) * sibilance * sibilance;
		naturalLevelEnergy_ =
			levelCoefficient * naturalLevelEnergy_ +
			(1.0 - levelCoefficient) * processed * processed;
		++naturalVoicedSamples_;
	}

	if (++naturalUpdateCounter_ >= kRnnoiseFrameSize)
	{
		naturalUpdateCounter_ = 0;
		updateNaturalTargets ();
	}

	processed = naturalProximityControl_.process (processed);
	processed = naturalSibilanceControl_.process (processed);
	processed *= dbToLinear (naturalGainDb_);
	return processNaturalDynamics (processed);
}

float VoiceDsp::processLimiter (float sample, bool bypass)
{
	const float delayed = limiterDelay_[limiterDelayPos_];
	limiterDelay_[limiterDelayPos_] = sample;
	limiterDelayPos_ = (limiterDelayPos_ + 1) % kLimiterLookahead;

	if (bypass)
		return delayed;

	const float ceiling = dbToLinear (-0.32f);
	const float target = std::abs (sample) > ceiling
		? ceiling / std::max (std::abs (sample), 1.0e-8f)
		: 1.0f;
	if (target < limiterGain_)
		limiterGain_ = target;
	else
	{
		const float release =
			std::exp (-1.0f / static_cast<float> (0.500 * sampleRate_));
		limiterGain_ = release * limiterGain_ + (1.0f - release);
	}
	return std::clamp (delayed * limiterGain_, -ceiling, ceiling);
}

float VoiceDsp::processMonoSample (float input)
{
	if (params_.diagnosticStage == 8)
	{
		float processed = matchHighPass_.process (input);
		processed = matchHighPass2_.process (processed);
		processed = matchMid_.process (processed);
		processed = matchClarity_.process (processed);
		processed = matchSibilance_.process (processed);
		processed = matchAir_.process (processed);
		processed *= dbToLinear (params_.inputGainDb);
		const float mixed = input + params_.amount * (processed - input);
		return processLimiter (
			params_.bypass ? input : mixed, params_.bypass);
	}

	const float dryDelayed = dryDelay_[dryDelayPos_];
	dryDelay_[dryDelayPos_] = input;
	dryDelayPos_ = (dryDelayPos_ + 1) % kRnnoisePipelineLatency;

	rnInput_[inputFramePos_++] = input;
	if (inputFramePos_ == kRnnoiseFrameSize)
	{
		processCollectedFrame ();
		inputFramePos_ = 0;
		outputFramePos_ = 0;
	}

	float denoised = 0.0f;
	float vad = 0.0f;
	if (outputFramePos_ < kRnnoiseFrameSize)
	{
		denoised = rnOutput_[outputFramePos_];
		vad = rnOutputVad_[outputFramePos_];
		++outputFramePos_;
	}

	const float reduction = params_.noiseReduction;
	float processed = dryDelayed + reduction * (denoised - dryDelayed);

	if (params_.diagnosticStage == 7)
	{
		processed = processNaturalChain (dryDelayed, denoised, vad);
		const float mixed =
			dryDelayed + params_.amount * (processed - dryDelayed);
		return processLimiter (
			params_.bypass ? dryDelayed : mixed, params_.bypass);
	}

	if (params_.diagnosticStage == 1)
		processed = dryDelayed;

	const float speechConfidence = clamp01 ((vad - 0.18f) / 0.56f);
	if (params_.diagnosticStage >= 3)
	{
		const float maximumAttenuationDb = -17.0f * params_.voiceFocus;
		const float targetFocusGain =
			dbToLinear (maximumAttenuationDb * (1.0f - speechConfidence));
		const float focusAttack =
			std::exp (-1.0f / static_cast<float> (0.018 * sampleRate_));
		const float focusRelease =
			std::exp (-1.0f / static_cast<float> (0.220 * sampleRate_));
		const float focusCoefficient =
			targetFocusGain < focusGain_ ? focusAttack : focusRelease;
		focusGain_ =
			focusCoefficient * focusGain_ +
			(1.0f - focusCoefficient) * targetFocusGain;
		processed *= focusGain_;
	}

	if (params_.diagnosticStage >= 4)
	{
		processed = highPass_.process (processed);
		processed = highPass2_.process (processed);
		processed = lowCut_.process (processed);
		processed = weight120_.process (processed);
		processed = weight200Cut_.process (processed);
		processed = weight250_.process (processed);
		processed = weight315Cut_.process (processed);
		processed = lowMid_.process (processed);
		processed = mid_.process (processed);
		processed = oneK_.process (processed);
		processed = presence_.process (processed);
		processed = clarity_.process (processed);
		processed = presenceNotch_.process (processed);
		processed = sibilance_.process (processed);
		processed = airNotch_.process (processed);
		processed = highShelf_.process (processed);
	}

	if (params_.diagnosticStage >= 5)
		processed = processAdaptiveEq (processed, speechConfidence);

	if (params_.diagnosticStage >= 6)
	{
		processed *= dbToLinear (autoGainDb_);
		processed = processDynamics (processed);
	}

	const float mixed = dryDelayed + params_.amount * (processed - dryDelayed);
	return processLimiter (params_.bypass ? dryDelayed : mixed, params_.bypass);
}

} // namespace VoiceMatch
