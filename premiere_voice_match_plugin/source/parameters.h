#pragma once

#include "pluginterfaces/vst/vsttypes.h"

namespace VoiceMatch {

enum ParameterIds : Steinberg::Vst::ParamID
{
	kAmountId = 100,
	kNoiseReductionId = 101,
	kEnhancementId = 102,
	kVoiceFocusId = 103,
	kInputGainId = 104,
	kBypassId = 105,
};

constexpr double kDefaultAmount = 1.0;
constexpr double kDefaultNoiseReduction = 1.0;
constexpr double kDefaultEnhancement = 1.0;
constexpr double kDefaultVoiceFocus = 1.0;
constexpr double kMinInputGainDb = -6.0;
constexpr double kMaxInputGainDb = 24.0;
constexpr double kDefaultInputGainDb = 7.8;
constexpr double kOutputCeilingDb = -0.32;

inline double gainDbToNormalized (double gainDb)
{
	return (gainDb - kMinInputGainDb) / (kMaxInputGainDb - kMinInputGainDb);
}

inline double normalizedToGainDb (double normalized)
{
	return kMinInputGainDb + normalized * (kMaxInputGainDb - kMinInputGainDb);
}

} // namespace VoiceMatch
