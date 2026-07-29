# Voz Neural — painel UXP Hybrid

Camada de interface e integração com o Premiere Pro 26.3. O painel usa sempre
o preset validado `capcut75-premiere-stereo-v1`; não há controles que possam
descaracterizar o resultado aprovado.

## Comportamento seguro

- considera somente `AudioClipTrackItem` selecionados na sequência ativa;
- processa cada clipe em um job separado, preservando seu próprio recorte;
- importa WAVs novos e os coloca a partir da posição original;
- cria quantas faixas novas forem necessárias quando clipes se sobrepõem;
- nunca faz relink, não apaga mídias e não substitui os originais;
- desativar o original é uma opção desmarcada por padrão e incluída na mesma
  transação reversível do Premiere.

Clipes offline, reversos, com velocidade alterada, mesclados, multicâmera ou
sequências aninhadas são recusados com uma orientação clara para renderizar
antes.

## Addon esperado

O manifesto v6 carrega `voz-neural.uxpaddon`. O binário deve existir na
estrutura de plataforma exigida pelo UXP:

```text
mac/arm64/voz-neural.uxpaddon
mac/x64/voz-neural.uxpaddon
win/x64/voz-neural.uxpaddon
```

Para o Mac atual, apenas `mac/arm64` é necessário durante o desenvolvimento.
O contrato síncrono é:

```text
getRuntimeInfo() -> JSON string
probeAudioTopology(inputPath) -> JSON string
processFile(JSON string) -> JSON string
cancel(requestId ou JSON string) -> JSON string
```

Antes de extrair, o painel usa `probeAudioTopology` na mídia original. Somente
uma trilha de áudio mono ou estéreo é aceita. A extração usa obrigatoriamente
`Wave48stereo24.epr`: mono recebe
`inputCompensationMode=premiere_mono_center_v1`; estéreo recebe
`inputCompensationMode=none`.

`processFile` recebe `inputPath`, `outputPath`, `requestId`, `preset`,
`inputCompensationMode`, `timelineStartSeconds`, `timelineEndSeconds` e
`clipName`. `sourceInSeconds` e `sourceOutSeconds` ficam registrados como
metadados de auditoria; o recorte já é materializado pelo AME no WAV de
entrada antes do runner. O addon cria o diretório pai da saída.

## Verificação estática

Na pasta `uxp`:

```sh
node tests/static-check.js
node --check main.js
node --check js/addon-adapter.js
node --check js/premiere-adapter.js
node --check js/workflow.js
node --check js/ui.js
```

O teste real no host deve ser feito pelo UXP Developer Tool com o addon
arm64 compilado e o modo de desenvolvedor habilitado no Premiere.
