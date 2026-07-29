#include "bridge.hpp"

#include "json.hpp"

#include <dlfcn.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

extern char** environ;

#ifndef VOZ_NEURAL_ADDON_VERSION
#define VOZ_NEURAL_ADDON_VERSION "0.1.0-dev"
#endif

#ifndef VOZ_NEURAL_RUNTIME_RELATIVE_PATH
#define VOZ_NEURAL_RUNTIME_RELATIVE_PATH "runtime"
#endif

#ifndef VOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE
#define VOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE 0
#endif

namespace voz_neural {
namespace {

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

constexpr std::string_view kPreset = "capcut75-premiere-stereo-v1";
constexpr std::size_t kMaximumPathBytes = 4096;
constexpr std::size_t kMaximumCapturedOutputBytes = 2 * 1024 * 1024;
constexpr double kDefaultTimeoutSeconds = 30.0 * 60.0;
constexpr double kMaximumTimeoutSeconds = 2.0 * 60.0 * 60.0;

class BridgeError final : public std::runtime_error {
 public:
  BridgeError(std::string code, std::string message)
      : std::runtime_error(std::move(message)), code_(std::move(code)) {}

  const std::string& code() const noexcept { return code_; }

 private:
  std::string code_;
};

struct RuntimePaths {
  fs::path addon;
  fs::path plugin_root;
  fs::path runtime_root;
  fs::path python;
  fs::path runner;
  fs::path deep_filter;
  fs::path rnnoise_vad;
  fs::path extraction_preset;
};

struct JobRequest {
  std::string request_id;
  std::string input_compensation_mode = "none";
  fs::path input;
  fs::path output;
  fs::path diagnostics;
  RuntimePaths runtime;
  double timeout_seconds = kDefaultTimeoutSeconds;
  bool overwrite = false;
  std::optional<double> source_in_seconds;
  std::optional<double> source_out_seconds;
  std::optional<double> timeline_start_seconds;
  std::optional<double> timeline_end_seconds;
  std::optional<std::string> clip_name;
};

struct ActiveJob {
  bool reserved = false;
  pid_t process_id = -1;
  std::string request_id;
  bool cancel_requested = false;
};

struct SpawnResult {
  bool spawned = false;
  int spawn_error = 0;
  bool exited = false;
  int exit_code = -1;
  int termination_signal = 0;
  bool timed_out = false;
  bool cancelled = false;
  bool output_truncated = false;
  std::string captured_output;
  std::chrono::milliseconds duration{0};
};

std::mutex g_active_job_mutex;
ActiveJob g_active_job;
std::atomic<std::uint64_t> g_request_counter{0};

std::string JsonError(std::string_view code, std::string_view message) {
  return "{\"ok\":false,\"error\":{\"code\":" + json::Quote(code) +
         ",\"message\":" + json::Quote(message) + "}}";
}

std::string RuntimeInfoError(std::string_view code,
                             std::string_view message) {
  return "{\"ok\":false,\"runtimeReady\":false,\"preset\":" +
         json::Quote(kPreset) + ",\"error\":{\"code\":" + json::Quote(code) +
         ",\"message\":" + json::Quote(message) + "}}";
}

std::string Lowercase(std::string value) {
  std::transform(value.begin(), value.end(), value.begin(),
                 [](unsigned char character) {
                   return static_cast<char>(std::tolower(character));
                 });
  return value;
}

void RejectEmbeddedNull(std::string_view value, std::string_view field) {
  if (value.find('\0') != std::string_view::npos) {
    throw BridgeError("invalid_request",
                      std::string(field) + " contains an embedded NUL byte");
  }
}

void ValidatePathText(std::string_view value, std::string_view field) {
  if (value.empty()) {
    throw BridgeError("invalid_request",
                      std::string(field) + " must not be empty");
  }
  if (value.size() > kMaximumPathBytes) {
    throw BridgeError("invalid_request",
                      std::string(field) + " exceeds 4096 bytes");
  }
  RejectEmbeddedNull(value, field);
}

bool IsWithin(const fs::path& child, const fs::path& parent) {
  const fs::path relative = child.lexically_relative(parent);
  if (relative.empty()) {
    return child == parent;
  }
  const auto first = relative.begin();
  return first != relative.end() && *first != ".." && !relative.is_absolute();
}

fs::path CanonicalExisting(const fs::path& path, std::string_view label) {
  std::error_code error;
  const fs::path canonical = fs::canonical(path, error);
  if (error) {
    throw BridgeError("path_not_found",
                      std::string(label) + " cannot be resolved: " +
                          error.message());
  }
  return canonical;
}

void RequireRegularFile(const fs::path& path, std::string_view label,
                        bool executable) {
  std::error_code error;
  const fs::file_status status = fs::status(path, error);
  if (error || !fs::is_regular_file(status)) {
    throw BridgeError("invalid_path",
                      std::string(label) + " is not a regular file");
  }
  if (::access(path.c_str(), executable ? X_OK : R_OK) != 0) {
    throw BridgeError(
        executable ? "runtime_not_executable" : "runtime_not_readable",
        std::string(label) + ": " + std::strerror(errno));
  }
}

fs::path AddonPath() {
  Dl_info info{};
  if (::dladdr(reinterpret_cast<const void*>(&GetRuntimeInfo), &info) == 0 ||
      info.dli_fname == nullptr) {
    throw BridgeError("runtime_resolution_failed",
                      "cannot resolve the loaded .uxpaddon path");
  }
  return CanonicalExisting(fs::path(info.dli_fname), "addon path");
}

RuntimePaths ResolveRuntime(
    const std::optional<std::string>& development_override = std::nullopt) {
  RuntimePaths paths;
  paths.addon = AddonPath();
  paths.plugin_root =
      CanonicalExisting(paths.addon.parent_path() / ".." / "..",
                        "plugin root");

  fs::path requested_root =
      paths.plugin_root / VOZ_NEURAL_RUNTIME_RELATIVE_PATH;
  if (development_override.has_value()) {
#if VOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE
    ValidatePathText(*development_override, "runtimeRoot");
    const fs::path override_path(*development_override);
    if (!override_path.is_absolute()) {
      throw BridgeError("invalid_runtime_root",
                        "runtimeRoot must be an absolute path");
    }
    requested_root = override_path;
#else
    throw BridgeError(
        "runtime_override_disabled",
        "runtimeRoot is accepted only by a development build compiled with "
        "VOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE");
#endif
  }

  paths.runtime_root = CanonicalExisting(requested_root, "runtime root");
  paths.python = CanonicalExisting(paths.runtime_root / "python/bin/python3",
                                   "embedded Python");
  paths.runner = CanonicalExisting(
      paths.runtime_root / "app/independent_voice_runner.py", "voice runner");
  paths.deep_filter = CanonicalExisting(
      paths.runtime_root /
          "bin/deep-filter-0.5.6-aarch64-apple-darwin",
      "DeepFilterNet binary");
  paths.rnnoise_vad = CanonicalExisting(
      paths.runtime_root / "bin/rnnoise_vad_arm64", "RNNoise VAD binary");
  paths.extraction_preset = CanonicalExisting(
      paths.runtime_root / "presets/Wave48stereo24.epr",
      "Premiere extraction preset");

  for (const auto& member :
       {paths.python, paths.runner, paths.deep_filter, paths.rnnoise_vad,
        paths.extraction_preset}) {
    if (!IsWithin(member, paths.runtime_root)) {
      throw BridgeError(
          "runtime_escape",
          "a canonical runtime component resolves outside runtimeRoot");
    }
  }
  RequireRegularFile(paths.python, "embedded Python", true);
  RequireRegularFile(paths.runner, "voice runner", false);
  RequireRegularFile(paths.deep_filter, "DeepFilterNet binary", true);
  RequireRegularFile(paths.rnnoise_vad, "RNNoise VAD binary", true);
  RequireRegularFile(paths.extraction_preset, "Premiere extraction preset",
                     false);
  return paths;
}

const json::Value* Find(const json::Object& object, std::string_view key) {
  const auto match = object.find(key);
  return match == object.end() ? nullptr : &match->second;
}

std::string RequireString(const json::Object& object, std::string_view key) {
  const json::Value* value = Find(object, key);
  if (value == nullptr || value->kind != json::Value::Kind::kString) {
    throw BridgeError("invalid_request",
                      std::string(key) + " must be a string");
  }
  RejectEmbeddedNull(value->string_value, key);
  return value->string_value;
}

std::optional<std::string> OptionalString(const json::Object& object,
                                          std::string_view key) {
  const json::Value* value = Find(object, key);
  if (value == nullptr || value->kind == json::Value::Kind::kNull) {
    return std::nullopt;
  }
  if (value->kind != json::Value::Kind::kString) {
    throw BridgeError("invalid_request",
                      std::string(key) + " must be a string or null");
  }
  RejectEmbeddedNull(value->string_value, key);
  return value->string_value;
}

std::optional<double> OptionalNumber(const json::Object& object,
                                     std::string_view key) {
  const json::Value* value = Find(object, key);
  if (value == nullptr || value->kind == json::Value::Kind::kNull) {
    return std::nullopt;
  }
  if (value->kind != json::Value::Kind::kNumber ||
      !std::isfinite(value->number_value)) {
    throw BridgeError("invalid_request",
                      std::string(key) + " must be a finite number or null");
  }
  return value->number_value;
}

bool OptionalBoolean(const json::Object& object, std::string_view key,
                     bool fallback) {
  const json::Value* value = Find(object, key);
  if (value == nullptr || value->kind == json::Value::Kind::kNull) {
    return fallback;
  }
  if (value->kind != json::Value::Kind::kBoolean) {
    throw BridgeError("invalid_request",
                      std::string(key) + " must be a boolean or null");
  }
  return value->boolean_value;
}

void ValidateKeys(const json::Object& object) {
  static const std::set<std::string, std::less<>> kAllowedKeys = {
      "clipName",
      "diagnosticsPath",
      "inputCompensationMode",
      "inputPath",
      "outputPath",
      "overwrite",
      "preset",
      "requestId",
      "runtimeRoot",
      "sourceInSeconds",
      "sourceOutSeconds",
      "timelineEndSeconds",
      "timelineStartSeconds",
      "timeoutSeconds",
  };
  for (const auto& [key, unused] : object) {
    static_cast<void>(unused);
    if (!kAllowedKeys.contains(key)) {
      throw BridgeError("invalid_request",
                        "unknown request property: " + key);
    }
  }
}

void ValidateMetadata(const std::optional<std::string>& clip_name,
                      const std::optional<double>& timeline_start,
                      const std::optional<double>& timeline_end) {
  if (clip_name.has_value() && clip_name->size() > 512) {
    throw BridgeError("invalid_request", "clipName exceeds 512 bytes");
  }
  if (timeline_start.has_value() != timeline_end.has_value()) {
    throw BridgeError(
        "invalid_request",
        "timelineStartSeconds and timelineEndSeconds must be supplied together");
  }
  if (timeline_start.has_value() &&
      (*timeline_start < 0.0 || *timeline_end <= *timeline_start)) {
    throw BridgeError("invalid_request",
                      "timeline range must be non-negative with end > start");
  }
}

fs::path CanonicalInput(std::string_view text) {
  ValidatePathText(text, "inputPath");
  const fs::path requested{std::string(text)};
  if (!requested.is_absolute()) {
    throw BridgeError("invalid_input", "inputPath must be absolute");
  }
  const fs::path canonical = CanonicalExisting(requested, "inputPath");
  RequireRegularFile(canonical, "inputPath", false);
  if (Lowercase(canonical.extension().string()) != ".wav") {
    throw BridgeError("unsupported_input",
                      "inputPath must be a PCM WAV file");
  }
  return canonical;
}

fs::path CanonicalOutput(std::string_view text, std::string_view field,
                         std::string_view required_extension, bool overwrite) {
  ValidatePathText(text, field);
  fs::path requested{std::string(text)};
  if (!requested.is_absolute()) {
    throw BridgeError("invalid_output",
                      std::string(field) + " must be absolute");
  }
  requested = requested.lexically_normal();
  const fs::path filename = requested.filename();
  if (filename.empty() || filename == "." || filename == "..") {
    throw BridgeError("invalid_output",
                      std::string(field) + " has no valid filename");
  }
  if (Lowercase(filename.extension().string()) != required_extension) {
    throw BridgeError(
        "invalid_output",
        std::string(field) + " must end in " + std::string(required_extension));
  }

  std::error_code error;
  fs::create_directories(requested.parent_path(), error);
  if (error) {
    throw BridgeError("output_directory_failed",
                      "cannot create parent directory for " +
                          std::string(field) + ": " + error.message());
  }
  const fs::path parent =
      CanonicalExisting(requested.parent_path(), "output parent directory");
  fs::path canonical = parent / filename;

  const fs::file_status link_status = fs::symlink_status(canonical, error);
  if (error && error != std::errc::no_such_file_or_directory) {
    throw BridgeError("invalid_output",
                      "cannot inspect " + std::string(field) + ": " +
                          error.message());
  }
  if (!error && fs::exists(link_status)) {
    if (fs::is_symlink(link_status)) {
      throw BridgeError("invalid_output",
                        std::string(field) + " must not be a symbolic link");
    }
    if (!fs::is_regular_file(link_status)) {
      throw BridgeError("invalid_output",
                        std::string(field) +
                            " exists but is not a regular file");
    }
    if (!overwrite) {
      throw BridgeError("output_exists",
                        std::string(field) +
                            " already exists; choose a unique path");
    }
    canonical = CanonicalExisting(canonical, field);
  }
  return canonical;
}

std::string NewRequestId() {
  const auto now = std::chrono::duration_cast<std::chrono::milliseconds>(
                       Clock::now().time_since_epoch())
                       .count();
  const auto serial = g_request_counter.fetch_add(1);
  return "voz-neural-" + std::to_string(now) + "-" + std::to_string(serial);
}

void ValidateRequestId(std::string_view request_id) {
  if (request_id.empty() || request_id.size() > 128) {
    throw BridgeError("invalid_request",
                      "requestId must contain 1 to 128 characters");
  }
  for (const unsigned char character : request_id) {
    if (!(std::isalnum(character) || character == '-' || character == '_' ||
          character == '.' || character == ':')) {
      throw BridgeError(
          "invalid_request",
          "requestId may contain only letters, digits, '-', '_', '.', ':'");
    }
  }
}

JobRequest ParseJob(std::string_view source) {
  const json::Object object = json::ParseObject(source);
  ValidateKeys(object);

  JobRequest request;
  request.request_id =
      OptionalString(object, "requestId").value_or(NewRequestId());
  ValidateRequestId(request.request_id);

  const std::string preset =
      OptionalString(object, "preset").value_or(std::string(kPreset));
  if (preset != kPreset) {
    throw BridgeError("unsupported_preset",
                      "only capcut75-premiere-stereo-v1 is supported");
  }

  request.input_compensation_mode =
      OptionalString(object, "inputCompensationMode").value_or("none");
  if (request.input_compensation_mode != "none" &&
      request.input_compensation_mode != "premiere_mono_center_v1") {
    throw BridgeError(
        "invalid_request",
        "inputCompensationMode must be 'none' or "
        "'premiere_mono_center_v1'");
  }

  request.overwrite = OptionalBoolean(object, "overwrite", false);
  request.input = CanonicalInput(RequireString(object, "inputPath"));
  request.output =
      CanonicalOutput(RequireString(object, "outputPath"), "outputPath",
                      ".wav", request.overwrite);

  if (request.input == request.output) {
    throw BridgeError("invalid_output",
                      "inputPath and outputPath must be different files");
  }

  const std::optional<std::string> diagnostics =
      OptionalString(object, "diagnosticsPath");
  const fs::path default_diagnostics =
      request.output.parent_path() /
      (request.output.stem().string() + ".json");
  request.diagnostics = CanonicalOutput(
      diagnostics.value_or(default_diagnostics.string()), "diagnosticsPath",
      ".json", request.overwrite);
  if (request.diagnostics == request.input ||
      request.diagnostics == request.output) {
    throw BridgeError("invalid_output",
                      "diagnosticsPath must be distinct from input and output");
  }

  request.source_in_seconds = OptionalNumber(object, "sourceInSeconds");
  request.source_out_seconds = OptionalNumber(object, "sourceOutSeconds");
  if (request.source_in_seconds.has_value() !=
      request.source_out_seconds.has_value()) {
    throw BridgeError(
        "invalid_request",
        "sourceInSeconds and sourceOutSeconds must be supplied together");
  }
  if (request.source_in_seconds.has_value() &&
      (*request.source_in_seconds < 0.0 ||
       *request.source_out_seconds <= *request.source_in_seconds)) {
    throw BridgeError("invalid_request",
                      "source range must be non-negative with out > in");
  }
  request.timeline_start_seconds =
      OptionalNumber(object, "timelineStartSeconds");
  request.timeline_end_seconds = OptionalNumber(object, "timelineEndSeconds");
  request.clip_name = OptionalString(object, "clipName");
  ValidateMetadata(request.clip_name, request.timeline_start_seconds,
                   request.timeline_end_seconds);

  request.timeout_seconds =
      OptionalNumber(object, "timeoutSeconds").value_or(kDefaultTimeoutSeconds);
  if (request.timeout_seconds < 1.0 ||
      request.timeout_seconds > kMaximumTimeoutSeconds) {
    throw BridgeError("invalid_request",
                      "timeoutSeconds must be between 1 and 7200");
  }

  request.runtime = ResolveRuntime(OptionalString(object, "runtimeRoot"));
  return request;
}

std::vector<std::string> BuildArguments(const JobRequest& request) {
  return {
      request.runtime.python.string(),
      request.runtime.runner.string(),
      request.input.string(),
      request.output.string(),
      "--deep-filter-bin",
      request.runtime.deep_filter.string(),
      "--rnnoise-vad-bin",
      request.runtime.rnnoise_vad.string(),
      "--diagnostics",
      request.diagnostics.string(),
      "--input-compensation-mode",
      request.input_compensation_mode,
  };
}

std::vector<std::string> SanitizedEnvironment() {
  static const std::set<std::string, std::less<>> kRejected = {
      "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"};
  std::vector<std::string> result;
  for (char** current = environ; current != nullptr && *current != nullptr;
       ++current) {
    const std::string entry(*current);
    const std::size_t separator = entry.find('=');
    const std::string name = entry.substr(0, separator);
    if (!kRejected.contains(name) && name != "PYTHONDONTWRITEBYTECODE" &&
        name != "PYTHONNOUSERSITE") {
      result.push_back(entry);
    }
  }
  result.emplace_back("PYTHONDONTWRITEBYTECODE=1");
  result.emplace_back("PYTHONNOUSERSITE=1");
  return result;
}

std::vector<char*> MutablePointers(std::vector<std::string>& strings) {
  std::vector<char*> pointers;
  pointers.reserve(strings.size() + 1);
  for (std::string& value : strings) {
    pointers.push_back(value.data());
  }
  pointers.push_back(nullptr);
  return pointers;
}

void SignalProcessGroup(pid_t process_id, int signal_number) {
  if (process_id <= 0) {
    return;
  }
  if (::kill(-process_id, signal_number) != 0) {
    // Fall back to the direct child if the process group was not established.
    ::kill(process_id, signal_number);
  }
}

bool ReserveJob(std::string_view request_id) {
  std::lock_guard lock(g_active_job_mutex);
  if (g_active_job.reserved) {
    return false;
  }
  g_active_job = ActiveJob{
      .reserved = true,
      .process_id = -1,
      .request_id = std::string(request_id),
      .cancel_requested = false,
  };
  return true;
}

bool PublishProcessId(std::string_view request_id, pid_t process_id) {
  std::lock_guard lock(g_active_job_mutex);
  if (!g_active_job.reserved || g_active_job.request_id != request_id) {
    return true;
  }
  g_active_job.process_id = process_id;
  return g_active_job.cancel_requested;
}

bool WasCancelled(std::string_view request_id) {
  std::lock_guard lock(g_active_job_mutex);
  return g_active_job.reserved && g_active_job.request_id == request_id &&
         g_active_job.cancel_requested;
}

void ReleaseJob(std::string_view request_id) {
  std::lock_guard lock(g_active_job_mutex);
  if (g_active_job.reserved && g_active_job.request_id == request_id) {
    g_active_job = ActiveJob{};
  }
}

class JobReservation final {
 public:
  explicit JobReservation(std::string request_id)
      : request_id_(std::move(request_id)) {}
  ~JobReservation() { ReleaseJob(request_id_); }

