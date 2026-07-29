#pragma once

#include "public.sdk/source/vst/vstaudioeffect.h"
#include "voice_dsp.h"

namespace VoiceMatch {

class Processor : public Steinberg::Vst::AudioEffect
{
public:
	Processor ();

	static Steinberg::FUnknown* createInstance (void*)
	{
		return static_cast<Steinberg::Vst::IAudioProcessor*> (new Processor ());
	}

	Steinberg::tresult PLUGIN_API initialize (Steinberg::FUnknown* context) override;
	Steinberg::tresult PLUGIN_API setBusArrangements (
		Steinberg::Vst::SpeakerArrangement* inputs,
		Steinberg::int32 numIns,
		Steinberg::Vst::SpeakerArrangement* outputs,
		Steinberg::int32 numOuts) override;
	Steinberg::tresult PLUGIN_API canProcessSampleSize (
		Steinberg::int32 symbolicSampleSize) override;
	Steinberg::tresult PLUGIN_API setupProcessing (
		Steinberg::Vst::ProcessSetup& setup) override;
	Steinberg::tresult PLUGIN_API setActive (Steinberg::TBool state) override;
	Steinberg::tresult PLUGIN_API setProcessing (Steinberg::TBool state) override;
	Steinberg::tresult PLUGIN_API process (Steinberg::Vst::ProcessData& data) override;
	Steinberg::uint32 PLUGIN_API getLatencySamples () override
	{
		return VoiceDsp::kTotalLatency;
	}
	Steinberg::uint32 PLUGIN_API getTailSamples () override
	{
		return VoiceDsp::kTotalLatency;
	}
	Steinberg::tresult PLUGIN_API setState (Steinberg::IBStream* state) override;
	Steinberg::tresult PLUGIN_API getState (Steinberg::IBStream* state) override;

private:
	void readParameterChanges (Steinberg::Vst::IParameterChanges* changes);

	template <typename Sample>
	bool processSamples (Steinberg::Vst::ProcessData& data);

	DspParameters params_ {};
	VoiceDsp dsp_;
	Steinberg::int32 channelCount_ {2};
};

} // namespace VoiceMatch
