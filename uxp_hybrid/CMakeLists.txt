cmake_minimum_required(VERSION 3.24)

project(voz_neural_uxp_addon VERSION 0.1.0 LANGUAGES CXX OBJCXX)

if(NOT APPLE)
  message(FATAL_ERROR
    "This target currently supports only macOS arm64. "
    "Configure it on an Apple Silicon Mac.")
endif()

set(UXP_HYBRID_SDK_DIR "" CACHE PATH
  "Root directory of the Adobe UXP Hybrid Plugin SDK")
option(VOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE
  "Allow processFile.runtimeRoot for local development only" OFF)

if(NOT UXP_HYBRID_SDK_DIR)
  message(FATAL_ERROR
    "UXP_HYBRID_SDK_DIR is required. Download the official Adobe UXP "
    "Hybrid Plugin SDK, then configure with "
    "-DUXP_HYBRID_SDK_DIR=/absolute/path/to/the/unpacked/sdk")
endif()

find_path(UXP_ADDON_UTILITIES_INCLUDE_DIR
  NAMES UxpAddon.h
  HINTS
    "${UXP_HYBRID_SDK_DIR}/src/utilities"
    "${UXP_HYBRID_SDK_DIR}/utilities"
  NO_DEFAULT_PATH)

find_path(UXP_ADDON_API_INCLUDE_DIR
  NAMES UxpAddonShared.h
  HINTS
    "${UXP_HYBRID_SDK_DIR}/src/api"
    "${UXP_HYBRID_SDK_DIR}/api"
  NO_DEFAULT_PATH)

if(NOT UXP_ADDON_UTILITIES_INCLUDE_DIR OR NOT UXP_ADDON_API_INCLUDE_DIR)
  message(FATAL_ERROR
    "The Adobe UXP Hybrid Plugin SDK is incomplete or the path is wrong. "
    "Expected UxpAddon.h under src/utilities and UxpAddonShared.h under "
    "src/api inside UXP_HYBRID_SDK_DIR='${UXP_HYBRID_SDK_DIR}'.")
endif()

if(NOT CMAKE_OSX_ARCHITECTURES)
  set(CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING
    "Architectures built for macOS" FORCE)
endif()
if(NOT CMAKE_OSX_ARCHITECTURES STREQUAL "arm64")
  message(FATAL_ERROR
    "This target is intentionally frozen for macOS arm64. "
    "Set -DCMAKE_OSX_ARCHITECTURES=arm64.")
endif()
if(NOT CMAKE_OSX_DEPLOYMENT_TARGET)
  set(CMAKE_OSX_DEPLOYMENT_TARGET "12.0" CACHE STRING
    "Minimum supported macOS version" FORCE)
endif()

add_library(voz_neural_uxpaddon MODULE
  native/audio_probe.mm
  native/bridge.cpp
  native/json.cpp
  native/module.cpp)

target_compile_features(voz_neural_uxpaddon PRIVATE cxx_std_20)
target_include_directories(voz_neural_uxpaddon PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/native"
  "${UXP_ADDON_UTILITIES_INCLUDE_DIR}"
  "${UXP_ADDON_API_INCLUDE_DIR}")
target_compile_definitions(voz_neural_uxpaddon PRIVATE
  VOZ_NEURAL_ADDON_VERSION="${PROJECT_VERSION}"
  VOZ_NEURAL_RUNTIME_RELATIVE_PATH="runtime"
  VOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE=$<BOOL:${VOZ_NEURAL_ALLOW_RUNTIME_ROOT_OVERRIDE}>)
target_compile_options(voz_neural_uxpaddon PRIVATE
  -Wall
  -Wextra
  -Wpedantic
  $<$<COMPILE_LANGUAGE:OBJCXX>:-fobjc-arc>)

find_library(AVFOUNDATION_FRAMEWORK AVFoundation REQUIRED)
find_library(AUDIOTOOLBOX_FRAMEWORK AudioToolbox REQUIRED)
find_library(COREMEDIA_FRAMEWORK CoreMedia REQUIRED)
find_library(FOUNDATION_FRAMEWORK Foundation REQUIRED)
target_link_libraries(voz_neural_uxpaddon PRIVATE
  "${AVFOUNDATION_FRAMEWORK}"
  "${AUDIOTOOLBOX_FRAMEWORK}"
  "${COREMEDIA_FRAMEWORK}"
  "${FOUNDATION_FRAMEWORK}")

set_target_properties(voz_neural_uxpaddon PROPERTIES
  CXX_STANDARD 20
  CXX_STANDARD_REQUIRED YES
  OBJCXX_STANDARD 20
  OBJCXX_STANDARD_REQUIRED YES
  CXX_VISIBILITY_PRESET hidden
  VISIBILITY_INLINES_HIDDEN YES
  PREFIX ""
  OUTPUT_NAME "voz-neural"
  SUFFIX ".uxpaddon"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/mac/arm64")

install(TARGETS voz_neural_uxpaddon
  LIBRARY DESTINATION "mac/arm64")

include(CTest)
if(BUILD_TESTING)
  add_executable(voz_neural_audio_probe_smoke
    native/audio_probe.mm
    native/json.cpp
    tests/audio_probe_smoke.cpp)
  target_compile_features(voz_neural_audio_probe_smoke PRIVATE cxx_std_20)
  set_target_properties(voz_neural_audio_probe_smoke PROPERTIES
    CXX_STANDARD 20
    CXX_STANDARD_REQUIRED YES
    OBJCXX_STANDARD 20
    OBJCXX_STANDARD_REQUIRED YES)
  target_include_directories(voz_neural_audio_probe_smoke PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/native")
  target_compile_options(voz_neural_audio_probe_smoke PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    $<$<COMPILE_LANGUAGE:OBJCXX>:-fobjc-arc>)
  target_link_libraries(voz_neural_audio_probe_smoke PRIVATE
    "${AVFOUNDATION_FRAMEWORK}"
    "${AUDIOTOOLBOX_FRAMEWORK}"
    "${COREMEDIA_FRAMEWORK}"
    "${FOUNDATION_FRAMEWORK}")
  add_test(NAME voz_neural_audio_probe_smoke
    COMMAND voz_neural_audio_probe_smoke)
endif()
