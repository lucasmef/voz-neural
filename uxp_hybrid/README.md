# Voz Neural — UXP Hybrid para Premiere Pro

Este diretório contém o painel UXP, a ponte nativa C++ e os testes que
transformam o fluxo validado de voz em uma operação por clipe dentro do
Premiere Pro 26.3.

## Garantia de igualdade

O addon não reescreve o DSP em C++. Ele inicia o mesmo Python 3.12.13, o
mesmo NumPy 2.3.5, o mesmo runner e os mesmos binários usados na validação.
Isso preserva FFT, quantis, reamostragem, arredondamento PCM e todas as
constantes do preset.

O gate `scripts/runtime_check.sh` executa dois jobs reais e exige:

- saída padrão estéreo dual-mono SHA-256
  `97504319359c70c3dbe81737931b0a48cc5ebfa185014d1dd84d5059497e259b`;
- saída mono SHA-256
  `4407cb2ff0d78e7e19c54bdd85a9c3e046765e6e275b9ce4640fd9d17e7f39ba`;
- PCM de 24 bits, 48 kHz e canais estéreo idênticos;
- ausência de mudanças no runtime e nas mídias de referência.

## Fluxo no Premiere

1. O painel lê somente os clipes de áudio selecionados.
2. O Premiere exporta cada trecho, individualmente, em WAV estéreo de
   48 kHz/24 bits, preservando a topologia física da mídia.
3. Para fontes mono confirmadas, o modo fixo
   `premiere_mono_center_v1` inverte os `-3,0103 dB` aplicados pelo Media
   Encoder e canonicaliza o sinal em PCM16 antes de VAD, RNNoise,
   DeepFilterNet, máscaras e reconstrução residual.
4. O addon trata cada WAV com o runtime canônico.
5. O runner entrega estéreo dual-mono e o painel coloca os novos arquivos em
   novas faixas, na posição original. A mídia de origem não é relinkada nem
   sobrescrita.

Esse desenho mantém o processamento independente por clipe. A extração
estéreo evita downmix antecipado; a sonda nativa decide com segurança quando
a inversão da lei de pan mono realmente se aplica.

## Preparação local

O runtime é deliberadamente excluído do Git. Para montá-lo a partir dos
artefatos locais já validados:

```sh
uxp_hybrid/scripts/prepare_runtime.py
uxp_hybrid/scripts/runtime_check.sh
```

## Compilação

É necessário o UXP Hybrid Plugin SDK oficial da Adobe. O script detecta a
cópia local em `.deps/uxp-hybrid-plugin-sdk-6.5.0`; outro caminho pode ser
informado pela variável abaixo:

```sh
UXP_HYBRID_SDK_DIR=/caminho/para/o/sdk \
  uxp_hybrid/scripts/build_plugin.sh
```

O pacote carregável pelo UXP Developer Tool será criado em
`uxp_hybrid/dist/Voz Neural`.

Na ausência de CMake, o script usa o `clang++` oficial das Command Line Tools
do macOS.

## Estrutura

- `uxp/`: painel, manifesto e integração com a API do Premiere;
- `native/`: addon C++ que valida caminhos, inicia e cancela o runner;
- `runtime_source/`: launcher e frontend de entrada reproduzíveis;
- `runtime/`: staging local, ignorado pelo Git;
- `tests/`: contrato bit-exact;
- `scripts/`: preparação, verificação, build e empacotamento.