 private:
  std::string request_id_;
};

bool SetCloseOnExec(int file_descriptor) {
  const int current = ::fcntl(file_descriptor, F_GETFD);
  if (current < 0) {
    return false;
  }
  return ::fcntl(file_descriptor, F_SETFD, current | FD_CLOEXEC) == 0;
}

bool SetNonBlocking(int file_descriptor) {
  const int current = ::fcntl(file_descriptor, F_GETFL);
  if (current < 0) {
    return false;
  }
  return ::fcntl(file_descriptor, F_SETFL, current | O_NONBLOCK) == 0;
}

void CaptureAvailable(int descriptor, SpawnResult& result, bool& reached_eof) {
  char buffer[8192];
  while (true) {
    const ssize_t count = ::read(descriptor, buffer, sizeof(buffer));
    if (count > 0) {
      const std::size_t remaining =
          result.captured_output.size() < kMaximumCapturedOutputBytes
              ? kMaximumCapturedOutputBytes - result.captured_output.size()
              : 0;
      const std::size_t retained =
          std::min<std::size_t>(remaining, static_cast<std::size_t>(count));
      result.captured_output.append(buffer, retained);
      if (retained != static_cast<std::size_t>(count)) {
        result.output_truncated = true;
      }
      continue;
    }
    if (count == 0) {
      reached_eof = true;
    } else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
      reached_eof = true;
    }
    return;
  }
}

SpawnResult SpawnAndWait(const JobRequest& request) {
  SpawnResult result;
  const auto started = Clock::now();
  int pipe_descriptors[2] = {-1, -1};
  if (::pipe(pipe_descriptors) != 0) {
    result.spawn_error = errno;
    return result;
  }
  if (!SetCloseOnExec(pipe_descriptors[0]) ||
      !SetCloseOnExec(pipe_descriptors[1])) {
    result.spawn_error = errno;
    ::close(pipe_descriptors[0]);
    ::close(pipe_descriptors[1]);
    return result;
  }

  posix_spawn_file_actions_t file_actions;
  posix_spawnattr_t attributes;
  bool file_actions_ready = false;
  bool attributes_ready = false;
  if (::posix_spawn_file_actions_init(&file_actions) == 0) {
    file_actions_ready = true;
  }
  if (::posix_spawnattr_init(&attributes) == 0) {
    attributes_ready = true;
  }
  if (!file_actions_ready || !attributes_ready) {
    result.spawn_error = errno != 0 ? errno : ENOMEM;
    if (file_actions_ready) {
      ::posix_spawn_file_actions_destroy(&file_actions);
    }
    if (attributes_ready) {
      ::posix_spawnattr_destroy(&attributes);
    }
    ::close(pipe_descriptors[0]);
    ::close(pipe_descriptors[1]);
    return result;
  }

  int setup_status =
      ::posix_spawn_file_actions_addclose(&file_actions, pipe_descriptors[0]);
  if (setup_status == 0) {
    setup_status = ::posix_spawn_file_actions_adddup2(
        &file_actions, pipe_descriptors[1], STDOUT_FILENO);
  }
  if (setup_status == 0) {
    setup_status = ::posix_spawn_file_actions_adddup2(
        &file_actions, pipe_descriptors[1], STDERR_FILENO);
  }
  if (setup_status == 0) {
    setup_status = ::posix_spawn_file_actions_addclose(&file_actions,
                                                       pipe_descriptors[1]);
  }
  if (setup_status == 0) {
    setup_status =
        ::posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
  }
  if (setup_status == 0) {
    setup_status = ::posix_spawnattr_setpgroup(&attributes, 0);
  }
  if (setup_status != 0) {
    result.spawn_error = setup_status;
    ::posix_spawn_file_actions_destroy(&file_actions);
    ::posix_spawnattr_destroy(&attributes);
    ::close(pipe_descriptors[0]);
    ::close(pipe_descriptors[1]);
    return result;
  }

  std::vector<std::string> arguments = BuildArguments(request);
  std::vector<std::string> environment = SanitizedEnvironment();
  std::vector<char*> argument_pointers = MutablePointers(arguments);
  std::vector<char*> environment_pointers = MutablePointers(environment);
  pid_t process_id = -1;
  const int spawn_status =
      ::posix_spawn(&process_id, request.runtime.python.c_str(), &file_actions,
                    &attributes, argument_pointers.data(),
                    environment_pointers.data());
  ::posix_spawn_file_actions_destroy(&file_actions);
  ::posix_spawnattr_destroy(&attributes);
  ::close(pipe_descriptors[1]);

  if (spawn_status != 0) {
    result.spawn_error = spawn_status;
    ::close(pipe_descriptors[0]);
    return result;
  }

  result.spawned = true;
  if (!SetNonBlocking(pipe_descriptors[0])) {
    result.spawn_error = errno;
    SignalProcessGroup(process_id, SIGTERM);
    int ignored_status = 0;
    while (::waitpid(process_id, &ignored_status, 0) < 0 && errno == EINTR) {
    }
    ::close(pipe_descriptors[0]);
    result.spawned = false;
    return result;
  }
  if (PublishProcessId(request.request_id, process_id)) {
    SignalProcessGroup(process_id, SIGTERM);
  }

  const auto deadline =
      started + std::chrono::milliseconds(static_cast<std::int64_t>(
                    std::llround(request.timeout_seconds * 1000.0)));
  std::optional<Clock::time_point> termination_sent;
  bool pipe_eof = false;
  bool child_reaped = false;
  bool wait_succeeded = false;
  int wait_status = 0;
  std::optional<Clock::time_point> reaped_at;

  while (!child_reaped || !pipe_eof) {
    pollfd descriptor{
        .fd = pipe_descriptors[0],
        .events = static_cast<short>(POLLIN | POLLHUP),
        .revents = 0,
    };
    const int poll_status = ::poll(&descriptor, 1, 100);
    if (poll_status > 0 &&
        (descriptor.revents & (POLLIN | POLLHUP | POLLERR)) != 0) {
      CaptureAvailable(pipe_descriptors[0], result, pipe_eof);
    } else if (poll_status < 0 && errno != EINTR) {
      pipe_eof = true;
    }

    if (!child_reaped) {
      const pid_t waited = ::waitpid(process_id, &wait_status, WNOHANG);
      if (waited == process_id) {
        child_reaped = true;
        wait_succeeded = true;
        reaped_at = Clock::now();
      } else if (waited < 0 && errno != EINTR) {
        child_reaped = true;
        reaped_at = Clock::now();
      }
    }

    const auto now = Clock::now();
    if (!termination_sent.has_value() &&
        (now >= deadline || WasCancelled(request.request_id))) {
      result.timed_out = now >= deadline;
      result.cancelled = WasCancelled(request.request_id);
      SignalProcessGroup(process_id, SIGTERM);
      termination_sent = now;
    } else if (termination_sent.has_value() && !child_reaped &&
               now - *termination_sent >= std::chrono::seconds(2)) {
      SignalProcessGroup(process_id, SIGKILL);
    }

    // A descendant that incorrectly kept stdout open must not hold Premiere
    // forever after the direct child has already exited.
    if (child_reaped && !pipe_eof && reaped_at.has_value() &&
        now - *reaped_at > std::chrono::seconds(5)) {
      pipe_eof = true;
    }
  }

  CaptureAvailable(pipe_descriptors[0], result, pipe_eof);
  ::close(pipe_descriptors[0]);
  result.duration = std::chrono::duration_cast<std::chrono::milliseconds>(
      Clock::now() - started);
  result.exited = wait_succeeded;
  if (wait_succeeded && WIFEXITED(wait_status)) {
    result.exit_code = WEXITSTATUS(wait_status);
  } else if (wait_succeeded && WIFSIGNALED(wait_status)) {
    result.termination_signal = WTERMSIG(wait_status);
  }
  return result;
}

std::string OptionalNumberJson(const std::optional<double>& value) {
  if (!value.has_value()) {
    return "null";
  }
  std::ostringstream output;
  output << std::setprecision(17) << *value;
  return output.str();
}

std::string MetadataJson(const JobRequest& request) {
  return "{\"inputCompensationMode\":" +
         json::Quote(request.input_compensation_mode) + ",\"clipName\":" +
         (request.clip_name.has_value() ? json::Quote(*request.clip_name)
                                        : "null") +
         ",\"sourceInSeconds\":" +
         OptionalNumberJson(request.source_in_seconds) +
         ",\"sourceOutSeconds\":" +
         OptionalNumberJson(request.source_out_seconds) +
         ",\"timelineStartSeconds\":" +
         OptionalNumberJson(request.timeline_start_seconds) +
         ",\"timelineEndSeconds\":" +
         OptionalNumberJson(request.timeline_end_seconds) + "}";
}

std::string ProcessSuccess(const JobRequest& request,
                           const SpawnResult& result) {
  return "{\"ok\":true,\"requestId\":" + json::Quote(request.request_id) +
         ",\"preset\":" + json::Quote(kPreset) + ",\"inputPath\":" +
         json::Quote(request.input.string()) + ",\"outputPath\":" +
         json::Quote(request.output.string()) + ",\"diagnosticsPath\":" +
         json::Quote(request.diagnostics.string()) + ",\"exitCode\":" +
         std::to_string(result.exit_code) + ",\"durationMs\":" +
         std::to_string(result.duration.count()) + ",\"runnerOutput\":" +
         json::Quote(result.captured_output) + ",\"outputTruncated\":" +
         (result.output_truncated ? "true" : "false") + ",\"metadata\":" +
         MetadataJson(request) + "}";
}

std::string ProcessFailure(const JobRequest& request, const SpawnResult& result,
                           std::string_view code, std::string_view message) {
  return "{\"ok\":false,\"requestId\":" + json::Quote(request.request_id) +
         ",\"cancelled\":" + (result.cancelled ? "true" : "false") +
         ",\"timedOut\":" + (result.timed_out ? "true" : "false") +
         ",\"exitCode\":" + std::to_string(result.exit_code) +
         ",\"signal\":" + std::to_string(result.termination_signal) +
         ",\"durationMs\":" + std::to_string(result.duration.count()) +
         ",\"runnerOutput\":" + json::Quote(result.captured_output) +
         ",\"outputTruncated\":" +
         (result.output_truncated ? "true" : "false") +
         ",\"metadata\":" + MetadataJson(request) +
         ",\"error\":{\"code\":" + json::Quote(code) + ",\"message\":" +
         json::Quote(message) + "}}";
}

std::optional<std::string> ParseCancellationId(std::string_view source) {
  if (source.empty()) {
    return std::nullopt;
  }
  const std::size_t first = source.find_first_not_of(" \t\r\n");
  if (first == std::string_view::npos) {
    return std::nullopt;
  }
  if (source[first] != '{') {
    const std::size_t last = source.find_last_not_of(" \t\r\n");
    std::string request_id(source.substr(first, last - first + 1));
    ValidateRequestId(request_id);
    return request_id;
  }
  const json::Object object = json::ParseObject(source);
  for (const auto& [key, unused] : object) {
    static_cast<void>(unused);
    if (key != "requestId") {
      throw BridgeError("invalid_request",
                        "cancel accepts only requestId");
    }
  }
  const auto request_id = OptionalString(object, "requestId");
  if (request_id.has_value()) {
    ValidateRequestId(*request_id);
  }
  return request_id;
}

}  // namespace

