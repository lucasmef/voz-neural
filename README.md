# Voz Neural para Adobe Premiere Pro

Este repositório privado contém somente o código-fonte do fluxo de voz
feminina reconstruído a partir de testes de caixa-preta e a integração em
desenvolvimento para o Adobe Premiere Pro.

O acervo local completo — áudios brutos, referências do CapCut, renders,
modelos, binários, projetos Adobe e métricas — permanece fora do Git em:

`/Users/lucas/Documents/Codex/voz-neural`

## Resultado de referência

O núcleo de produção está em `independent_neural_flow_20260729/`. Seu preset
padrão entrega WAV PCM de 24 bits, 48 kHz e estéreo dual-mono para evitar a
atenuação de pan aplicada a clipes mono no Premiere.

Para a fixture 65, o contrato bit-exact atual é:

- WAV final: `97504319359c70c3dbe81737931b0a48cc5ebfa185014d1dd84d5059497e259b`
- PCM intercalado: `24630cb72ad06aa6c5353564c2b98c1bef477894bdecce3d4f8ea6b9106ad8c1`
- PCM de cada canal: `1b84148dffc8c69aa36e27cd438747ac97b8e7f728cfcfb36ff4d96ed5836d38`

## UXP Hybrid

`uxp_hybrid/` contém o painel para Premiere Pro 26.3 e a ponte C++ nativa.
A primeira versão mantém o runner Python/NumPy congelado como fonte de
verdade. Essa decisão evita diferenças numéricas em FFT, quantis,
reamostragem, biquads e arredondamento.

O painel processa cada clipe selecionado como um job independente, importa
o novo WAV e o insere de maneira reversível, sem relinkar a mídia original.

## Dependências

As versões e hashes aceitos estão em `DEPENDENCIES.lock`. Binários e modelos
não pertencem ao Git e devem ser montados no pacote local pelo bootstrap.

## Privacidade

Não inclua mídia, projetos do Premiere/CapCut, modelos, executáveis ou
resultados de clientes. A política de allow-list do `.gitignore` bloqueia
esses arquivos por padrão.
