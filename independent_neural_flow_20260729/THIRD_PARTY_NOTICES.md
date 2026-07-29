# Third-party notices

## DeepFilterNet 0.5.6

- Projeto: https://github.com/Rikorose/DeepFilterNet
- Autor/copyright: Hendrik Schröter, 2021
- Licença do projeto: MIT ou Apache-2.0, à escolha do usuário.
- Opção usada para este pacote: MIT.
- Texto incluído: `licenses/DEEPFILTERNET_MIT.txt`
- Binário incluído:
  `bin/deep-filter-0.5.6-aarch64-apple-darwin`
- SHA-256:
  `4601e7f4e4c03e59a4c5b5000216ef3add3e808799cfccd95e14e83ea4611081`

O modelo que acompanha o binário é o modelo redistribuível do próprio release
oficial. Não é um modelo extraído do CapCut.

## RNNoise

- Projeto: https://github.com/xiph/rnnoise
- Copyrights listados no texto incluído: Jean-Marc Valin; Amazon; Mozilla;
  Xiph.Org Foundation; e Mark Borgerding.
- Licença: BSD-3-Clause.
- Texto incluído: `licenses/RNNOISE_BSD-3-Clause.txt`
- Helper incluído: `bin/rnnoise_vad_arm64`
- SHA-256:
  `b12848f27892a77e0b454b62ff5ce8e28d23d754848f142960a8f85756457357`

O helper chama `rnnoise_process_frame` somente para obter a probabilidade de
voz. O sinal denoised calculado pela API não entra na saída do runner.

## Ausência de componente do CapCut

Nenhum executável, biblioteca, modelo, runtime, cache, asset ou código do
CapCut é incluído neste pacote. O nome “CAPCUT75” no wrapper identifica apenas
o perfil de referência ao qual o fluxo foi calibrado.
