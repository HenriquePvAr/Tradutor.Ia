# YomuSekai — estado do snapshot de consolidação

**Snapshot de trabalho:** `integration/yomu-main-consolidation-v1` (local, não publicado).
**Base publicada observada em 2026-10-14:** `origin/main` `121d6285`.
**Código integrado até aqui:** `785f9ee` + `c8ec9b1` + `578a578` (`HEAD` deve ser lido do Git antes de integrar).
**Não é uma release nem prova de aceitação física.**

## Arquitetura canônica

- `yomu/` é o runtime modular canônico neste snapshot. Os entrypoints legados na raiz são compatibilidade; não criar uma segunda implementação de pipeline, provider, máscara, artefato ou worker.
- A UI/bridge cria e acompanha jobs persistidos; o JobStore e os workers são a autoridade para progresso e estado terminal. Eventos de UI não substituem o estado persistido.
- O plano de saída governa operações e artefatos. Clean, tradução, PDF/PSD e revisão são capacidades distintas; não inferir que um arquivo existente foi validado ou que todo modo produz os mesmos formatos.
- Supabase permanece a autoridade de autenticação/licença/dados remotos. Fail-closed continua obrigatório; nenhuma chave administrativa pode ser empacotada.
- Gemini SAFE CLEAN e Discord possuem contratos e testes offline/integrados no código, mas health, mocks e relatórios antigos não provam uma geração física ou entrega específica.
- A configuração de duas slots/VPS, notebooks, datasets, bakeoffs e labs são experimentais/deferred; a presença no repositório não as habilita em produção.

## Estado e evidência

| Domínio | Estado neste snapshot | Evidência exigida para elevar o estado |
| --- | --- | --- |
| Pacote `yomu/`, entrypoints e compatibilidade | `INTEGRATED`; regressão final ainda pendente | import/package tests e build frozen do hash exato |
| JobStore, modos e progresso | `INTEGRATED`; validação final pendente | testes de worker/UI/progresso sobre o snapshot final |
| Comix single-pass/materialização | `INTEGRATED`; não reexecutado nesta reconciliação | testes herméticos/frozen; execução física é gate separado |
| OCR, ownership, máscara e Safe Final | `INTEGRATED`; segurança física não inferida | testes focados; resultado físico por arquivo e região |
| Supabase Auth/licença e dispositivo | `INTEGRATED`; identidade/licença real não consultada nesta tarefa | smoke com sessão licenciada autorizada; nenhum entitlement é criado |
| Gemini/VPS | `INTEGRATED`; provider físico não chamado | request/geração correlacionada no worker e validação do Safe Final |
| Discord/outbox | `INTEGRATED`; delivery por evento não presumida | receipt correlacionado e ACK individual HTTP 204 |
| PDF/PSD e outputs seletivos | `INTEGRATED`; contratos específicos devem ser testados no snapshot | testes de formato, manifest e reabertura |
| Updater/instalador | `DEFERRED`/`EXTERNAL_GATE` conforme chaves e infraestrutura de release | build assinada, manifest hospedado e smoke limpo; nenhum release nesta tarefa |
| Pesquisas e configuração VPS multi-slot | `DEFERRED`; não promovidas | decisão de produto, revisão de segurança e validação operacional independente |

## Trabalho desta reconciliação

O branch local foi avançado sem alterar os worktrees-fonte: `origin/main` → `main` local → integração `785f9ee`, e foram aplicadas as correções revisadas de backend desktop preexistente e tipos do `device-verify`. O suporte de Cloudflare/Comix, assets de login e código principal do pacote já existiam na integração e não foram duplicados. Ver a [matriz de proveniência](YOMU_SOURCE_RECONCILIATION_MATRIX.md) e o [plano](YOMU_CONSOLIDATION_PLAN.md).

## Validação executada nesta reconciliação

- Focados Python (desktop, Comix clearance/challenge, SAFE CLEAN, observability e JobStore): `100 passed`.
- Frontend Node: `node --experimental-vm-modules --test <todos os tests/*.mjs>` — `48 passed`, `0 failed`.
- Edge Function: `deno check supabase/functions/device-verify/index.ts` — passou.
- Regressão Python: `python -m pytest -q` — `6050 passed, 44 skipped, 2 xfailed, 736 subtests passed`, `0 failed` (739,96 s).
- Board JS `node --check`, integridade de links/alvos documentais e `git diff --check` passaram.
- PyInstaller não está disponível no Python instalado e não foi encontrado ambiente virtual de build nos worktrees/builds inspecionados; nenhum EXE foi gerado. Nenhum scan de segredos dedicado foi encontrado no PATH; a validação de ausência de segredos privilegiados no pacote final fica pendente porque não houve pacote frozen.
- A busca dirigida nos arquivos Git-tracked de runtime/config detectou somente o canário sintético `sb_secret_TEST_ONLY_REDACTION_CANARY_...` em teste de redação; não detectou chave privada, URL real de webhook, JWT atribuído a `SUPABASE_SERVICE_ROLE_KEY` ou token `sb_secret_` fora desse teste. Isso é triagem de padrões, não substitui scanner dedicado nem auditoria do pacote.

## Limites desta afirmação

Este documento registra integração de código e estado de validação. A regressão source passou, mas sem build frozen, verificação de package contents/segredos e smoke físico, o snapshot não está pronto para atualizar `main` nem para beta. Nenhum job de capítulo, Gemini, DeepL, consumo YK, alteração de VPS/Supabase, publicação, push ou merge remoto foi realizado nesta tarefa.
