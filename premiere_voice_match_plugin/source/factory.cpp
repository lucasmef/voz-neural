#include "controller.h"
#include "ids.h"
#include "processor.h"
#include "version.h"

#include "public.sdk/source/main/pluginfactory_constexpr.h"

#define stringPluginName "Voz Clara Feminina"

BEGIN_FACTORY_DEF (stringCompanyName, stringCompanyWeb, stringCompanyEmail, 2)

DEF_CLASS (VoiceMatch::ProcessorUID,
           Steinberg::PClassInfo::kManyInstances,
           kVstAudioEffectClass,
           stringPluginName,
           Steinberg::Vst::kDistributable,
           "Fx|Restoration",
           FULL_VERSION_STR,
           kVstVersionString,
           VoiceMatch::Processor::createInstance,
           nullptr)

DEF_CLASS (VoiceMatch::ControllerUID,
           Steinberg::PClassInfo::kManyInstances,
           kVstComponentControllerClass,
           stringPluginName " Controller",
           0,
           "",
           FULL_VERSION_STR,
           kVstVersionString,
           VoiceMatch::Controller::createInstance,
           nullptr)

END_FACTORY

