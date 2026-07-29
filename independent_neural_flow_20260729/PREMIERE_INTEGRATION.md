# Integração validada no Premiere

Data: 29/07/2026  
Premiere Pro: 26.3.0  
Projeto isolado: `Teste_Audio_CapCut_Neural_2026-07-29.prproj`

## Resultado

O runner entrega **estéreo dual-mono por padrão**. Os canais esquerdo e
direito recebem exatamente as mesmas amostras processadas. Assim, o arquivo
entra numa sequência estéreo do Premiere sem a atenuação aproximada de
3,01 dB observada anteriormente com um WAV mono centralizado.

Não é necessário aplicar `+3,016 dB`, normalização, EQ ou outro efeito ao
arquivo novo.

## Teste completo — par 65

Entrada:

- `/Users/lucas/Movies/Vídeos/Edit/65.wav`

Saída direta do pacote:

- `B_NOSSO_FLUXO_65_PREMIERE_STEREO.wav`;
- 48 kHz, PCM 24-bit, estéreo dual-mono;
- pico: -0,9845 dBFS;
- diferença máxima entre os canais: zero.

Comparada ao render mono anterior do mesmo fluxo, após apenas igualar a taxa
de amostragem:

- correlação: 0,999999999995;
- diferença de RMS: menor que 0,000001 dB;
- diferença residual: -148,84 dBFS.

Export do Premiere com `Waveform Audio`, 48 kHz, estéreo, 16-bit:

- `PREMIERE_STEREO_SEM_COMPENSACAO_65.wav`;
- correlação após alinhar a posição da timeline: 0,999999565;
- diferença de RMS: -0,000618 dB;
- correção ótima residual: +0,000613 dB;
- residual: -96,25 dBFS;
- SNR entre fonte e export: 77,58 dB;
- diferença máxima entre os canais do export: zero.

O lag medido foi exatamente um quadro da sequência de 25 fps, ou 40 ms,
causado somente pela posição do clipe de teste na timeline. O export também
terminou 31,94 ms depois do WAV por causa do limite de quadro da faixa Source
In/Out. Esses dois valores não representam processamento de áudio.

## Fluxo recomendado

1. Arraste o WAV bruto sobre `PROCESSAR_VOZ_CAPCUT75.command`.
2. Importe o arquivo `_VOZ_CAPCUT75.wav` gerado.
3. Deixe o ganho do clipe e da faixa em `0,0 dB`.
4. Não aplique a antiga compensação de `+3,016 dB`.
5. Exporte como Waveform Audio, 48 kHz, Stereo.

Se for necessário um arquivo realmente mono para outro sistema, use a opção
`--mono` pelo Terminal. Para o Premiere, mantenha a saída padrão.