std::string GetRuntimeInfo() {
  try {
    const RuntimePaths paths = ResolveRuntime();
#if defined(__aarch64__) || defined(__arm64__)
    constexpr std::string_view kArchitecture = "arm64";
#else
    constexpr std::string_view kArchitecture = "unsupported";
#endif
    return "{\"ok\":true,\"addonVersion\":" +
           json::Quote(VOZ_NEURAL_ADDON_VERSION) +
           ",\"apiVersion\":1,\"platform\":\"macos\",\"architecture\":" +
           json::Quote(kArchitecture) + ",\"preset\":" +
           json::Quote(kPreset) + ",\"runtimeReady\":true,\"addonPath\":" +
           json::Quote(paths.addon.string()) + ",\"pluginRoot\":" +
           json::Quote(paths.plugin_root.string()) + ",\"runtimeRoot\":" +
           json::Quote(paths.runtime_root.string()) + ",\"pythonPath\":" +
           json::Quote(paths.python.string()) + ",\"runnerPath\":" +
           json::Quote(paths.runner.string()) +
           ",\"extractionPresetPath\":" +
           json::Quote(paths.extraction_preset.string()) + "}";
  } catch (const BridgeError& error) {
    return RuntimeInfoError(error.code(), error.what());
  } catch (const std::exception& error) {
    return RuntimeInfoError("runtime_info_failed", error.what());
  } catch (...) {
    return RuntimeInfoError("runtime_info_failed",
                            "unknown native exception");
  }
}

