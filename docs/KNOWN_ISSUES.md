# Pendências conhecidas — candidata local de consolidação

> Revisado em 2026-10-14 para `integration/yomu-main-consolidation-v1`. Não é status de release.
> O [estado consolidado](consolidation/YOMU_CURRENT_PRODUCT_STATE.md) e a [matriz](consolidation/YOMU_SOURCE_RECONCILIATION_MATRIX.md)
> são as fontes de verdade para integração e níveis de evidência.

- O loopback `127.0.0.1:18777` foi observado sem listener nesta revisão; a readiness UI somente diagnostica API/fila/worker/schema e não inicia túnel nem comprova login Gemini.
- O job `128e8082d6c34aec8af7a832b7e9610f` tem resumo histórico de três `ConnectionError`, mas seus logs/JobStore correlacionados não estavam disponíveis nos caminhos locais consultados. A classe específica da falha segue `NOT_PROVEN`.
- O delta de readiness/transporte ainda está somente no worktree, sem build frozen correspondente. Não usar o EXE anterior como prova das alterações atuais.
- A reconciliação de overlays sujos e tips fora da candidata continua incompleta; beta/main update permanecem bloqueados.

## Notas históricas preservadas

Os itens seguintes são registros antigos e não afirmam o status atual de release:

- Incidente histórico de aproximadamente 27 minutos: `NOT_REPRODUCIBLE_EXACT_INPUT_UNAVAILABLE`.
- Grouping/classification possui otimização futura possível, sem explosão no workload local de 35 páginas.
- `review_required` pode ser legítimo mesmo com PDF válido.
- Estado histórico pré-Beta 17: signing/updater/installer, distribuição e clean-VM apareciam
  como pendentes. Não use esta linha como status atual de release.
