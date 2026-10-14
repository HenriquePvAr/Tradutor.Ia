# YomuSekai — estado do snapshot de consolidação

**Snapshot de trabalho:** `integration/yomu-main-consolidation-v1` (local, não publicado).
**Base publicada observada em 2026-10-14:** `origin/main` `121d6285`.
**Código integrado até aqui:** `HEAD` local (consultar `git rev-parse HEAD`; snapshot revisado em 2026-10-14).
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
| Pacote `yomu/`, entrypoints e compatibilidade | `TESTED_FROZEN` para boot e rotas HTTP básicas; não equivale a aceite completo | import/package tests e build frozen do hash exato; licença real continua externa |
| JobStore, modos e progresso | `INTEGRATED`; regressão source passou; smoke de modos/JobStore no EXE não executado | testes de worker/UI/progresso sobre o snapshot final |
| Comix single-pass/materialização | `INTEGRATED`; não reexecutado nesta reconciliação | testes herméticos/frozen; execução física é gate separado |
| OCR, ownership, máscara e Safe Final | `INTEGRATED`; segurança física não inferida | testes focados; resultado físico por arquivo e região |
| Supabase Auth/licença e dispositivo | `INTEGRATED`; guarda adicional de dispositivo/licença revogados/expirados tem teste Deno hermético; nada foi implantado | smoke com sessão licenciada autorizada e implantação controlada da Edge Function; nenhum entitlement é criado |
| Gemini/VPS | `INTEGRATED`; provider físico não chamado | request/geração correlacionada no worker e validação do Safe Final |
| Discord/outbox | `INTEGRATED`; delivery por evento não presumida | receipt correlacionado e ACK individual HTTP 204 |
| PDF/PSD e outputs seletivos | `INTEGRATED`; contratos específicos devem ser testados no snapshot | testes de formato, manifest e reabertura |
| Updater/instalador | `DEFERRED`/`EXTERNAL_GATE` conforme chaves e infraestrutura de release | build assinada, manifest hospedado e smoke limpo; nenhum release nesta tarefa |
| Pesquisas e configuração VPS multi-slot | `DEFERRED`; não promovidas | decisão de produto, revisão de segurança e validação operacional independente |

## Trabalho desta reconciliação

O branch local foi avançado sem alterar os worktrees-fonte: `origin/main` → `main` local → integração `785f9ee`; depois recebeu deltas revisados de backend desktop, assinatura de dispositivo, assets de login, documentação/testes e uma guarda fail-closed para status de dispositivo/licença na Edge Function `device-verify`. O suporte Comix equivalente já existia no pacote. O follow-up de revogação usa somente campos já retornados por `get_device_challenge_for_verify`; teste Deno simulado rejeita dispositivo desconhecido/revogado e licença revogada/expirada/malformada antes de consumir o challenge. Isso não prova deploy nem altera Supabase de produção. Ver a [matriz de proveniência](YOMU_SOURCE_RECONCILIATION_MATRIX.md) e o [plano](YOMU_CONSOLIDATION_PLAN.md).

## Validação executada nesta reconciliação

