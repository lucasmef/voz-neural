# Voz Neural para Adobe Premiere Pro

Este repositório público contém somente o código-fonte do fluxo de voz
feminina reconstruído a partir de testes de caixa-preta e a integração em
desenvolvimento para o Adobe Premiere Pro.

Áudios brutos, referências, renders, modelos, binários, projetos Adobe e métricas permanecem em armazenamento local fora do Git.

## Resultado de referência

O núcleo de produção está em `independent_neural_flow_20260729/`. Seu preset
padrão entrega WAV PCM de 24 bits, 48 kHz e estéreo dual-mono para evitar a
atenuação de pan aplicada a clipes mono no Premiere.

As verificações bit-exact usam arquivos de referência locais, que não acompanham o repositório. Os hashes de dependências e versões aceitas estão em `DEPENDENCIES.lock`.

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
