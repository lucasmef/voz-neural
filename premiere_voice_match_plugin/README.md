# Voz Clara Feminina 0.3 — Match CapCut

Correção VST3 para usar depois das ferramentas de IA do Adobe Premiere Pro.

## Fluxo medido

1. Marcar cada clipe como `Diálogo`.
2. Aplicar `Enhance Speech` em `7,0`.
3. Em `Loudness`, executar `Auto-Match` para `-23 LUFS`.
4. Aplicar `Voz Clara Feminina` com os valores padrão.
5. Exportar com o preset `Wave Only Audio`.

Ao atualizar da versão 0.2, remova instâncias antigas do efeito e adicione o
plug-in novamente. Projetos antigos podem conservar os valores da versão
anterior mesmo depois da atualização.

O plug-in não executa uma segunda redução neural de ruído. Ele faz somente a
correção final medida: rejeição de subgrave, ajuste de presença/clareza, ar,
ganho final de 7,8 dB e limitador a -0,32 dBFS com soltura de 500 ms.

Nos dois pares femininos disponíveis para fechar a cadeia de IA a 70%, o erro
de RMS ficou em aproximadamente 0,03–0,04 dB. O erro espectral médio ficou
entre aproximadamente 1,15 e 1,19 dB. Um dos dois testes reconstruiu
offline o ganho escalar do Auto-Match porque o Premiere ficou bloqueado antes
da última exportação.

O projeto não contém código do CapCut. A resposta foi estimada por medições de
caixa-preta dos pares originais e tratados.