- Focados Python (desktop, Comix clearance/challenge, SAFE CLEAN, observability e JobStore): `100 passed`; depois do ajuste de assets, `25 passed` nos testes de desktop/build/package.
- Frontend Node: `node --experimental-vm-modules --test <todos os tests/*.mjs>` — `48 passed`, `0 failed`.
- Edge Function: `deno check supabase/functions/device-verify/index.ts` — passou.
- Regressão Python no snapshot atual: `python -m pytest -q` — `6051 passed, 44 skipped, 2 xfailed, 736 subtests passed`, `0 failed` (730,17 s).
- Frontend Node: 48 passed/0 failed; `deno check supabase/functions/device-verify/index.ts`, `node --check docs/status/board/board.js` e `git diff --check` passaram.
- Delta de dispositivo/licença: `deno test --allow-env supabase/functions/device-verify/index_test.ts` — 1 passed; `deno check` e `git diff --check` passam no follow-up. Nenhuma chamada ao Supabase real ou consumo de challenge de produção.
- Focados de UI/progresso/modos/desktop no snapshot: `python -m pytest -q tests/test_loading_view_ui.py tests/test_processing_surface_ui.py tests/test_output_plan.py tests/test_selective_pipeline_execution.py tests/test_translate_only_completion.py tests/test_desktop_app.py` — 163 passed, 15 subtests.
- Frontend completo repetido no snapshot: `node --experimental-vm-modules --test tests/*.mjs` — 48 passed, 0 failed; `node --check docs/status/board/board.js` passou.
- Build isolada PyInstaller 6.22.2: `C:\Users\henri\YomuExperimental\yomu-main-consolidation-v1-v2\dist\YomuSekai\YomuSekai.exe`, SHA-256 `0D16D1238252C4C7CE9F62243E57075C5569D39B8918ED80C7FD8C7C6FC6E32E`. O ambiente `.venv-beta` foi reutilizado sem instalação/atualização global.
- Conteúdo conferido no pacote: `config/public-runtime.json`, `ui/ui_shell.html` e os dois `auth-background*`; URL pública aponta para `ybxwjiycujminbwbdtlb.supabase.co`, há publishable key e não há propriedade service-role nesse JSON. A triagem de segredos é dirigida, não um scanner dedicado de todo o bundle.
- Smoke frozen em porta/perfil isolados: processo servidor veio do EXE com o SHA acima; `/api/health` e `/` responderam HTTP 200. O smoke não fez login, consulta real de licença/entitlement/dispositivo, JobStore job, nem validou todas as rotas/assets de frontend.
- A busca dirigida nos arquivos Git-tracked de runtime/config detectou somente o canário sintético `sb_secret_TEST_ONLY_REDACTION_CANARY_...` em teste de redação; não detectou chave privada, URL real de webhook, JWT atribuído a `SUPABASE_SERVICE_ROLE_KEY` ou token `sb_secret_` fora desse teste. Isso é triagem de padrões, não substitui scanner dedicado nem auditoria do pacote.

## Reconciliação de refs e worktrees

No snapshot atual existem 109 worktrees e 256 refs locais de branch; 66 pontas de branch não são ancestrais do candidato. `git cherry` soma 1.804 commits de patch não equivalentes nas histórias por branch (contagem cumulativa, com histórias compartilhadas), e 9 equivalentes; `git rev-list --all --not HEAD` é a contagem de objetos commit únicos fora de HEAD. Os refs foram preservados. Os deltas de produto/security identificados foram decididos na matriz; entre os 66 heads, 40 branches `research/clean-*` e 5 outras `research/*`, mais experimentos Qwen/Glyph e validações sem holdout independente, ficam `DEFERRED`/`PRESERVE_FOR_LATER`, sem promoção automática. A linha concorrente de progresso/login UI está `SUPERSEDED` para runtime pela UI modular atual (`static/loading_view.js`, `static/processing_surface.js`) e pelos testes `tests/test_loading_view_ui.py`, `tests/test_processing_surface_ui.py`; sua semântica de não inventar progresso deve permanecer coberta. M20 segue sem aceite físico independente.

O inventário externo inicial está em `C:\Users\henri\YomuMainReconciliationBackups\20261014-105033`; ele foi verificado por hash, mas alguns arquivos locais gerados surgiram depois do snapshot (novos índices `.basinfy` e relatórios locais). As fontes continuam intactas; não afirmar que o backup inicial contém esses deltas posteriores. Contagens vivas de status/worktrees prevalecem sobre os totais antigos no inventário. O inventário atual classifica 40 branches `research/clean-*` e 11 outras `research/*` como preservadas/deferred; os experimentos M20 permanecem sem promoção.

## Limites desta afirmação

Este documento registra integração de código e estado de validação, não aceitação final. O smoke frozen prova boot e HTTP básico, não autenticação licenciada nem fluxos completos da UI. Embora os 66 tips tenham sido inventariados e classificados por famílias/decisão, a reconciliação de todos os arquivos modificados dos worktrees sujos e o refresh de backup para os arquivos locais criados após 10:50 continuam pendentes. A build frozen v2 precede apenas este follow-up de Edge Function, que não é código do executável; nenhuma Edge Function foi implantada. Portanto, não declare este branch pronto para atualizar `main`. Nenhum job de capítulo, Gemini, DeepL, consumo YK, alteração de VPS/Supabase, publicação, push ou merge remoto foi realizado nesta tarefa.
