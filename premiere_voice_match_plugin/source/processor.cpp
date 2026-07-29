#include "processor.h"

#include "base/source/fstreamer.h"
#include "ids.h"
#include "parameters.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"

#include <algorithm>
#include <cmath>
#include <type_traits>

namespace VoiceMatch {

using namespace Steinberg;
using namespace Steinberg::Vst;

Processor::Processor ()
{
	setControllerClass (ControllerUID);
	params_.amount = static_cast<float> (kDefaultAmount);
	params_.noiseReduction = static_cast<float> (kDefaultNoiseReduction);
	params_.enhancement = static_cast<float> (kDefaultEnhancement);
	params_.voiceFocus = static_cast<float> (kDefaultVoiceFocus);
	params_.inputGainDb = static_cast<float> (kDefaultInputGainDb);
}

tresult PLUGIN_API Processor::initialize (FUnknown* context)
{
	const tresult result = AudioEffect::initialize (context);
	if (result == kResultTrue)
	{
		addAudioInput (STR16 ("Entrada"), SpeakerArr::kStereo);
		addAudioOutput (STR16 ("Saida"), SpeakerArr::kStereo);
	}
	return result;
}

tresult PLUGIN_API Processor::setBusArrangements (
	SpeakerArrangement* inputs, int32 numIns,
	SpeakerArrangement* outputs, int32 numOuts)
{
	if (numIns != 1 || numOuts != 1 || inputs[0] != outputs[0])
		return kResultFalse;
	const int32 channels = SpeakerArr::getChannelCount (inputs[0]);
	if (channels != 1 && channels != 2)
		return kResultFalse;
	const tresult result =
		AudioEffect::setBusArrangements (inputs, numIns, outputs, numOuts);
	if (result == kResultOk)
		channelCount_ = channels;
	return result;
}

tresult PLUGIN_API Processor::canProcessSampleSize (int32 symbolicSampleSize)
{
	return symbolicSampleSize == kSample32 || symbolicSampleSize == kSample64
		? kResultTrue
		: kResultFalse;
}

tresult PLUGIN_API Processor::setupProcessing (ProcessSetup& setup)
{
	const tresult result = AudioEffect::setupProcessing (setup);
	if (result == kResultOk)
		dsp_.initialize (setup.sampleRate);
	return result;
}

tresult PLUGIN_API Processor::setActive (TBool state)
{
	if (state)
		dsp_.initialize (processSetup.sampleRate);
	else
		dsp_.reset ();
	return AudioEffect::setActive (state);
}

tresult PLUGIN_API Processor::setProcessing (TBool state)
{
	if (state)
		dsp_.reset ();
	return kResultOk;
}

void Processor::readParameterChanges (IParameterChanges* changes)
{
	if (!changes)
		return;
	for (int32 index = 0; index < changes->getParameterCount (); ++index)
	{
		IParamValueQueue* queue = changes->getParameterData (index);
		if (!queue || queue->getPointCount () == 0)
			continue;
		int32 sampleOffset = 0;
		ParamValue value = 0.0;
		if (queue->getPoint (queue->getPointCount () - 1, sampleOffset, value) != kResultTrue)
			continue;
		switch (queue->getParameterId ())
		{
			case kAmountId:
				params_.amount = static_cast<float> (value);
				break;
			case kNoiseReductionId:
				params_.noiseReduction = static_cast<float> (value);
				break;
			case kEnhancementId:
				params_.enhancement = static_cast<float> (value);
				break;
			case kVoiceFocusId:
				params_.voiceFocus = static_cast<float> (value);
				break;
			case kInputGainId:
				params_.inputGainDb =
					static_cast<float> (normalizedToGainDb (value));
				break;
			case kBypassId:
				params_.bypass = value > 0.5;
				break;
			default:
				break;
		}
	}
	dsp_.setParameters (params_);
}

template <typename Sample>
bool Processor::processSamples (ProcessData& data)
{
	auto** input = [&] {
		if constexpr (std::is_same_v<Sample, float>)
			return data.inputs[0].channelBuffers32;
		else
			return data.inputs[0].channelBuffers64;
	} ();
	auto** output = [&] {
		if constexpr (std::is_same_v<Sample, float>)
			return data.outputs[0].channelBuffers32;
		else
			return data.outputs[0].channelBuffers64;
	} ();

	const int32 channels = std::min (
		data.inputs[0].numChannels, data.outputs[0].numChannels);
	bool outputIsSilent = true;
	for (int32 sampleIndex = 0; sampleIndex < data.numSamples; ++sampleIndex)
	{
		float mono = 0.0f;
		for (int32 channel = 0; channel < channels; ++channel)
			mono += static_cast<float> (input[channel][sampleIndex]);
		mono /= static_cast<float> (std::max (channels, 1));
		const float result = dsp_.processMonoSample (mono);
		if (std::abs (result) > 1.0e-12f)
			outputIsSilent = false;
		for (int32 channel = 0; channel < channels; ++channel)
			output[channel][sampleIndex] = static_cast<Sample> (result);
	}
	return outputIsSilent;
}

tresult PLUGIN_API Processor::process (ProcessData& data)
{
	readParameterChanges (data.inputParameterChanges);
	if (data.numInputs == 0 || data.numOutputs == 0 || data.numSamples <= 0)
		return kResultOk;
	const bool outputIsSilent = data.symbolicSampleSize == kSample32
		? processSamples<float> (data)
		: processSamples<double> (data);
	const int32 channels = data.outputs[0].numChannels;
	data.outputs[0].silenceFlags = outputIsSilent && channels > 0
		? (static_cast<uint64> (1) << channels) - 1
		: 0;
	return kResultOk;
}

tresult PLUGIN_API Processor::setState (IBStream* state)
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
	params_.amount = amount;
	params_.noiseReduction = noise;
	params_.enhancement = enhancement;
	params_.voiceFocus = focus;
	params_.inputGainDb = gain;
	params_.bypass = bypass != 0;
	dsp_.setParameters (params_);
	return kResultOk;
}

tresult PLUGIN_API Processor::getState (IBStream* state)
{
	if (!state)
		return kResultFalse;
	IBStreamer stream (state, kLittleEndian);
	stream.writeFloat (params_.amount);
	stream.writeFloat (params_.noiseReduction);
	stream.writeFloat (params_.enhancement);
	stream.writeFloat (params_.voiceFocus);
	stream.writeFloat (params_.inputGainDb);
	stream.writeInt32 (params_.bypass ? 1 : 0);
	return kResultOk;
}

} // namespace VoiceMatch
