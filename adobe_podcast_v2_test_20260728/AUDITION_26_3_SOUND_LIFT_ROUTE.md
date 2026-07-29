# Rota Audition 26.3 — separação neural de fala e ambiente

## Conclusão

O Audition 26.3.0 instalado contém um filtro neural chamado **Sound Lift**. Ele é diferente de `DeNoise`, `DeReverb` e `Enhance Speech`: o filtro declara seis saídas independentes:

1. `Speech`
2. `Music`
3. `Sfx`
4. `Noise`
5. `Reverb`
6. `Background`

Isso oferece, em princípio, a arquitetura necessária para reproduzir o comportamento observado no CapCut: voz tratada mais uma quantidade controlada de ambiente residual.

## Evidência local

- Framework: `Adobe Audition 2026.app/Contents/Frameworks/dvaaudiofilters.framework`
- Filtro: `Sound Lift`
- Modelo: `SoundLift4Way-20260122`
- Preset de fábrica: `Version 1`
- Taxa interna: 48 kHz
- Modelo declarado como `ccd-on-demand`; o primeiro uso provavelmente exige Creative Cloud autenticada e download do modelo.
- Metadados do modelo: `Resources/MLModels/model_metadata.json`
- O filtro aceita mono e estéreo.

O pacote também registra uma API ExtendScript interna:

```text
dvaaudiofilters.AudioFilterProcessor
  getPresetsForAudioFilter(audioFilter)
  isAudioChannelLayoutSupportedByAudioFilter(audioChannelLayout, audioFilter)
  process(audioFilter, preset, inputWaveFile, outputs)
  compare(inputWaveFile, baseline)

dvaaudiofilters.WaveFile(filepath)
```

`process` é assíncrono e grava um WAV por barramento de saída. A forma provável da chamada é:

```jsx
var processor = new dvaaudiofilters.AudioFilterProcessor();
var input = new dvaaudiofilters.WaveFile("/caminho/entrada.wav");
var outputs = [
    new dvaaudiofilters.WaveFile("/caminho/Speech.wav"),
    new dvaaudiofilters.WaveFile("/caminho/Music.wav"),
    new dvaaudiofilters.WaveFile("/caminho/Sfx.wav"),
    new dvaaudiofilters.WaveFile("/caminho/Noise.wav"),
    new dvaaudiofilters.WaveFile("/caminho/Reverb.wav"),
    new dvaaudiofilters.WaveFile("/caminho/Background.wav")
];
processor.process("Sound Lift", "Version 1", input, outputs);
```

Essa sintaxe ainda precisa ser validada dentro do host. A API se descreve como ferramenta de testes automatizados e não aparece na documentação pública; portanto, não deve ser tratada como interface estável.

## O que o Audition expõe oficialmente

- `DeNoise`: redução de ruído com `Amount`, `Gain`, foco espectral e `Output Noise Only`.
- `DeReverb`: redução de reverberação, sem produzir stem de ambiente.
- `Noise Reduction (process)`: usa impressão de ruído e permite auditar `Output Noise Only`.
- Effects Rack/Favorites/Batch Process: caminho oficial para aplicar efeitos em lote.

`Output Noise Only` é apenas o sinal rejeitado pelo restaurador. Ele pode conter voz e reverberação e **não equivale** a um stem neural de ambiente.

## Limites de automação

- Não foi encontrado executável de processamento headless nem opção de linha de comando para Sound Lift.
- `dvaaudiofilterscan` apenas examina plug-ins; não processa áudio.
- O Audition suporta CEP/ExtendScript, mas a automação precisa rodar dentro do host.
- Um painel CEP assinado pode chamar ExtendScript sem alterar preferências. Usar extensão sem assinatura exigiria ativar modo de desenvolvimento, o que altera preferências e não deve ser feito implicitamente.
- A documentação pública atual do Audition 26.3 não anuncia Sound Lift. O motor está registrado no framework compartilhado, mas sua presença no menu do Audition ainda deve ser verificada em uma sessão descartável.

## Teste mínimo recomendado

1. Trabalhar somente com cópias WAV de 48 kHz dentro da pasta de teste.
2. Processar um clipe feminino representativo com Sound Lift.
3. Exportar `Speech`, `Background`, `Noise` e `Reverb`.
4. Comparar estas reconstruções, com loudness final igualado:
   - `Speech`
   - `Speech + 25% de Background`
   - `Speech + 25% de (Noise + Reverb)`
5. Aplicar a cadeia de presença/dinâmica depois da reconstrução, não antes.
6. Comparar contra o CapCut em métricas de loudness, espectro, modulação e residual, além da audição.

O primeiro alvo deve ser `Speech + 25% de Background`, pois a Adobe descreve publicamente o controle de intensidade do Enhance Speech v2, baseado em Sound Lift, como um balanço entre clareza da fala e ruído ambiente.