std::string ProcessFile(std::string_view request_json) {
  try {
    JobRequest request = ParseJob(request_json);
    if (!ReserveJob(request.request_id)) {
      return JsonError("busy",
                       "another voice processing job is already active");
    }
    JobReservation reservation(request.request_id);
    const SpawnResult result = SpawnAndWait(request);
    if (!result.spawned) {
      return ProcessFailure(
          request, result, "spawn_failed",
          std::string("cannot start embedded runtime: ") +
              std::strerror(result.spawn_error));
    }
    if (result.cancelled) {
      return ProcessFailure(request, result, "cancelled",
                            "processing was cancelled");
    }
    if (result.timed_out) {
      return ProcessFailure(request, result, "timeout",
                            "processing exceeded timeoutSeconds");
    }
    if (!result.exited || result.exit_code != 0) {
      return ProcessFailure(
          request, result, "runner_failed",
          result.termination_signal != 0
              ? "voice runner terminated by signal " +
                    std::to_string(result.termination_signal)
              : "voice runner exited with status " +
                    std::to_string(result.exit_code));
    }
    std::error_code error;
    if (!fs::is_regular_file(request.output, error) || error) {
      return ProcessFailure(request, result, "missing_output",
                            "runner exited successfully but produced no WAV");
    }
    error.clear();
    if (!fs::is_regular_file(request.diagnostics, error) || error) {
      return ProcessFailure(
          request, result, "missing_diagnostics",
          "runner exited successfully but produced no diagnostics JSON");
    }
    return ProcessSuccess(request, result);
  } catch (const BridgeError& error) {
    return JsonError(error.code(), error.what());
  } catch (const json::ParseError& error) {
    return JsonError("invalid_json", error.what());
  } catch (const std::exception& error) {
    return JsonError("native_failure", error.what());
  } catch (...) {
    return JsonError("native_failure", "unknown native exception");
  }
}

