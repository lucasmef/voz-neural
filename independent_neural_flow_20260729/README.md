# Fluxo de voz independente — perfil 75

Este pacote processa WAVs de voz no macOS e entrega um perfil calibrado para
se aproximar do tratamento usado nos exports de referência a 75%. Ele está
pronto para uso local, mas **não é uma cópia bit a bit do CapCut**.

## Uso mais simples

Arraste um WAV sobre:

`PROCESSAR_VOZ_CAPCUT75.command`

Também é possível abrir o arquivo `.command` e escolher um WAV. O resultado é
gravado ao lado do original:

`<nome>_VOZ_CAPCUT75.wav`

O processamento não reproduz som. Ele também grava um JSON de diagnóstico ao
lado do WAV. Se o nome já existir, o wrapper cria `_2`, `_3` e assim por
diante; ele não sobrescreve silenciosamente o render anterior.

Por padrão, o resultado é **estéreo dual-mono**: os canais esquerdo e direito
contêm exatamente as mesmas amostras. Esse formato entra no Premiere sem a
atenuação aproximada de 3,01 dB que pode ocorrer quando um clipe mono é
centralizado em uma faixa estéreo.

## Uso pelo Terminal

```bash
python3 independent_voice_runner.py entrada.wav saida.wav
```

Para forçar saída mono:

```bash
python3 independent_voice_runner.py entrada.wav saida.wav --mono
```

O runner aceita WAV PCM inteiro de 8, 16, 24 ou 32 bits. A saída é WAV PCM de
24 bits, na mesma taxa de amostragem e com a mesma duração do arquivo de
entrada. Toda entrada é analisada em mono e, por padrão, volta como estéreo
dual-mono, a menos que `--mono` seja usado.

## Cadeia congelada

1. RNNoise analisa o áudio bruto e fornece probabilidade de voz a cada 10 ms.
2. DeepFilterNet faz a primeira separação; a mistura preserva 5% do bruto.
3. DeepFilterNet faz uma segunda passagem; a mistura preserva 25% do estágio
   anterior.
4. O DSP local aplica a curva de presença/“peso” reconstruída.
5. O nível da voz é ajustado dinamicamente para mediana ativa de -15,5 dBFS.
6. O residual usa somente bruto, primeira separação e VAD. A mistura entre
   ambiência, transição e voz é contínua — não há gate binário.
7. O limiter final tem teto de -0,30 dBFS.

Os valores estão congelados no `FlowSettings` de
`independent_voice_runner.py`.

## O que o runtime não usa

O processamento **não abre, carrega nem chama**:

- executável do CapCut;
- biblioteca, modelo neural, runtime ou cache do CapCut;
- projeto do CapCut;
- site, API ou serviço de rede do CapCut.

Os exports do CapCut existem apenas fora do runtime, como referências offline
para `validate_real_pairs.py`. O runner de produção recebe somente o WAV de
entrada.

## Componentes redistribuíveis

- [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet), versão 0.5.6:
  licença dupla MIT/Apache-2.0. O binário arm64 veio da
  [página oficial de releases](https://github.com/Rikorose/DeepFilterNet/releases).
  SHA-256:
  `4601e7f4e4c03e59a4c5b5000216ef3add3e808799cfccd95e14e83ea4611081`.
- [RNNoise](https://github.com/xiph/rnnoise): licença BSD-3-Clause. Neste
  pacote ele é usado somente para VAD; o áudio denoised do RNNoise é
  descartado. SHA-256 do helper arm64:
  `b12848f27892a77e0b454b62ff5ce8e28d23d754848f142960a8f85756457357`.
- `runtime_dsp.py`: I/O PCM, alinhamento, resampling, EQ e limiter locais,
  sem dependência de código ou modelo do CapCut.

## Validação

O preset congelado foi validado em 15 pares reais alinhados, incluindo seis
pares diretos e nove matches adicionais. O relatório completo está em
`RELATORIO_FINAL.md`; os dados brutos estão em `VALIDATION.json`.

O caso 65 pronto para escuta cega está em `AB_FINAL_65/`.

O teste reproduzível do layout para Premiere está em
`test_premiere_dual_mono.py`. Ele confirma que os dois canais do WAV padrão
são idênticos entre si e idênticos, amostra por amostra, ao WAV criado com
`--mono`. O resultado da execução final está em
`PREMIERE_DUAL_MONO_VALIDATION.json`.

A passagem completa pelo Premiere, incluindo as medições do export real, está
documentada em `PREMIERE_INTEGRATION.md`.

## Limites atuais

- A calibração é para voz feminina falada e conteúdo curto/reels.
- O comportamento de voz forte já está muito próximo das referências. As
  maiores diferenças restantes aparecem em respirações e transições de VAD.
- Um dos 15 pares ficou fora da margem de 3 dB na mediana de fundo, embora
  tenha passado na energia RMS quieta.
- O pacote é um processador offline; ainda não é um VST/AU nativo do Premiere.
