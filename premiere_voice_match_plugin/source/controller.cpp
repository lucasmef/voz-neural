#include "controller.h"

#include "base/source/fstreamer.h"
#include "parameters.h"
#include "public.sdk/source/vst/vstparameters.h"

namespace VoiceMatch {

using namespace Steinberg;
using namespace Steinberg::Vst;

tresult PLUGIN_API Controller::initialize (FUnknown* context)
{
	const tresult result = EditController::initialize (context);
	if (result != kResultTrue)
		return result;

	auto addPercent = [this] (const TChar* title, ParamID id, double defaultValue) {
		auto* parameter =
			new RangeParameter (title, id, STR16 ("%"), 0.0, 100.0,
			                    defaultValue * 100.0);
		parameter->setPrecision (0);
		parameters.addParameter (parameter);
	};

	addPercent (STR16 ("Intensidade"), kAmountId, kDefaultAmount);
	addPercent (STR16 ("Corte de subgrave"), kNoiseReductionId, kDefaultNoiseReduction);
	addPercent (STR16 ("Correcao de presenca"), kEnhancementId, kDefaultEnhancement);
	addPercent (STR16 ("Ar e detalhe"), kVoiceFocusId, kDefaultVoiceFocus);

	auto* gain = new RangeParameter (
		STR16 ("Nivel final"), kInputGainId, STR16 ("dB"),
		kMinInputGainDb, kMaxInputGainDb, kDefaultInputGainDb);
	gain->setPrecision (1);
	parameters.addParameter (gain);

	parameters.addParameter (
		STR16 ("Bypass"), nullptr, 1, 0.0,
		ParameterInfo::kCanAutomate | ParameterInfo::kIsBypass, kBypassId);

	return kResultTrue;
}

tresult PLUGIN_API Controller::setComponentState (IBStream* state)
{
	if (!state)
		return kResultFalse;
	IBStreamer stream (state, kLittleEndian);
	float amount = 0.0f;
	float noise = 0.0f;
	float enhancement = 0.0f;
	float focus = 0.0f;
	float gain = 0.0f;
	int32 bypass = 0;
	if (!stream.readFloat (amount) || !stream.readFloat (noise) ||
	    !stream.readFloat (enhancement) || !stream.readFloat (focus) ||
	    !stream.readFloat (gain) || !stream.readInt32 (bypass))
		return kResultFalse;
	setParamNormalized (kAmountId, amount);
	setParamNormalized (kNoiseReductionId, noise);
	setParamNormalized (kEnhancementId, enhancement);
	setParamNormalized (kVoiceFocusId, focus);
	setParamNormalized (kInputGainId, gainDbToNormalized (gain));
	setParamNormalized (kBypassId, bypass ? 1.0 : 0.0);
	return kResultOk;
}

} // namespace VoiceMatch