std::string Cancel(std::string_view request_json) {
  try {
    const std::optional<std::string> requested_id =
        ParseCancellationId(request_json);
    std::lock_guard lock(g_active_job_mutex);
    if (!g_active_job.reserved) {
      return "{\"ok\":true,\"cancelled\":false,\"reason\":"
             "\"no_active_job\"}";
    }
    if (requested_id.has_value() &&
        *requested_id != g_active_job.request_id) {
      return JsonError("request_mismatch",
                       "requestId does not match the active job");
    }
    g_active_job.cancel_requested = true;
    const pid_t process_id = g_active_job.process_id;
    const std::string active_id = g_active_job.request_id;
    if (process_id > 0) {
      SignalProcessGroup(process_id, SIGTERM);
    }
    return "{\"ok\":true,\"cancelled\":true,\"requestId\":" +
           json::Quote(active_id) + "}";
  } catch (const BridgeError& error) {
    return JsonError(error.code(), error.what());
  } catch (const json::ParseError& error) {
    return JsonError("invalid_json", error.what());
  } catch (const std::exception& error) {
    return JsonError("cancel_failed", error.what());
  } catch (...) {
    return JsonError("cancel_failed", "unknown native exception");
  }
}

void Terminate() {
  std::lock_guard lock(g_active_job_mutex);
  if (!g_active_job.reserved) {
    return;
  }
  g_active_job.cancel_requested = true;
  if (g_active_job.process_id > 0) {
    SignalProcessGroup(g_active_job.process_id, SIGTERM);
  }
}

}  // namespace voz_neural
