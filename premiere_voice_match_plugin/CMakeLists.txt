cmake_minimum_required(VERSION 3.25)

set(CMAKE_OSX_DEPLOYMENT_TARGET "11.0" CACHE STRING "macOS deployment target")

project(VozClaraFeminina
    VERSION 0.3.0.0
    DESCRIPTION "Correcao CapCut para voz feminina no Adobe Premiere Pro"
)

set(SMTG_ENABLE_VST3_HOSTING_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SMTG_ENABLE_VST3_PLUGIN_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SMTG_ENABLE_VSTGUI_SUPPORT OFF CACHE BOOL "" FORCE)
set(SMTG_CREATE_PLUGIN_LINK OFF CACHE BOOL "" FORCE)
set(SMTG_RUN_VST_VALIDATOR OFF CACHE BOOL "" FORCE)

set(vst3sdk_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/vst3sdk")
set(rnnoise_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/rnnoise")

add_subdirectory("${vst3sdk_SOURCE_DIR}" "${PROJECT_BINARY_DIR}/vst3sdk")
smtg_enable_vst3_sdk()

set(RNNOISE_SOURCES
    "${rnnoise_SOURCE_DIR}/src/denoise.c"
    "${rnnoise_SOURCE_DIR}/src/rnn.c"
    "${rnnoise_SOURCE_DIR}/src/pitch.c"
    "${rnnoise_SOURCE_DIR}/src/kiss_fft.c"
    "${rnnoise_SOURCE_DIR}/src/celt_lpc.c"
    "${rnnoise_SOURCE_DIR}/src/nnet.c"
    "${rnnoise_SOURCE_DIR}/src/nnet_default.c"
    "${rnnoise_SOURCE_DIR}/src/parse_lpcnet_weights.c"
    "${rnnoise_SOURCE_DIR}/src/rnnoise_data.c"
    "${rnnoise_SOURCE_DIR}/src/rnnoise_tables.c"
)

add_library(rnnoise_embedded STATIC ${RNNOISE_SOURCES})
target_include_directories(rnnoise_embedded
    PUBLIC "${rnnoise_SOURCE_DIR}/include"
    PRIVATE "${rnnoise_SOURCE_DIR}/src"
)
target_compile_definitions(rnnoise_embedded PRIVATE RNNOISE_BUILD=1)
set_target_properties(rnnoise_embedded PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    C_VISIBILITY_PRESET hidden
)

add_library(voz_clara_dsp STATIC
    source/voice_dsp.cpp
    source/voice_dsp.h
)
target_compile_features(voz_clara_dsp PUBLIC cxx_std_17)
target_include_directories(voz_clara_dsp PUBLIC source)
target_link_libraries(voz_clara_dsp PUBLIC rnnoise_embedded)
set_target_properties(voz_clara_dsp PROPERTIES POSITION_INDEPENDENT_CODE ON)

add_executable(voz_clara_cli
    tools/voice_dsp_cli.cpp
)
target_compile_features(voz_clara_cli PUBLIC cxx_std_17)
target_link_libraries(voz_clara_cli PRIVATE voz_clara_dsp)

add_executable(rnnoise_vad_cli
    tools/rnnoise_vad_cli.cpp
)
target_compile_features(rnnoise_vad_cli PUBLIC cxx_std_17)
target_link_libraries(rnnoise_vad_cli PRIVATE rnnoise_embedded)

smtg_add_vst3plugin(VozClaraFeminina
    PACKAGE_NAME "Voz Clara Feminina"
    SOURCES_LIST
        source/controller.cpp
        source/controller.h
        source/factory.cpp
        source/ids.h
        source/parameters.h
        source/processor.cpp
        source/processor.h
        source/version.h
)

target_compile_features(VozClaraFeminina PUBLIC cxx_std_17)
target_link_libraries(VozClaraFeminina PRIVATE sdk voz_clara_dsp)
smtg_target_configure_version_file(VozClaraFeminina)

if(SMTG_MAC)
    smtg_target_set_bundle(VozClaraFeminina
        BUNDLE_IDENTIFIER "com.lucas.vozclarafeminina"
        COMPANY_NAME "Lucas"
    )
    set_target_properties(VozClaraFeminina PROPERTIES
        MACOSX_BUNDLE_GUI_IDENTIFIER "com.lucas.vozclarafeminina"
        MACOSX_BUNDLE_BUNDLE_NAME "Voz Clara Feminina"
        MACOSX_BUNDLE_BUNDLE_VERSION "${PROJECT_VERSION}"
        MACOSX_BUNDLE_SHORT_VERSION_STRING "${PROJECT_VERSION}"
        MACOSX_BUNDLE_COPYRIGHT "Copyright 2026 Lucas"
    )
    get_target_property(VOZ_CLARA_PACKAGE_PATH
        VozClaraFeminina SMTG_PLUGIN_PACKAGE_PATH)
    add_custom_command(
        TARGET VozClaraFeminina POST_BUILD
        COMMAND codesign --force --deep --sign - "${VOZ_CLARA_PACKAGE_PATH}"
        COMMENT "Assinando o pacote VST3 completo"
    )
endif()
