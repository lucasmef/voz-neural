# Audition Sound Lift × CapCut

Este diretório contém um comparador independente para os quatro stems do
**Audition Sound Lift**:

- `Speech`
- `Background`
- `Noise`
- `Reverb`

Ele não altera nenhum relatório ou export existente. O objetivo é responder a
duas perguntas diferentes:

1. uma mistura linear dos stems consegue reproduzir o estágio
   `CAPCUT_ENHANCE_ONLY_75.WAV`?
2. essa mesma mistura já chega perto de
   `CAPCUT_FULL_CHAIN_75_RECHECK.WAV`, ou ainda faltam denoise, EQ e dinâmica?

## Uso

O script requer Python 3 com NumPy. Nesta máquina, uma invocação já verificada
para checar as dependências é:

```bash
'/Users/lucas/Library/Application Support/Eagle/Plugins/ai-search/python-env/bin/python3' \
  adobe_podcast_v2_test_20260728/compare_sound_lift_to_capcut.py \
  --check
```

Quando os quatro stems estiverem exportados:

```bash
PYTHON='/Users/lucas/Library/Application Support/Eagle/Plugins/ai-search/python-env/bin/python3'

"$PYTHON" adobe_podcast_v2_test_20260728/compare_sound_lift_to_capcut.py \
  --speech '/caminho/Speech.wav' \
  --background '/caminho/Background.wav' \
  --noise '/caminho/Noise.wav' \
  --reverb '/caminho/Reverb.wav' \
  --output-dir adobe_podcast_v2_test_20260728/results \
  --write-top 3
```

Se os stems vierem do painel UXP deste diretório, eles serão arquivos
individuais como `S01_Speech.wav`, `S01_Background.wav` etc. Nesse caso não os
passe como se fossem a timeline completa. Use o modo de lote:

```bash
./adobe_podcast_v2_test_20260728/compare_soundlift_output.sh
```

Esse comando monta S01, S03, S06, M04 e F02 em suas posições originais,
mantendo o reset neural por clipe, e limita o ranking a esses cinco trechos. O
primeiro argumento opcional troca a pasta dos stems e o segundo troca a pasta
de resultados:

```bash
./adobe_podcast_v2_test_20260728/compare_soundlift_output.sh \
  '/outra/pasta/de/stems' \
  '/outra/pasta/de/resultados'
```

Os caminhos das referências CapCut, do áudio cru, dos timecodes e do decoder
AVFoundation já possuem defaults relativos à raiz deste projeto.

Uma variante manual também pode ser acrescentada:

```bash
"$PYTHON" adobe_podcast_v2_test_20260728/compare_sound_lift_to_capcut.py \
  --speech '/caminho/Speech.wav' \
  --background '/caminho/Background.wav' \
  --noise '/caminho/Noise.wav' \
  --reverb '/caminho/Reverb.wav' \
  --variant 'meu_teste=1,0.25,0.10,0.20'
```

A ordem dos quatro números é sempre:

```text
Speech, Background, Noise, Reverb
```

## Saídas

- `COMPARISON.json`: métricas completas, parâmetros e limitações;
- `SUMMARY.md`: relatório legível com baseline e os melhores rankings;
- `VARIANT_RANKING.csv`: ranking compacto contra aprimoramento 75%, cadeia
  completa e compromisso entre ambos;
- `PER_CLIP_METRICS.csv`: métricas para cada um dos 30 clipes;
- `TOP_*.wav`: melhores misturas para audição cega posterior.

Além das variantes fixas, o script cria pesos positivos ajustados
automaticamente para:

- o aprimoramento CapCut a 75%;
- a cadeia completa CapCut;
- o compromisso entre as duas referências.

Se nem o melhor ajuste automático obtiver boa aproximação, a diferença não é
explicada apenas pelos sliders dos stems. Isso indica a necessidade de etapas
não lineares posteriores (denoise, EQ dinâmica, compressão/limiter ou outra
reconstrução neural).

## Critérios

O ranking combina:

- erro de waveform após ajuste de ganho;
- correlação de envelope;
- distância espectral por bandas;
- diferença de contraste entre trechos ativos e silenciosos;
- consistência do ganho necessário entre clipes;
- diferença de nível sem compensação.

O resultado é uma métrica de engenharia, não substitui escuta cega. A análise
principal é mono porque o decoder AVFoundation já validado no projeto faz
downmix; diferenças de imagem estéreo precisam de uma auditoria separada.
