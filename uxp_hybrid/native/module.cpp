#include "bridge.hpp"

#include "UxpAddon.h"
#include "UxpAddonShared.h"

#include <exception>
#include <cstring>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

addon_value CreateString(addon_env env, std::string_view text) {
  addon_value result = nullptr;
  const addon_status status = UxpAddonApis.uxp_addon_create_string_utf8(
      env, text.data(), text.size(), &result);
  if (status != addon_ok) {
    UxpAddonApis.uxp_addon_throw_error(
        env, nullptr, "Unable to create the native addon response");
    return nullptr;
  }
  return result;
}

std::string FirstStringArgument(addon_env env, addon_callback_info info,
                                bool required) {
  addon_value argument = nullptr;
  std::size_t argument_count = 1;
  const addon_status callback_status = UxpAddonApis.uxp_addon_get_cb_info(
      env, info, &argument_count, &argument, nullptr, nullptr);
  if (callback_status != addon_ok || argument_count == 0) {
    if (!required) {
      return {};
    }
    throw std::runtime_error("Expected one JSON string argument");
  }

  std::size_t byte_count = 0;
  addon_status string_status = UxpAddonApis.uxp_addon_get_value_string_utf8(
      env, argument, nullptr, 0, &byte_count);
  if (string_status != addon_ok) {
    throw std::runtime_error("Argument must be a UTF-8 string");
  }
  std::vector<char> buffer(byte_count + 1, '\0');
  std::size_t copied = 0;
  string_status = UxpAddonApis.uxp_addon_get_value_string_utf8(
      env, argument, buffer.data(), buffer.size(), &copied);
  if (string_status != addon_ok) {
    throw std::runtime_error("Unable to read the UTF-8 argument");
  }
  return std::string(buffer.data(), copied);
}

addon_value GetRuntimeInfoCallback(addon_env env, addon_callback_info) {
  try {
    return CreateString(env, voz_neural::GetRuntimeInfo());
  } catch (const std::exception&) {
    return CreateString(env,
                        "{\"ok\":false,\"error\":{\"code\":"
                        "\"native_boundary_failure\",\"message\":"
                        "\"native exception at the UXP boundary\"}}");
  } catch (...) {
    return CreateString(
        env,
        "{\"ok\":false,\"error\":{\"code\":\"native_boundary_failure\","
        "\"message\":\"unknown native exception\"}}");
  }
}

addon_value ProcessFileCallback(addon_env env, addon_callback_info info) {
  try {
    return CreateString(env,
                        voz_neural::ProcessFile(
                            FirstStringArgument(env, info, true)));
  } catch (const std::exception& error) {
    UxpAddonApis.uxp_addon_throw_error(env, nullptr, error.what());
    return nullptr;
  } catch (...) {
    UxpAddonApis.uxp_addon_throw_error(env, nullptr,
                                      "Unknown processFile native exception");
    return nullptr;
  }
}

addon_value ProbeAudioTopologyCallback(addon_env env,
                                       addon_callback_info info) {
  try {
    return CreateString(
        env, voz_neural::ProbeAudioTopology(
                 FirstStringArgument(env, info, true)));
  } catch (const std::exception& error) {
    UxpAddonApis.uxp_addon_throw_error(env, nullptr, error.what());
    return nullptr;
  } catch (...) {
    UxpAddonApis.uxp_addon_throw_error(
        env, nullptr, "Unknown probeAudioTopology native exception");
    return nullptr;
  }
}

addon_value CancelCallback(addon_env env, addon_callback_info info) {
  try {
    return CreateString(
        env, voz_neural::Cancel(FirstStringArgument(env, info, false)));
  } catch (const std::exception& error) {
    UxpAddonApis.uxp_addon_throw_error(env, nullptr, error.what());
    return nullptr;
  } catch (...) {
    UxpAddonApis.uxp_addon_throw_error(env, nullptr,
                                      "Unknown cancel native exception");
    return nullptr;
  }
}

addon_status ExportFunction(addon_env env, addon_value exports,
                            const addon_apis& apis, const char* name,
                            addon_callback callback) {
  addon_value function = nullptr;
  addon_status status =
      apis.uxp_addon_create_function(env, name, std::strlen(name), callback,
                                     nullptr, &function);
  if (status != addon_ok) {
    return status;
  }
  return apis.uxp_addon_set_named_property(env, exports, name, function);
}

addon_value Init(addon_env env, addon_value exports, const addon_apis& apis) {
  if (ExportFunction(env, exports, apis, "getRuntimeInfo",
                     GetRuntimeInfoCallback) != addon_ok ||
      ExportFunction(env, exports, apis, "probeAudioTopology",
                     ProbeAudioTopologyCallback) != addon_ok ||
      ExportFunction(env, exports, apis, "processFile",
                     ProcessFileCallback) != addon_ok ||
      ExportFunction(env, exports, apis, "cancel", CancelCallback) !=
          addon_ok) {
    apis.uxp_addon_throw_error(env, nullptr,
                               "Unable to export Voz Neural native API");
  }
  return exports;
}

void Terminate(addon_env) {
  try {
    voz_neural::Terminate();
  } catch (...) {
    // Never let an exception cross the UXP ABI boundary.
  }
}

}  // namespace

UXP_ADDON_INIT(Init)
UXP_ADDON_TERMINATE(Terminate)
