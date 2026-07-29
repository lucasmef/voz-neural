#pragma once

#include <string>
#include <string_view>

namespace voz_neural {

std::string GetRuntimeInfo();
std::string ProbeAudioTopology(std::string_view input_path);
std::string ProcessFile(std::string_view request_json);
std::string Cancel(std::string_view request_json);
void Terminate();

}  // namespace voz_neural
