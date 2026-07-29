#include "bridge.hpp"

#include "json.hpp"

#import <AVFoundation/AVFoundation.h>
#import <AudioToolbox/AudioToolbox.h>
#import <CoreMedia/CoreMedia.h>
#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <unistd.h>

namespace voz_neural {
namespace {

namespace fs = std::filesystem;

constexpr std::int64_t kMetadataTimeoutNanoseconds =
    15LL * NSEC_PER_SEC;

class ProbeError final : public std::runtime_error {
 public:
  ProbeError(std::string code, std::string message)
      : std::runtime_error(std::move(message)), code_(std::move(code)) {}

  const std::string& code() const noexcept { return code_; }

 private:
  std::string code_;
};

std::string ProbeFailure(std::string_view code, std::string_view message) {
  return "{\"ok\":false,\"error\":{\"code\":" + json::Quote(code) +
         ",\"message\":" + json::Quote(message) + "}}";
}

std::string Utf8(NSString* value) {
  if (value == nil) {
    return {};
  }
  const char* bytes = value.UTF8String;
  return bytes == nullptr ? std::string() : std::string(bytes);
}

std::string ErrorDescription(NSError* error) {
  if (error == nil) {
    return "unknown AVFoundation metadata error";
  }
  std::string description = Utf8(error.localizedDescription);
  return description.empty() ? "unknown AVFoundation metadata error"
                             : description;
}

fs::path ResolveInput(std::string_view input_path) {
  if (input_path.empty()) {
    throw ProbeError("invalid_path", "inputPath must not be empty");
  }
  if (input_path.find('\0') != std::string_view::npos) {
    throw ProbeError("invalid_path", "inputPath contains a null byte");
  }

  const fs::path requested{std::string(input_path)};
  if (!requested.is_absolute()) {
    throw ProbeError("invalid_path", "inputPath must be absolute");
  }

  std::error_code error;
  const fs::path canonical = fs::canonical(requested, error);
  if (error) {
    throw ProbeError("file_not_found",
                     "inputPath does not resolve to an existing file");
  }

  const fs::file_status status = fs::status(canonical, error);
  if (error || !fs::is_regular_file(status)) {
    throw ProbeError("invalid_path", "inputPath is not a regular file");
  }
  if (::access(canonical.c_str(), R_OK) != 0) {
    throw ProbeError("unreadable_file", "inputPath is not readable");
  }
  return canonical;
}

void LoadMetadataKey(id<AVAsynchronousKeyValueLoading> object,
                     NSString* key) {
  dispatch_semaphore_t completion = dispatch_semaphore_create(0);
  __block AVKeyValueStatus status = AVKeyValueStatusUnknown;
  __block NSError* load_error = nil;

  [object loadValuesAsynchronouslyForKeys:@[ key ]
                        completionHandler:^{
                          status = [object statusOfValueForKey:key
                                                        error:&load_error];
                          dispatch_semaphore_signal(completion);
                        }];

  const long wait_status = dispatch_semaphore_wait(
      completion,
      dispatch_time(DISPATCH_TIME_NOW, kMetadataTimeoutNanoseconds));
  if (wait_status != 0) {
    throw ProbeError("metadata_timeout",
                     "AVFoundation timed out while loading audio metadata");
  }
  if (status != AVKeyValueStatusLoaded) {
    throw ProbeError("metadata_load_failed", ErrorDescription(load_error));
  }
}

std::string FormatSampleRate(double sample_rate) {
  std::ostringstream output;
  output << std::setprecision(15) << sample_rate;
  return output.str();
}

std::string ProbeResolvedFile(const fs::path& input_path) {
  const std::string native_path = input_path.string();
  NSString* path =
      [[NSString alloc] initWithBytes:native_path.data()
                              length:native_path.size()
                            encoding:NSUTF8StringEncoding];
  if (path == nil) {
    throw ProbeError("invalid_path_encoding",
                     "inputPath is not valid UTF-8");
  }

  NSURL* url = [NSURL fileURLWithPath:path isDirectory:NO];
  AVURLAsset* asset = [AVURLAsset
      URLAssetWithURL:url
              options:@{AVURLAssetPreferPreciseDurationAndTimingKey : @YES}];
  if (asset == nil) {
    throw ProbeError("asset_open_failed",
                     "AVFoundation could not open inputPath");
  }

  LoadMetadataKey(asset, @"tracks");

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
  NSArray<AVAssetTrack*>* audio_tracks =
      [asset tracksWithMediaType:AVMediaTypeAudio];
#pragma clang diagnostic pop
  const NSUInteger audio_track_count = audio_tracks.count;
  if (audio_track_count == 0) {
    throw ProbeError("no_audio_track",
                     "AVFoundation found no audio track in inputPath");
  }

  AVAssetTrack* first_track = audio_tracks.firstObject;
  LoadMetadataKey(first_track, @"formatDescriptions");

#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
  NSArray* format_descriptions = first_track.formatDescriptions;
#pragma clang diagnostic pop
  const AudioStreamBasicDescription* stream_description = nullptr;
  for (id candidate in format_descriptions) {
    CMFormatDescriptionRef format =
        (__bridge CMFormatDescriptionRef)candidate;
    stream_description =
        CMAudioFormatDescriptionGetStreamBasicDescription(format);
    if (stream_description != nullptr) {
      break;
    }
  }

  if (stream_description == nullptr) {
    throw ProbeError(
        "audio_format_unavailable",
        "the first audio track has no readable stream description");
  }

  const UInt32 channels = stream_description->mChannelsPerFrame;
  const double sample_rate = stream_description->mSampleRate;
  if (channels == 0 || !std::isfinite(sample_rate) || sample_rate <= 0.0) {
    throw ProbeError(
        "invalid_audio_topology",
        "the first audio track reported invalid channels or sample rate");
  }

  return "{\"ok\":true,\"channels\":" + std::to_string(channels) +
         ",\"sampleRate\":" + FormatSampleRate(sample_rate) +
         ",\"audioTrackCount\":" +
         std::to_string(audio_track_count) + "}";
}

}  // namespace

std::string ProbeAudioTopology(std::string_view input_path) {
  try {
    @autoreleasepool {
      @try {
        return ProbeResolvedFile(ResolveInput(input_path));
      } @catch (NSException* exception) {
        const std::string reason = Utf8(exception.reason);
        return ProbeFailure(
            "avfoundation_exception",
            reason.empty() ? "AVFoundation raised an Objective-C exception"
                           : reason);
      }
    }
  } catch (const ProbeError& error) {
    return ProbeFailure(error.code(), error.what());
  } catch (const std::exception& error) {
    return ProbeFailure("audio_probe_failed", error.what());
  } catch (...) {
    return ProbeFailure("audio_probe_failed",
                        "unknown native audio probe failure");
  }
}

}  // namespace voz_neural
