# Pendências conhecidas — candidata local de consolidação

> Revisado em 2026-10-15 para `integration/yomu-main-consolidation-v1`. Não é status de release.
> O [estado consolidado](consolidation/YOMU_CURRENT_PRODUCT_STATE.md) e a [matriz](consolidation/YOMU_SOURCE_RECONCILIATION_MATRIX.md)
> são as fontes de verdade para integração e níveis de evidência.

- O loopback `127.0.0.1:18777` foi observado sem listener nesta revisão; a readiness UI somente diagnostica API/fila/worker/schema e não inicia túnel nem comprova login Gemini.
- O job `128e8082d6c34aec8af7a832b7e9610f` tem resumo histórico de três `ConnectionError`, mas seus logs/JobStore correlacionados não estavam disponíveis nos caminhos locais consultados. A classe específica da falha segue `NOT_PROVEN`.
- A build onedir de validação mais recente é `C:\Users\henri\YomuExperimental\yomu-main-clean-boundary-20261015\dist\YomuSekai\YomuSekai.exe` (SHA-256 `90DB867E5A5E90BA3CF6AF03165997B0D1F0CB4B2EC5D63A3C7FE6AA13281438`); archive inspection e selftest local de RapidOCR passaram. Ela não comprova UI/licença real, protocolo Comix child frozen, conectividade VPS/Gemini, Discord, nem Clean físico.
- A fronteira Clean agora é comum aos workers seriais/paralelos e a verificação persistida depende de certificado ligado aos hashes e revisão QC; a suite completa passou 6.011 testes. A aceitação física/provider continua pendente.
- A reconciliação de overlays sujos e tips fora da candidata continua incompleta; beta/main update permanecem bloqueados.

## Notas históricas preservadas

Os itens seguintes são registros antigos e não afirmam o status atual de release:

- Incidente histórico de aproximadamente 27 minutos: `NOT_REPRODUCIBLE_EXACT_INPUT_UNAVAILABLE`.
- Grouping/classification possui otimização futura possível, sem explosão no workload local de 35 páginas.
- `review_required` pode ser legítimo mesmo com PDF válido.
- Estado histórico pré-Beta 17: signing/updater/installer, distribuição e clean-VM apareciam
  como pendentes. Não use esta linha como status atual de release.
