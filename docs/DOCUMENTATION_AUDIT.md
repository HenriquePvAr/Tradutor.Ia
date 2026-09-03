# Auditoria de Documentação

> **Base auditada:** commit `c81c798`, branch `fix/main-e2e-findings`, worktree limpo
> **Data:** 2026-08-19
> **Escopo:** auditoria completa da documentação existente + estabelecimento da documentação
> viva (técnica, usuário, política, contrato de sincronização). **Nenhuma mudança de
> comportamento de produção.**

---

## 1. Documentos encontrados e classificação

26 arquivos Markdown versionados no início da auditoria.

| Documento | Classificação | Ação |
| --- | --- | --- |
| `README.md` | Parcialmente desatualizado | **Corrigido** (ver §2) |
| `docs/ARCHITECTURE.md` | Parcialmente desatualizado / contraditório | **Corrigido** (ver §2) |
| `docs/WORKER_QUEUE.md` | Atual, incompleto | **Corrigido** (supervisão, estados, retomada) |
| `docs/SUPABASE_AUTH.md` | Contraditório | **Corrigido** (provedor padrão e lista de provedores) |
| `docs/CONFIGURATION.md` | Atual | Mantido |
| `docs/INSTALLATION.md` | Atual | Mantido; agora referenciado como caminho de desenvolvedor |
| `docs/QUALITY_AND_VALIDATION.md` | Atual | Mantido |
| `docs/SECURITY.md` | Atual | Mantido |
| `docs/TESTING.md` | Atual | Mantido |
| `docs/TROUBLESHOOTING.md` | Atual | Mantido |
| `docs/SOURCE_ADAPTERS.md` | Atual | Mantido |
| `docs/UNIVERSAL_CHAPTER_ADAPTER.md` | Atual | Mantido |
| `docs/LOCAL_FOLDER_INPUT.md` | Atual | Mantido |
| `docs/DOWNLOAD_TRANSPORTS.md` | Atual | Mantido |
| `docs/I18N.md` | Atual | Mantido |
| `docs/COMMUNITY_AUTHORIZATION.md` | Atual | Mantido |
| `docs/COMMUNITY_STORAGE.md` | Atual | Mantido |
| `docs/SUPABASE_SOCIAL_BACKEND.md` | Atual | Mantido |
| `docs/SUPABASE_SOCIAL_SCHEMA.md` | Atual | Mantido |
| `docs/SOCIAL_COMMUNITY_UI.md` | Atual | Mantido |
| `docs/EXPLICIT_SOCIAL_PDF_PUBLISHING.md` | Atual | Mantido |
| `docs/SOCIAL_ASSET_RETENTION_RECONCILIATION.md` | Atual | Mantido |
| `docs/BETTER_AUTH_MIGRATION.md` | Atual (preparação, não default) | Mantido |
| `docs/FULL_FUNCTIONAL_AUDIT.md` | Auditoria histórica | **Arquivado por rotulagem** no índice, como registro pontual |
| `SEMANTIC_CLASSIFICATION_AUDIT.md` | Auditoria histórica + referência técnica | Mantido, indexado |
| `scripts/auth-migration/rollback-plan.md` | Plano operacional histórico | Mantido, indexado |

**Nenhum documento foi apagado.** Documentos de auditoria pontual foram preservados e
rotulados como histórico no índice, em vez de removidos.

### Ausências encontradas

- Não existia `CLAUDE.md` nem `AGENTS.md`. **Criado `CLAUDE.md`** na raiz (convenção padrão
  da ferramenta; não havia convenção concorrente no repositório).
- Não existia índice de documentação. **Criado `docs/README.md`.**
- Não existia guia para usuário não técnico. **Criado `docs/user/GUIA_DO_USUARIO.md`.**
- Não existia documento técnico consolidado. **Criado
  `docs/technical/DOCUMENTACAO_TECNICA.md`.**
- Não existia política de manutenção. **Criado `docs/DOCUMENTATION_POLICY.md`.**

## 2. Contradições e afirmações obsoletas corrigidas

| # | Afirmação obsoleta | Onde | Realidade verificada | Evidência |
| --- | --- | --- | --- | --- |
| 1 | "usa por padrão uma API compatível com OpenAI hospedada pela NVIDIA" | `README.md` | O provider padrão é **DeepL** | `ui_helpers.py:34` `DEFAULT_TRANSLATION_PROVIDER = "deepl"`; `translator_nllb.get_translator` |
| 2 | "substitua o valor de `NVIDIA_API_KEY`" no início rápido | `README.md` | O default exige `DEEPL_API_KEY`; login exige Supabase | `.env.example`, `translator_deepl.py` |
| 3 | "Para abrir a interface local: `python app_ui.py`" apresentado como caminho principal | `README.md` | O canônico é `start_tradutor.py` (worker + UI + supervisão). `app_ui.py` sozinho deixa jobs em `queued` | `start_tradutor.py:193-214` |
| 4 | "`ui_bridge.py` mantém uma fila local e executa um item por vez / inicia o subprocesso" | `docs/ARCHITECTURE.md` | A UI **não executa o pipeline**; ela cria jobs no SQLite e o worker independente executa | `worker_service.py`, `job_runner.py`, `docs/WORKER_QUEUE.md` |
| 5 | "Com o default `TRANSLATION_MODE=nvidia`, `translator_nvidia.py` usa..." | `docs/ARCHITECTURE.md` | `TRANSLATION_MODE` é eixo ortogonal; o provider efetivo padrão é DeepL | `translator_nllb.py:88-112`, `config.py:114` |
| 6 | "`local` — sessão de operador em loopback (padrão)" | `docs/SUPABASE_AUTH.md` | O padrão é **`supabase`**; há 4 provedores, não 2 | `community_auth.py:519-542` |
| 7 | Estado terminal listado como `error` | `README.md` | O estado real é `failed`; "erro" é o rótulo da UI | `job_store.py:36`, `static/tradutor_ui.js:94` |
| 8 | "Um job `interrupted`/`resumable` pode ser retomado pela UI" / "use 'Retomar' na UI" | `docs/WORKER_QUEUE.md` | O endpoint existe, mas **nenhum arquivo de `static/` ou `ui/` o chama** | Ausência de `ui/resume` em `static/`, `ui/`; `app_ui.py:1792` |
| 9 | Supervisão do worker ausente da documentação | `docs/WORKER_QUEUE.md` | Implementada em TDD #53 | `worker_supervisor.py` |
| 10 | Taxonomia de estados incompleta (faltavam `staging` e `source_analysis_ready`) | `docs/WORKER_QUEUE.md` | 14 estados em `JobStatus.ALL` | `job_store.py:26-51` |

## 3. Comandos verificados

Executados ou validados contra o commit base:

| Comando | Resultado |
| --- | --- |
| `git rev-parse HEAD` | `c81c798840b2f3e2526d94e205c0d59efa7f8b6b` |
| `python run_webtoon.py --help` | ✅ funciona; flags conferidas uma a uma |
| `python worker_service.py --help` | ✅ `--db`, `--once`, `--status`, `--poll-interval` |
| `python process_launcher.py --help` | ✅ `--runtime-directory`, `--cwd`, `--stdout-path`, `--stderr-path` |
| `python -m pytest --collect-only -q` | ✅ 3714 testes coletados |
| `python -m pytest -q test_worker_supervision.py test_stale_job_reconcile.py test_launcher.py` | ✅ 63 passaram |
| `python -m pytest -q test_worker_process_loss.py` | ✅ 18 passaram |
| `python -m pytest -q test_async_ui_stages.py test_deepl_translation_provider.py` | ✅ 86 passaram |
| `python start_tradutor.py {all,worker,ui,status,stop,stop-worker}` | ✅ conferidos no código (não executados: iniciariam runtime real) |

Comandos inválidos/obsoletos removidos da documentação: nenhum comando inexistente foi
encontrado. O que foi corrigido foi o **enquadramento** de `python app_ui.py` como caminho
principal (item 3 da §2).

### Nota de ambiente

Rodando com o interpretador de um venv em `.runtime/`, quatro testes de crash de processo
falham (`test_worker_process_loss`, `test_worker_supervision`). Causa confirmada: aquele
`python.exe` é um *trampoline* que re-executa o interpretador real, então `Popen.pid` ≠ PID
do filho. Com o Python 3.11 normal os mesmos testes passam. **Não é regressão do
repositório**; registrado na documentação técnica §24.

## 4. Recursos classificados

### Atuais (documentados como disponíveis)

Pipeline ponta a ponta URL→PDF; entrada por pasta local; fila persistente; worker
independente; supervisão limitada do worker; detecção de crash duro por PID+create_time;
reconciliação de jobs órfãos; cancelamento cooperativo; revisão de páginas antes do OCR;
OCR híbrido com fallback seletivo; classificação semântica; DeepL/Riva/Nemotron; Smart
Split; inpainting e redesenho; gates visuais; proveniência de linha de OCR; validação de
completude de origem; PDF nomeado; histórico local; comunidade Supabase + Drive com
publicação explícita; isolamento hermético de testes; guard de rede offline.

### Parciais

- **Retomada de job interrompido** — API e bridge existem, controle na UI não.

### Planejados (documentados como não existentes)

- Instalador para usuário final (`Setup.exe`) — nenhum artefato de empacotamento no índice
  do Git.
- Atualizador assinado — nenhum módulo de manifest, assinatura, SHA ou rollback.
- Licenciamento/expiração de tester — nenhum módulo correspondente.
- Validação em VM Windows limpa — sem evidência.

**Afirmações falsas de disponibilidade nos novos documentos: 0.**

## 5. Capturas de tela

**Capturadas: 0. Fabricadas: 0.**

Motivo registrado honestamente: as telas do produto só existem depois do login em um
ambiente configurado com Supabase, e a missão foi executada com orçamento de rede zero e
sem jobs reais. Capturar exigiria autenticar com uma conta real e subir o runtime de
produção, com risco de expor e-mail, sessão e caminhos locais.

Lacuna documentada em ambos os documentos primários. Diretório `docs/assets/user/` criado e
reservado. Telas previstas quando houver uma conta de demonstração segura: acesso, nova
tradução, origem validada, progresso do pipeline, revisão de páginas, revisão de qualidade,
biblioteca, resultado final.

**Dados sensíveis expostos em imagens: 0** (não há imagens).

## 6. Achados da auditoria (bugs e riscos encontrados, não corrigidos)

Esta missão é somente de documentação. Nenhum bug foi corrigido; todos foram registrados.

| ID | Severidade | Achado | Evidência | TDD futuro recomendado |
| --- | --- | --- | --- | --- |
| `UI-COPY-NAMES-NVIDIA` | Baixa | A mensagem `environment_not_configured` do frontend diz "Configure o arquivo .env e a `NVIDIA_API_KEY`", mas o provider padrão é DeepL. Copy desatualizada visível ao usuário. | `static/tradutor_ui.js`, mapa `reasonMessages` | Mensagem neutra de provider |
| `PROVIDER-HTTP-TELEMETRY-GAP` | Baixa | Não há telemetria HTTP unificada entre providers; cada um mantém suas próprias `stats`. | `translator_deepl.py`, `translator_nvidia.py` | Se a Beta exigir observabilidade de provider |

> **Fechado no TDD #56:** `UI-RESUME-NOT-EXPOSED`. O frontend agora chama
> `POST /api/ui/resume` a partir do painel **Retomar** (`#interruptedJobsPanel`), habilitado
> apenas pela capability `can_resume` derivada de `UiBridge.resume_block_reason()` — a
> recuperabilidade continua sendo decidida pelo backend. `resume()` passou a ser idempotente
> (`already_resumed`) e deixou de recolocar a linha original na fila. Cobertura em
> `test_interrupted_job_resume_ui.py` e `test_interrupted_job_resume_ui.mjs`.
> **Capturas:** não atualizadas — a lacuna de captura autenticada descrita no TDD #54
> permanece; a tela "capítulo interrompido com o botão Retomar" foi adicionada à lista de
> capturas previstas em `docs/user/GUIA_DO_USUARIO.md`.

> **Fechados no TDD #55:** `HERMETIC-SQLITE-URI-GUARD-GAP` (normalização de URI SQLite em
> `hermetic_runtime.sqlite_uri_path`), `UI-HISTORY-REAL-OUTPUT-READ` (`output_root`
> injetável em `UIHistoryStore` + guard de `scandir`/`listdir` sobre `output/` e
> `ui_history.json`) e `STALE-RECONCILE-CLOCK-EQUALITY` (base de tempo única, com prova
> determinística do flake). Cobertura em `test_runtime_isolation_contract.py` e
> `test_stale_job_reconcile.py`.

> A mensagem `UI-COPY-NAMES-NVIDIA` foi **documentada como está** no guia do usuário, com
> nota explicativa. Alterar o texto seria mudança de comportamento de produção, fora do
> escopo desta missão.

## 7. Segurança da documentação

| Verificação | Resultado |
| --- | --- |
| Valores reais de chave de API | 0 |
| Tokens / cookies / headers de autorização | 0 |
| Senhas | 0 |
| Service-role / secret keys | 0 |
| Conteúdo ou caminho absoluto de token do Drive | 0 |
| Caminhos locais com nome real de pessoa | 0 |
| `.env` / `.env.local` lidos por inteiro durante a auditoria | Não — apenas nomes de variável, por busca direcionada |

## 8. Lacunas de documentação remanescentes

1. **Capturas de tela** — nenhuma. Requer conta de demonstração segura (§5).
2. **Instalação para usuário final** — só poderá ser escrita quando o instalador existir.
3. **Updater e licenciamento** — contratos de atualização já definidos na política; o
   conteúdo só existe quando os recursos existirem.
4. **`docs/CONFIGURATION.md`** — descreve as variáveis, mas não foi reescrito nesta missão;
   uma revisão variável-a-variável contra `.env.example` continua sendo trabalho futuro
   útil.
5. **Números da classe Full de performance** (~405,82s vs ~489,73s) — herdados das missões
   de performance e não reproduzíveis a partir do repositório. Registrados como referência
   histórica, explicitamente rotulados.
6. **Exportação PDF/HTML da documentação** — não existe mecanismo no repositório e nenhuma
   dependência foi adicionada para criar um. Markdown continua sendo a fonte de verdade.

## 9. Documentos produzidos

| Caminho | Papel |
| --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | Documentação técnica principal |
| `docs/user/GUIA_DO_USUARIO.md` | Guia do usuário final |
| `docs/README.md` | Índice da documentação |
| `docs/DOCUMENTATION_POLICY.md` | Política de manutenção |
| `docs/DOCUMENTATION_AUDIT.md` | Este relatório |
| `CLAUDE.md` | Contrato permanente de sincronização para agentes |
| `docs/assets/user/` | Diretório reservado para capturas |

Documentos corrigidos: `README.md`, `docs/ARCHITECTURE.md`, `docs/WORKER_QUEUE.md`,
`docs/SUPABASE_AUTH.md`.

### Ajuste de suporte

`.gitignore` ignorava `*.png` globalmente, com exceção apenas para `static/assets/`. Isso
tornaria **impossível versionar** as futuras capturas de tela do guia do usuário. Foram
acrescentadas duas linhas de negação (`!docs/assets/`, `!docs/assets/**`), verificadas com
uma imagem de teste. É uma mudança de configuração de suporte à documentação; **não altera
nenhum comportamento de aplicação**.

---

## 10. Atualizações posteriores da documentação viva

### TDD #57 — Arquitetura de confiança do update assinado (2026-08-20, base `7ab0ea3`)

**Gatilho:** mudança de arquitetura técnica (segurança, empacotamento/updater). Contrato de
sincronização do `CLAUDE.md`, itens 3 e 6.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | §3 mapa de componentes (`update_manifest.py`, `update_installer.py`, `scripts/sign_release.py`); §23 › "Updater assinado" reescrito de **PLANEJADO** para **PARCIAL** com modelo de confiança, diagrama Mermaid, schema do manifest, canonicalização, ordem de verificação, política de versão, extração segura, staging, ativação atômica, rollback, rotação de chave, modelo de ameaça e trabalho restante; §29 dívida técnica reclassificada; §30 e checklist de Beta atualizados; metadados de verificação. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Já classifica "Atualização automática do programa" como **Em desenvolvimento** e responde "Não nesta versão" à pergunta direta. Nenhum recurso de atualização ficou visível ao usuário neste TDD, então dizer qualquer outra coisa seria documentar comportamento planejado como implementado. |
| `README.md` | **Não requer mudança** | Não descreve updater; o status de empacotamento/Beta continua correto. |
| Capturas de tela | **Não requer** | Não existe UI de atualização. Fabricar uma tela seria violação direta da política. |

**Honestidade verificada:** nenhuma afirmação de que o programa se atualiza sozinho foi
introduzida em documento algum. O núcleo está fechado; o produto de atualização, não.

**Novos bloqueadores registrados** (§29): `APP-VERSION-SOURCE-MISSING` (o repositório não tem
versão autoritativa do produto) e `LAUNCHER-SELF-UPDATE-BLOCKER` (um launcher em execução não
pode se sobrescrever com segurança no Windows).

### TDD #58 — Integração remota do updater assinado (2026-08-20, base `c1c6b89`)

**Gatilho:** mudança de arquitetura técnica (segurança, empacotamento/updater). Contrato de
sincronização do `CLAUDE.md`, itens 3 e 6.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | §2 passa o updater assinado para **PARCIAL** com integração remota/launcher; §3 mapa de componentes inclui `app_version.py`, `update_transport.py` e `update_bootstrap.py`; §23 reclassificado para TDD #57/#58, documentando versão canônica, transporte HTTPS, bootstrap de startup, handoff, rollback pós-selftest e limite explícito de self-update; §29 remove dívidas fechadas de versão/transporte e registra `UPDATER-RELEASE-CHANNEL-PENDING` e `LAUNCHER-SELF-UPDATE-DEFERRED`; §30/checklist de Beta atualizados. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Ainda não há canal/chave/UI de produção nem Setup. O usuário final continua não tendo atualização automática disponível nesta versão. |
| `docs/README.md` | **Atualizado** | Status resumido muda de “em desenvolvimento” para “parcial — canal/UI pendentes”. |
| Capturas de tela | **Não requer** | Nenhuma UI foi adicionada. |

**Honestidade verificada:** o produto ainda não promete auto-atualização para usuário final.
O #58 fecha a ponte técnica remota/hermética e mantém a raiz de confiança de produção vazia
até existir chave pública real de release.

### TDD #59 — Regressão real de qualidade em artifact UI Shadow Slave (2026-08-20, base `4d18e92`)

**Gatilho:** regressão de qualidade observada em PDF real gerado pela UI, afetando
story-text, resíduo físico, proper nouns e naturalização. Contrato de sincronização do
`CLAUDE.md`, itens 3, 4 e 6.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | §2 registra gates de qualidade reforçados; §13 documenta ancestry contínua de páginas lógicas/Smart Split; §16 documenta limpeza de texto claro aberto sobre arte/fumaça e resíduo OCR não atribuído grudado a story; §17 documenta denominador semântico story-text vs crédito/promo/SFX, texto de sistema, nomes declarados, fragmentos OCR e naturalização PT-BR; checklist final marca qualidade fechada novamente pelo TDD #59. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Nenhuma UI mudou. `review_required` já é estado terminal conhecido; a alteração é contrato interno de qualidade/fail-closed. |
| `docs/README.md` | **Atualizado** | Estado resumido de qualidade passa a citar reforço story-text/resíduo físico. |
| Capturas de tela | **Não requer** | Não houve alteração visual de UI nem nova tela. |

**Honestidade verificada:** o PDF histórico continua evidência forense e não foi editado.
A correção é sistêmica para próximas gerações; nenhum provider, job real ou rede foi
consumido neste TDD.

### TDD #62 — Story review → clean render closure offline (2026-08-20, base `baede2c5`)

**Gatilho:** achados persistidos do E2E #60 indicavam 24 regiões story preservadas no PDF
real e um resíduo físico extra não atribuído. Contrato de sincronização do `CLAUDE.md`,
itens 3, 4 e 6.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | §2 registra que os contratos estão implementados, mas a validação real limpa ainda está pendente; §17 documenta story authority para limpeza source-scoped, reparos estreitos pré-validação e o estado honesto do #62. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Documenta que a elegibilidade de tradução segue autoridade semântica de story text e que os reparos locais estreitos não substituem o validator nem provider. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Nenhuma UI mudou. `review_required` continua com o mesmo significado para o usuário; a alteração é interna e fail-closed. |
| `docs/README.md` | **Atualizado** | O resumo de status deixa de sugerir qualidade completamente fechada pelo #59 e registra pendência de validação real limpa após o #62. |
| Capturas de tela | **Não requer** | Não houve alteração visual de UI nem nova tela. |

**Honestidade verificada:** o artefato real #60 não foi reescrito nem promovido a limpo. O
TDD #62 só fecha contratos locais offline; a qualidade do produto permanece dependente de
novo E2E real controlado.

### TDD #64 — Pós-#63: identidade de artefato e fechamento offline de resíduos (2026-08-20, base `6ee5cd8`)

**Gatilho:** o E2E real #63 gerou PDF fisicamente útil, mas o job terminou `failed`
porque o comando reconstruído após source selection escreveu em `output/<run_id_slug>` em
vez do `output/<chapter_slug>/<run_id>` persistido no SQLite. O mesmo artefato ainda
registrou resíduos físicos/review em story text.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Registra o contrato canônico `chapter_slug/run_id`, preservação de `run_id` no `run_manifest.json` e o fechamento offline pós-#63 sem promover qualidade real. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Documenta que OCR suspeito longo pode ser roteado ao tradutor com evidência, mantendo validadores/render gates como autoridade final. |
| `docs/README.md` e `README.md` | **Atualizados** | Status passa de “até #62” para “até #64”, mantendo validação real limpa pendente. |
| Capturas de tela | **Não requer** | Não houve alteração de UI; somente contratos internos e testes offline. |

**Honestidade verificada:** os PDFs reais #60 e #63 permaneceram byte-for-byte intactos.
O TDD #64 fecha causas locais offline; a qualidade do produto continua aberta até novo E2E
real controlado.

### TDD #66 — Contabilidade de render e ownership físico residual offline (2026-08-20, base `6eadfeb`)

**Gatilho:** o E2E real #65 provou que binding de runtime, provider e artifact estavam
corretos, mas ainda terminou `review_required` com 15 resíduos físicos. A missão #66 foi
perícia/TDD offline: auditar os resíduos persistidos, fechar contabilidade local e impedir
que story text com candidato válido desapareça do plano de render sem motivo estruturado.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | §17 documenta `render_plan_accounting`, desfechos obrigatórios de story text, razão estruturada para render skipped e ownership de linhas OCR filhas via `cleanup_lines`/`cleanup_line_boxes`; checklist de Beta passa de #64 para #66 sem promover qualidade real. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Registra a separação entre render limpo, revisão estruturada e ausência de desfecho; documenta que os 15 resíduos do #65 foram auditados offline e que PDFs históricos permanecem intactos. |
| `README.md` e `docs/README.md` | **Atualizados** | Status resumido passa a citar correções offline/forenses até o TDD #66, mantendo novo E2E real limpo como pendência. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Nenhuma UI ou semântica visível mudou. `review_required` segue sendo o mesmo terminal para usuário. |
| Capturas de tela | **Não requer** | A mudança é de contrato interno/relatório, sem tela nova. |

**Honestidade verificada:** os artefatos reais #60/#63/#65 não foram reescritos nem
promovidos a limpos. O #66 fecha caminhos locais offline e melhora a explicabilidade do
gate; a qualidade do produto permanece **OPEN — REAL POST-#66 E2E REQUIRED**.

### TDD #67 — Real post-#66 quality closure E2E (2026-08-20, base `c7795dd`)

**Gatilho:** execução real única autorizada pela UI visível para provar se os contratos
offline do #66 fechavam qualidade de produto em Shadow Slave chapter 1.5.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Registra que o #67 preservou runtime/binding/manifest, mas continuou `review_required` com resíduos físicos; tabela de status inicial e checklist deixam de sugerir que falta apenas executar E2E limpo. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Acrescenta o resultado real #67: `physical_gate_passed=false`, 15 resíduos físicos persistentes e `QUALITY` ainda aberto. |
| `README.md` e `docs/README.md` | **Atualizados** | Status resumido passa a dizer que o #67 real foi executado e falhou o fechamento de qualidade. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | O fluxo de usuário não mudou; `review_required` segue como terminal conhecido. |
| Capturas de tela | **Não requer** | A evidência está nos artefatos e relatórios locais; nenhuma tela nova foi introduzida. |

**Honestidade verificada no fechamento #67:** o #67 criou exatamente um job real, sem
rerun, Community ou Drive. Na leitura inicial, os resíduos pareciam reprovar o caminho
pós-#66.

### TDD #68 — Production-path parity guard / `OFFLINE-PRODUCTION-PARITY-001` (2026-08-20, base `feb4a38`)

**Gatilho:** a perícia local dos artefatos #67 mostrou que o `run_manifest.json` físico
declara `commit_hash=c7795dd`, enquanto a missão atual parte de `feb4a38` preservando o
commit docs-only. Portanto o zero-delta #65 → #67 não prova que as correções offline #66
falharam no caminho de produção; prova que o artifact foi produzido por runtime/runner
stale.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Documenta o guard de paridade `job.commit_hash` ↔ `run_manifest.commit_hash` no `job_runner.py` e a falha `pipeline_commit_mismatch`. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Registra `OFFLINE-PRODUCTION-PARITY-001` como blocker Beta e reclassifica #67 como evidência de runtime stale, não como prova de qualidade pós-#66. |
| `docs/DOCUMENTATION_AUDIT.md` | **Atualizado** | Este registro. |
| `README.md` e `docs/README.md` | **Atualizados** | Status resumido passa a exigir novo E2E real pós-guard. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | O comportamento visível de usuário não mudou; a alteração é de fail-closed runtime/diagnóstico. |
| Capturas de tela | **Não requer** | Evidência é textual nos manifests e testes herméticos. |

**Honestidade #68:** nenhum job real, provider, rede, Supabase, Community ou Drive foi
acionado. A qualidade do produto permanece **OPEN — REAL POST-#68 E2E REQUIRED**.

### TDD #69 — First valid real post-#66/#68 quality E2E (2026-08-20, base `3ded288`)

**Gatilho:** após o guard #68, era necessário executar um único E2E real de Shadow Slave
chapter 1.5 com provenance autoritativa (`CURRENT_HEAD == job.commit_hash ==
run_manifest.commit_hash`) antes de concluir qualquer coisa sobre qualidade pós-#66.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Registra que #69 validou runtime/binding pós-guard, mas manteve qualidade aberta por resíduos ordinary-story. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Acrescenta os números do #69: 104 regiões físicas esperadas, 91 traduzidas, 13 retidas para revisão, 14 resíduos físicos e `physical_gate_passed=false`. |
| `docs/DOCUMENTATION_AUDIT.md` | **Atualizado** | Este registro. |
| `README.md` e `docs/README.md` | **Atualizados** | Status resumido deixa de pedir “novo E2E pós-guard” e passa a refletir que ele rodou, mas qualidade continua aberta. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | O comportamento visível de usuário não mudou; a alteração é evidência de validação/qualidade. |
| Capturas de tela | **Não requer** | A evidência canônica está no DB, manifests, PDF, quality report e audit visual local. |

**Honestidade #69:** houve exatamente um job real e um runner, sem rerun, sem Community,
sem Drive e sem push. A execução foi válida para provenance, mas o produto não está pronto:
o PDF permaneceu `review_required` e a auditoria visual confirmou story text comum em
inglês, incluindo sentinelas p002, p005, p006, p025, p030, p044, p062 e p068.

### TDD #70 — P68 geometry-first physical proof subgate (2026-08-20, base `4d182fb`)

**Gatilho:** o #69 provou que p068 ainda continha `IT'LL` visualmente, mas a evidência
estruturada lia a linha filha como `77,!!`. Isso mostrou que OCR pós-render não pode ser a
única prova de remoção física.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Registra ownership de cleanup para narração aberta, `source_owned_geometry_coverage`, perdão PT-BR curto provenance-bound, roteamento de frase story OCR-suspeita, white/dark-patch guards calibrados, split de outlier story/SFX, area gate por máscara efetiva e passiva preservada no fidelity gate. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Explica que máscara parcial continua review mesmo quando OCR vira ruído, que OCR é sinal secundário, e que p015/p062/p025/p030/p002/p044-like têm contratos locais estreitos. |
| `docs/DOCUMENTATION_AUDIT.md` | **Atualizado** | Este registro. |
| `README.md` e `docs/README.md` | **Não requer mudança** | O status macro permanece qualidade aberta aguardando E2E pós-#70 completo. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Sem alteração visível de fluxo de usuário. |
| Capturas de tela | **Não requer** | A mudança é contrato hermético de máscara/geometria, coberto por testes offline. |

**Honestidade #70 parcial:** este registro fecha os subgates locais P68, p015 (`SÓ` lido
como `SO`), p062 (speech longo compactado por OCR), P005-like (story text degradado mas
translatável quando há autoridade story e `main_text_score` suficiente), p025-like (fundo
claro comprovado não vira falso white-patch e tem linha owned completamente mascarada),
p030-like (seed curto/SFX destacado é separado do bloco story), P006-like (`decorative` em
`textured_art` só é traduzido e aceito pelo `source_scoped` quando a frase comum é forte e
limpa, e caption claro aberto não vira falso white-patch), STAGGER-like (palavra única em
falsa caixa clara é preservada como SFX), além do
blocker p002-like em que o
source-scoped area gate precisa medir a máscara efetiva, não a caixa fonte inteira, e em
que fundo saturado mas uniformemente escuro não vira falso dark-blotch; p044-like em que
passiva preservada não deve ser roteada como `state_action_changed`. Ele não classifica o
TDD #70 completo como A/B e não autoriza por si só fechamento de qualidade; os demais
rejected/validated-not-rendered de #69 permanecem para auditoria ou E2E controlado
posterior conforme orçamento explícito.

**Auditoria #71:** o ledger #69 foi reconciliado offline/read-only como evidência canônica:
os 14 resíduos físicos, 13 structured-review e os motivos `translation_not_rendered`,
`translation_not_selected`, `untranslated_source_after_retries` e
`semantic_fidelity_failed_after_retries` têm IDs explícitos e sem unexplained local. A matriz
atual fecha os roots ordinários com candidato real persistido e conserva P68 como prova de
geometria/máscara, não OCR-string-only. A classificação documental esperada é **B**, porque
P005/P062 não têm candidato DeepL real no #69; o downstream está provado com sintético, mas a
qualidade real dessas traduções exige o próximo provider run autorizado.

### TDD #72 — Real post-#71 quality E2E ledger (2026-08-24, base `0b40ca2`)

**Gatilho:** após o #71, foi executado um E2E real autorizado para substituir o #69 como
evidência de qualidade do código corrente, com job/manifest/HEAD todos em
`0b40ca23f0d734a345b8a559bf5934f80c01123e`.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado no #73** | Registra o #72 como evidência real atual e delimita os dois blockers finais ordinários. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado no #73** | Registra os números do #72: 104 esperadas, 99 traduzidas/renderizadas, 5 retidas, 6 resíduos físicos, P005/P006/P062 verdes e P063/P068 abertos. |
| `docs/DOCUMENTATION_AUDIT.md` | **Atualizado** | Este registro. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | O comportamento visível de usuário não mudou; a alteração é evidência de qualidade/runtime. |
| Capturas de tela | **Não requer** | Evidência canônica está nos artefatos persistidos #72, quality report e inspeção visual local. |

**Honestidade #72:** houve exatamente um job real; não houve rerun nem push. A qualidade
permaneceu aberta porque `p063:BALAO_1` ficou em inglês e `p068:LINE_004` deixou `IT'LL`
visível no PDF.

### TDD #73 — Final two ordinary-story blockers offline closure (2026-08-24, base `0b40ca2`)

**Gatilho:** o #72 reduziu a falha real a dois blockers ordinários: falso positivo de
validação em P063 e perda de ownership físico em P068. A missão exigiu zero job, zero
provider e uso exclusivo dos artefatos persistidos #72.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Documenta o root P063 (`repeated_translation_fragment` amplo demais), o root P68 (attachment antes de background classification) e o subgate `ordinary_story_physical_residual_*`. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Registra que #73 fecha os roots offline, preserva fail-closed global e exige E2E real futuro para provar artifact novo. |
| `docs/DOCUMENTATION_AUDIT.md` | **Atualizado** | Este registro. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Nenhuma mudança de UX/review-state foi introduzida. |
| Capturas de tela | **Não requer** | O fechamento é TDD/perícia offline sobre artefatos e fixtures. |

**Honestidade #73:** nenhum job real, provider, rede, Supabase, Community ou Drive foi
acionado. PDFs #69/#72 e artefatos históricos permanecem imutáveis. A qualidade de produto
continua **OPEN — REAL POST-#73 VALIDATION REQUIRED**.

### TDD #74/#75 — P068 final ordinary-story root closure offline (2026-08-24, base `da3bd10`)

**Gatilho:** o E2E real #74 foi executado uma única vez pela UI visível e validou o commit
`da3bd1033609973fc55659f6e59fffbdfce7dd38` no job, manifest e HEAD. P063 fechou no caminho
real, mas P068 permaneceu visível em inglês porque `p068:BALAO_2` não foi traduzido/renderizado.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Registra o root P068 pós-#74: child cleanup-only derrubava o score OCR do parent e bloqueava provider routing antes de qualquer candidato real. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Registra que #74 fecha P063 real, mantém P068 como único residual ordinário e que #75 fecha routing/render/cleanup apenas offline/sintético por falta de candidato DeepL real. |
| `docs/DOCUMENTATION_AUDIT.md` | **Atualizado** | Este registro. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Nenhuma mudança de UX foi introduzida. |
| Capturas de tela | **Não requer** | A evidência está nos artefatos #74, no PDF persistido e nos testes offline. |

**Honestidade #75:** nenhum job real, runner, provider, Vortex, Supabase, Community, Drive ou
push foi usado. O #74 não tinha candidato DeepL persistido para `p068:BALAO_2`; por isso o
fechamento local é de routing/render/cleanup, não de qualidade real do provider. PDFs #72/#74
permanecem imutáveis e a qualidade de produto continua **OPEN — REAL POST-#75 E2E REQUIRED**.

### TDD #76 — Real post-#75 final quality proof (2026-08-24, base `594f7f0`)

**Gatilho:** o #75 fechou P068 offline, mas faltava provar o caminho real provider → render →
cleanup para `p068:BALAO_2`.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `README.md` | **Atualizado** | Estado atual passa a registrar que a qualidade Beta de story text está fechada pelo E2E real #76. |
| `docs/README.md` | **Atualizado** | Tabela de status muda o item de qualidade para `QUALITY CLOSED — REAL POST-#75 E2E VALIDATED`. |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Registra os números canônicos do #76, o fechamento real de P068 e a preservação fail-closed dos quatro resíduos não-story. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Registra provenance, P068 real provider, render-plan, gate ordinário zero e limite de `review_required` não-story. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Nenhuma mudança de UX foi introduzida. |
| Capturas de tela | **Não requer** | Evidência canônica está no DB, manifests, PDF, quality report, pages finais e auditoria visual local. |

**Resultado #76:** houve exatamente um job real pela UI visível, sem rerun e sem hotfix:
job `3979b3f3482b41fc8ce481a7050b4c8e`, run
`05a77bb8-487c-46a6-98cd-c1f23ff7e233`, DeepL `quality_optimized`, RapidOCR, `force=true`,
`use_cache=false`, 35 source items e 72 páginas finais. O PDF novo ficou com SHA256
`415056E61F24A2AE8CCB923BE58119A911BF98C4A6C7322CF84F953C083897B4`.

**Honestidade #76:** o job terminou `review_required`, mas o subgate ordinário fechou:
`ordinary_story_physical_residual_count=0`, P068 passou com candidato DeepL real e os quatro
resíduos físicos restantes (`p011:BALAO_1`, `p011:BALAO_2`, `p013:BALAO_3`, `p015:BALAO_8`)
foram reconciliados como SFX/OCR ambíguo não-story. Community, Drive, Supabase remoto, push e
segundo job não foram usados.

### TDD #77 — Scan Beta tester licensing foundation (2026-08-24, base `113b0da`)

**Gatilho:** com a qualidade Beta de story text fechada no #76, o próximo bloqueio para
Scan Beta externa passou a ser autorização de tester antes de Setup/VM limpa.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `README.md` | **Atualizado** | Estado atual registra licenciamento apenas como fundação local/offline e mantém integração Supabase/Setup como pendentes. |
| `docs/README.md` | **Atualizado** | Tabela de status separa fundação local de integração remota. |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Documenta autenticação vs autorização, estados, expiração UTC, device model, fail-closed, gate de job, RLS/migration local e limites da fase. |
| `docs/QUALITY_AND_VALIDATION.md` | **Não requer mudança estrutural** | O #77 não reabre qualidade de tradução; apenas mantém o status do #76. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Ainda não há UX final de licença para tester externo. |
| Capturas de tela | **Não requer** | A missão foi 100% local/offline e sem tela nova obrigatória. |

**Resultado #77:** foi criada a fundação local fail-closed de licenciamento em
`beta_license.py`, com estado canônico, expiração/revogação, limite de dispositivos,
revogação por dispositivo, fingerprint minimizado e teste de concorrência do último slot.
`UiBridge.start()` e `UiBridge.resume()` agora têm fronteira de autorização antes de criar
novos jobs protegidos; o runner valida o metadado seguro como defesa em profundidade.

**Honestidade #77:** não houve job real, DeepL, Vortex, Supabase remoto, Community, Drive,
publicação de update, Setup, push ou mutation remota. A migration
`20260824120000_beta_tester_licensing_foundation.sql` é contrato local para a próxima fase e
não foi aplicada remotamente.

### TDD #78 — Controlled Supabase Beta license integration (2026-08-24, base `ed22fd6`)

**Gatilho:** #77 fechou a fundação local/offline; faltava provar schema remoto, RPC atômica,
RLS/grants e negação autenticada antes de qualquer grant real.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `README.md` | **Atualizado** | Estado atual passa a registrar schema/RLS/RPC remotos aplicados e primeiro tester ainda pendente. |
| `docs/README.md` | **Atualizado** | Tabela de status diferencia integração remota concluída de grant real pendente. |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Documenta migrations remotas, RPC, `auth.uid()`, server time, locking, grants e smoke negativo. |
| `docs/QUALITY_AND_VALIDATION.md` | **Não requer mudança** | Licenciamento não reabre qualidade de tradução. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Ainda não há UX final de licença para tester externo. |
| Capturas de tela | **Não requer** | Evidência canônica está nos metadados remotos e testes locais. |

**Resultado #78:** no projeto Supabase `mimrsxnhqbqkffsekxuw` (`Tradutor IA Community`) foram
aplicadas apenas migrations de licensing:

- `20260824192515 beta_tester_licensing_foundation`
- `20260824192601 beta_tester_authorization_rpc`
- `20260824192729 beta_tester_grants_hardening`

As tabelas `beta_tester_entitlements`, `beta_tester_devices` e
`beta_tester_license_events` existem com RLS ligada. A RPC
`public.authorize_beta_tester_device(text,text)` é `SECURITY DEFINER`, fixa
`search_path = pg_catalog, public`, usa `auth.uid()`, tempo do servidor e `FOR UPDATE` no
entitlement para serializar alocação de device. `anon` não executa a RPC; `authenticated`
executa a RPC e tem somente `SELECT` bruto protegido por RLS.

**Honestidade #78:** não foi criado entitlement real, device administrativo, tester,
Community/Drive/update/Setup/job de tradução/provider/push. Smokes remotos: sem auth →
`AUTH_REQUIRED`; contexto JWT simulado no banco sem entitlement → `NOT_ENTITLED`,
`allowed=false`. Não houve extração de JWT/cookie/localStorage do navegador, portanto o smoke
via sessão autenticada real do produto fica como primeiro passo antes do grant controlado.
Contagem final remota: entitlements `0`, devices `0`, events `0`.

### TDD #79 — Source flow UX + Vortex Chapter 2 acceptance (2026-08-24, base `1b16278`)

**Gatilho:** a fonte real `https://vortexscans.org/series/shadow-slave/chapter-2` expõe
reader válido mesmo com área de lista de capítulos vazia/enganosa. A UX também exigia a
sequência manual `Validar origem → Iniciar`, ruim para Beta.

| Documento | Ação | Motivo |
| --- | --- | --- |
| `README.md` | **Atualizado** | Roadmap separa story-text fechado de reconstrução visual, qualidade semântica e leitor interno ainda abertos. |
| `docs/README.md` | **Atualizado** | Tabela de status explicita os tracks #80/#81/#82. |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Documenta start com análise automática, estados de erro e contrato do relatório de fonte. |
| `docs/SOURCE_ADAPTERS.md` | **Atualizado** | Registra que Vortex usa reader como autoridade e não lista de capítulos. |
| `docs/user/GUIA_DO_USUARIO.md` | **Atualizado** | Guia passa a orientar `colar URL → Iniciar tradução`. |
| `docs/QUALITY_AND_VALIDATION.md` | **Não requer mudança estrutural** | O #79 não executa tradução nem altera gates de qualidade. |

**Resultado #79:** o botão manual “Validar origem” sai do happy path. O clique em
Iniciar executa análise segura de fonte, mostra “Analisando a fonte...”, persiste um
resultado sanitizado e submete o start com a análise recém-gerada. Chamadas diretas de URL
sem análise compatível continuam falhando antes de job. Fonte incompatível exige consentimento
explícito para registrar um relatório local metadata-only; duplicatas por URL normalizada +
motivo são idempotentes.

**Roadmap pós-#79:** story-text coverage continua fechado, mas `ART-RECON-001`
(texto fantasma/contraste ruim em região texturizada), `ART-RECON-002` (patch claro/plano
sobre textura), `TRANSLATION-SEMANTIC-001` (português semântico/natural ruim) e leitor PDF
integrado permanecem abertos para #80/#81/#82.

### TDD #80 — Confiabilidade de finalização, UI de pipeline e idempotência de start (2026-08-24, base `a132533`)

**Gatilho:** uso manual real após o #79 expôs três defeitos de aplicação — não de qualidade
de tradução. O Chapter 2 concluiu mas só apareceu em "Capítulos traduzidos" minutos depois;
uma tela azul de prontidão cobria a interface normal; cliques rápidos em Iniciar produziam
várias submissões com a resposta "esse processo já está na fila".

**Perícia (somente leitura, sem rerun).** Job `9d213d4b…`, run `da736270-…`,
`review_required` / `quality_review_required`, 42 itens de fonte aceitos (43 candidatos, 1
rejeitado) → Smart Split → 84 páginas lógicas → 84 páginas de PDF. A linha do tempo em disco:

| Evento | Instante |
| --- | --- |
| PDF gravado (13.395.572 bytes) | 20:04:25.68 |
| `run_manifest.json` | 20:04:29.84 |
| `job_manifest.json` | 20:04:31.08 |
| Linha do job terminal, com `pdf_path` e `manifest_path` vinculados | 20:04:31.077 |

| Defeito | Estado |
| --- | --- |
| `FINALIZATION-RACE-001` | **NÃO registrado — não comprovado.** Os artefatos foram publicados *antes* do estado terminal; a ordem estava correta. |
| `HISTORY-REVISION-CROSS-PROCESS-001` | Registrado. `history_revision` era contador em memória do processo da UI; a transição terminal é escrita pelo worker, em outro processo, então o sinal de refresh nunca se movia. |
| `PIPELINE-UI-REGRESSION-001` | Registrado. `renderBootstrapSurface()` repintava a tela de prontidão em `#loadingSurface`, dentro da coluna de "Nova tradução", a cada `refreshBootstrap()`. |
| `START-SPAM-001` | Registrado. `_pending_duplicate()` lê a fila e só depois cria o job, sem nada segurando o intervalo (TOCTOU). |

| Documento | Ação | Motivo |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | §6 ganha camadas de proteção do start e comportamento da tela de prontidão/Pipeline; §10 documenta `SCHEMA_VERSION = 10` e os índices únicos parciais; §18 ganha o contrato de atualização do Histórico e a ordem de finalização comprovada. |
| `docs/DOCUMENTATION_AUDIT.md` | **Atualizado** | Este registro. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | Nenhuma descrição de comportamento visível estava errada: o guia não prometia atualização imediata do histórico nem descrevia a tela de prontidão. |
| `docs/QUALITY_AND_VALIDATION.md` | **Não requer mudança estrutural** | O #80 não executa tradução nem altera gates de qualidade. |

**Resultado #80:** o sinal de refresh do Histórico passa a somar uma parcela derivada do
banco (`JobStore.terminal_revision`), tornando-o cross-process; `closeBoot()` trava
`bootHasClosed` e nenhum refresh posterior repinta a tela de prontidão sobre a aplicação; e
o start ganha single-flight no cliente mais o índice único parcial
`uq_jobs_active_owner_chapter`, que é a garantia real contra submissão simultânea. O painel
Pipeline compacto e as etapas de produção já existiam e foram preservados — estavam sendo
deslocados, não removidos.

**Honestidade #80:** nenhum job real, nenhuma chamada de provider, nenhuma mutação remota,
nenhum push. O run do Chapter 2 não foi reexecutado nem alterado; toda a perícia foi leitura
do banco local e do diretório de saída. `review_required` permanece um estado **com**
artefato completo — o PDF do Chapter 2 existe e está vinculado.

**Roadmap pós-#80:** `ART-RECON-001`/`ART-RECON-002` (#81),
`TRANSLATION-SEMANTIC-001` (#82) e leitor PDF integrado (#83) seguem abertos.

### TDD #81 — Qualidade de reconstrução de arte (2026-08-24, base `f43abf4`)

| Documento | Classificação | Ação |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Seção 16 ganha “Segurança de preenchimento plano”, “Detector de patch plano” e “Fallback estruturado”; a tabela de status move Reconstrução visual de arte de **ABERTO** para **REVISÃO**. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Nova subseção separando story-text coverage (CLOSED no #76) de art reconstruction (`clean`/`review` por região) e listando os contadores novos. |
| `docs/CONFIGURATION.md` | **Atualizado** | Documenta as chaves novas de flatness/patch plano. |
| `docs/user/*` | **Não requer mudança** | Nenhum estado de revisão visível ao usuário mudou de nome ou de significado. |

**Perícia #81 (somente leitura sobre artefato real de 72 páginas):**

- **Página 25** (`BY THE NIGHTMARE SPELL` → `PELO FEITIÇO DO PESADELO`): o run persistido
  registra `uniform_light_line_pixels: 127863` — os três quadriláteros de linha OCR — e
  `visual_validation_passed: true` com `source_text_coverage: 1.0`. O fundo é fumaça
  texturizada, mas *suave*: `local_texture_mean 0.373`, `edge_density 0.0`, o que fez a
  classificação coarse marcar `uniform_light`/`strict_uniform_light` e autorizar
  preenchimento de cor única. Medido no replay offline: 127863 px preenchidos com **1 cor,
  desvio padrão 0.0**, sobre arte com desvio 69.7. O spread de luminância do anel local
  mede **42.0** contra o limite de 30 — a evidência que faltava.
- **Página 5** (`NOT THE CHEAP SYNTHETIC STUFF...`): o run persistido registra
  `uncovered_source_text_pixels: 110` com maior componente de 53 px — o ghost do lettering —
  e ainda assim `source_text_coverage: 0.998` foi tratado como pass. Medido nos mesmos
  pixels, a máscara antiga deixa 161 px de origem descobertos; depois do #81 deixa 13.

**Resultado #81:** os defeitos são sistêmicos e as correções também: nenhum ramo de
produção referencia página, capítulo ou frase. Página e texto aparecem apenas em testes e
perícia. Arte muito estruturada, quando a reconstrução não pode ser provada segura, passa a
cair em revisão estruturada em vez de receber retângulo destrutivo.

**Honestidade #81:** nenhum job real, nenhum provider, nenhuma rede, nenhuma mutação
remota, nenhum push. O artefato real de 72 páginas foi lido, nunca reescrito; todo o replay
de reconstrução foi para diretório temporário. A reconstrução por inpainting não é
generativa: ela suaviza, e por isso o gate de patch plano exige near-uniformidade absoluta
em vez de exigir que a textura seja reproduzida.

### TDD #82 — Fidelidade semântica e PT-BR natural (2026-08-24, base `29c8e95`)

| Documento | Classificação | Ação |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Nova subseção na auditoria linguística separando confiança na fonte OCR, fidelidade de significado e naturalidade; a tabela de status move Qualidade semântica/natural PT-BR de **ABERTO** para **REVISÃO**. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | Nova seção “Fidelidade semântica e PT-BR natural” com a tabela de severidades, as âncoras de significado, o contrato de contexto de tradução e a contabilidade nova; `TRANSLATION-SEMANTIC-001` deixa de ser sentinela planejada e `ART-SEAM-DETECTOR-001` entra explicitamente como pendente. |
| `docs/CONFIGURATION.md` | **Não requer mudança** | Nenhuma chave de configuração nova. |
| `docs/user/*` | **Não requer mudança** | Nenhum estado visível ao usuário mudou de nome ou de significado. |

**Perícia #82 (somente leitura sobre `quality_report.json` reais; 542 regiões story
distintas, aceitas e renderizadas):**

- **P068 / página 68 `REGION_002`** — OCR: `TAKEAFEWHOURS FORTHENEAREST AWAKENEDTO GET
  HERE.`; candidato aceito como `ok`, sem retry: `LEVE ALGUMAS HORAS PARA CHEGAR AQUI,
  DEPOIS QUE ACORDAR.` A classe de pessoas ("the nearest Awakened") virou um evento
  ("depois que [alguém] acordar"). Raiz primária: `PROVIDER_SEMANTIC_ERROR` sobre fonte
  aglutinada por OCR. Passou no gate antigo porque nenhum número, nome do ledger ou negação
  se moveu — `AWAKENED` estava colado em `AWAKENEDTO` e por isso nunca casou com o ledger de
  terminologia. Pós-fix: `verify` / `temporal_relation_changed:introduced` — a única região
  das 542 em que a regra dispara. Sem candidato alternativo persistido.
- **Página 43 `REGION_001`** — OCR leu `SLUM` como `SLLM`; o token sobreviveu literalmente:
  `UM RATO DO SLLM`. Raiz: `OCR_SOURCE_CORRUPTION`. O grupo já carregava
  `short_improbable_caps_token`, mas essa evidência nunca chegava à aceitação da tradução.
  Pós-fix: `review` / `source_ocr_suspicious:SLLM` — renderiza, nunca é contado como limpo.
  Sem candidato alternativo persistido.
- **Páginas 42 (`COLLD`), 46 (`VALLT`), 25 (`IHNH`), 18 (`ARTBUSUNG`,
  `ADAPTATIONTTOMIN`, `RANKTANK`)** — mesma classe, mesmo desfecho.
- **Página 65 `REGION_002`** (`AGATETHROUGHWHICH` → `A GÁGATA`) permanece **não detectada**:
  a palavra inventada tem forma portuguesa plausível e o repositório não tem léxico PT-BR.
  Sinalizar aglutinação em bloco custaria 126 das 542 regiões em revisão, quase todas
  traduzidas corretamente — foi medido e rejeitado.

**Resultado #82:** 531 de 542 regiões seguem limpas, 8 vão para revisão semântica e 3
bloqueiam. Nenhum ramo de produção referencia página, capítulo, região ou frase; os literais
persistidos aparecem apenas em `test_semantic_meaning_and_ptbr_quality.py`. Cobertura de
story-text continua **CLOSED**: nenhuma severidade nova retém a origem em inglês na página.

**Honestidade #82:** nenhum job real, nenhum provider, nenhuma rede, nenhuma mutação remota,
Supabase, Community ou Drive, nenhum push. Os artefatos reais foram lidos, nunca reescritos.
Modalidade não foi implementada: as regras testadas produziram apenas falsos positivos.
`ART-SEAM-DETECTOR-001` continua **pendente** e deliberadamente fora de #82.


### TDD #83 — Leitor de capítulos embutido (2026-08-25, base `bc43c66`)

| Documento | Classificação | Ação |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Nova subseção "Leitor de capítulos embutido (TDD #83)" em §18: motor de renderização sem dependência nova, vínculo execução↔artefato, tabela de vetores de segurança do serviço de PDF, ciclo de render, modelo de estado e corridas, zoom/ajuste, teclado, tela cheia e garantia de somente leitura. |
| `docs/user/GUIA_DO_USUARIO.md` | **Atualizado** | §7 ganha a área **Leitor** e o atalho passa de "1 a 8" para "1 a 9"; §16 ganha "Lendo um capítulo dentro do programa" com a tabela de controles, os atalhos de teclado, o comportamento em capítulos com revisão necessária e a mensagem de arquivo indisponível. |
| `docs/QUALITY_AND_VALIDATION.md` | **Não requer mudança** | Nenhum gate, severidade ou contagem de qualidade mudou. `ART-SEAM-DETECTOR-001` continua **pendente**. |
| `docs/CONFIGURATION.md` | **Não requer mudança** | Nenhuma variável de ambiente nova. |
| `docs/SECURITY.md` | **Não requer mudança** | O leitor não cria fronteira de confiança nova: reutiliza `_owned_ui_job`, a mesma prova de posse em SQL das demais rotas privadas de job. |

**Dependência:** nenhuma. `pdf.py` já grava todo capítulo com Pillow como PDF de
imagem por página (`/DCTDecode`), então `pdf_reader.py` devolve o JPEG que já está
dentro do artefato sem recodificar e sem motor de PDF no navegador. Nada de CDN,
nada de script remoto, nenhuma entrada nova em `requirements*.txt`. Um PDF fora
dessa forma cai para o visualizador nativo do navegador (`mode: "embed"`), estado
que é reportado em vez de disfarçado.

**Perícia #83 (somente leitura sobre os 14 PDFs reais em `output/`):** todos os 14
foram analisados com sucesso pelo parser (42 a 99 páginas). O capítulo 2 real
(84 páginas, 13,4 MB) analisa em 14,5 ms; a primeira página sai em 0,36 ms e as
seguintes em ~0,2 ms, porque cada página é lida por offset e nenhuma é decodificada
para navegar. Vinte miniaturas custam 309 ms no total, com pico de 4,6 MB de heap.

**Smoke local (runtime isolado, identidade `local_test`, cópia do capítulo 2 real):**
histórico abre o leitor pela ação primária **LER**; 84 páginas, ajuste à largura
por padrão; 84 miniaturas criadas mas apenas 6 carregadas; um único `<img>` no palco;
limites de página, salto por digitação, lixo digitado, zoom 25–400 %, 100 %, ajuste
à largura/página, teclado (`←`/`→`/`Home`/`End`) e supressão de atalho dentro do campo
de página verificados ao vivo. Abrir a execução A e imediatamente a B deixa apenas a B
(42 páginas) — nenhuma página da A sobrevive. Execução desconhecida produz erro contido
com três ações e sem stack trace. Travessia (`..%2F..%2F.env`, `..%5C..%5CWindows`),
caminho absoluto do cliente e outro artefato via rota de PDF: todos 404. Página válida:
`200 image/jpeg` com `nosniff`; fallback: `206 application/pdf` com `Accept-Ranges`.
Nenhuma URL ou log do leitor carrega token, credencial ou caminho de arquivo.

**Imutabilidade:** SHA-256 do capítulo 2 antes e depois de todo o exercício —
`eb14c98d19313c8ecfa7b9639f1b5960c82a37dfda9081f2d166f988b5bb12ea`, inalterado,
`mtime` inalterado, nenhum arquivo novo na pasta da execução.

**Honestidade #83:** nenhum job real, nenhum provider, nenhuma rede de provider, nenhuma
mutação remota, Supabase, Community, Drive ou publicação de update, nenhum push. Não foi
possível capturar uma imagem de tela neste ambiente (o painel do navegador não compõe
quadros); a verificação visual foi feita por asserções de geometria, DOM e rede sobre a
aplicação real em execução, e isso está declarado em vez de fabricado. `ART-SEAM-DETECTOR-001`
continua **pendente** e deliberadamente fora de #83.

### TDD #84 — Detector de costura de reconstrução (2026-08-25, base `b768f84`)

| Documento | Classificação | Ação |
| --- | --- | --- |
| `docs/technical/DOCUMENTACAO_TECNICA.md` | **Atualizado** | Nova subseção "Detector de costura (ART-SEAM-DETECTOR-001)" em §16: os três sinais de borda relativos à reconstrução, a supressão do sinal de textura em contexto plano, a regra de corroboração e a calibração medida. |
| `docs/QUALITY_AND_VALIDATION.md` | **Atualizado** | §"Cobertura de story text ≠ qualidade de reconstrução" ganha a terceira leitura (segurança ≠ fidelidade); a dívida `ART-SEAM-DETECTOR-001` passa de **pendente** para **fechado offline**, com a pendência de provedor real declarada. |
| `docs/CONFIGURATION.md` | **Atualizado** | Sete variáveis novas de calibração de costura. |
| `docs/user/GUIA_DO_USUARIO.md` | **Não requer mudança** | O comportamento visível de revisão não mudou: uma costura roteia para a mesma revisão estruturada de reconstrução que já existia. |
| `docs/SECURITY.md` | **Não requer mudança** | Nenhuma fronteira de confiança nova; o detector é aritmética local sobre pixels já carregados. |

**Calibração (offline, medida — não estimada):** retângulo destrutivo real da página 25
`seam_score` 1,0; bloco chapado em gradiente 1,0; halo de inpaint 3,49; patch texturizado
1,09. Controle negativo que motivou a regra de corroboração: legenda plana de dois tons
com preenchimento levemente diferente do vizinho, `seam_score` 0,26 — um único sinal
raspando o limite, **mantida aceita**. Balão plano, gradiente contínuo e contorno de
origem cruzando a máscara não produzem sinal algum.

**Corpus semântico #82 rerodado (somente leitura sobre as execuções persistidas):** 460
pares (origem, candidato) distintos — 452 limpos, 7 em revisão por
`source_ocr_suspicious`, 1 em `verify` por `temporal_relation_changed`. As duas sentinelas
reproduzem exatamente: o candidato P068 continua **não aceito limpo** e o
`SLLM` / "rato do slim" continua roteado para revisão. Bare "depois" (advérbio) segue
passando. O baseline de 542 regiões do #82 **não é reproduzível** a partir do que ficou
persistido — o critério de seleção daquele corpus não virou script —, então o que se
compara é a proporção: 8/460 (1,7 %) contra 11/542 (2,0 %). Nenhuma explosão.

**Fase D — E2E real tentado, falha de ambiente (classificação F).** Um único job real foi
submetido pela UI visível contra o commit funcional `f575536`, com orçamento explícito de
1 job: `job_id 0348997189ef40b1a10f0fb73beaf479`, `run_id
089ab5ed-87e7-42b1-be7a-57d20f5daf41`, `mode: quality`, `use_cache: false`, `force: true`,
escopo completo. Proveniência confere por SHA completo — runtime HEAD, `JOB.commit_hash` e
`job_manifest.commit_hash` são todos `f575536101ce0478710b7b1a014a0691a2e41b22`.

A fonte resolveu normalmente (Vortex disponível, 35/35 páginas, adapter `vortexscans` v2).
O pipeline abortou **um segundo depois de iniciar**, antes de OCR, tradução, reconstrução
ou PDF:

```
run_webtoon.py _configure_mode -> ocr_engine.require_available_engine
OCREngineUnavailableError: engine=paddle disponivel=false motivo=dependency_unavailable
```

Estado do ambiente: `OCR_ENGINE='paddle'`, `OCR_FALLBACK_ENGINE='paddle'`,
`RAPIDOCR_ENABLED=False`, com `paddleocr` **ausente** e `rapidocr_onnxruntime`
**instalado**. Ou seja, o motor exigido pela configuração não tem dependência instalada,
enquanto o RapidOCR — o motor de produção que esta missão pretendia exercitar — está
presente mas desligado por configuração. O guard falhou fechado corretamente: o runtime
recusou operar com motor indisponível em vez de degradar em silêncio.

Consequências registradas sem maquiagem: **nenhuma chamada ao DeepL** (zero ocorrências no
log do runner), nenhum PDF novo, nenhum artefato além do `job_manifest.json`. Portanto
**não existe evidência de provedor real em #84**. `ART-SEAM-DETECTOR-001` permanece fechado
*offline* e sem validação em execução real; `TRANSLATION-SEMANTIC-001` continua dependente
de provedor real. As auditorias das Fases E–H (P068, SLLM, páginas 5 e 25, costuras, 72
páginas, leitor) **não foram executadas** por ausência de artefato — não por terem passado.

Sem rerun e sem hotfix, conforme a política da missão: o orçamento de 1 job real foi
consumido e a falha é de ambiente, não do código de #84.

**Imutabilidade e disciplina:** os 8 PDFs históricos do capítulo mantêm SHA-256 idêntico ao
baseline anterior à execução; a fila ganhou exatamente 1 job (49 → 50), `attempt = 1`,
nenhum cancel, nenhum segundo Start. Nenhuma mutação remota, Supabase, Community, Drive ou
publicação de update, nenhum push.

**Prontidão para Setup.exe: NÃO** — por ausência de evidência real, não por defeito
provado. O gate de qualidade real continua aberto até que o ambiente rode o pipeline com
o motor de OCR de produção.

#### Fase D (2ª tentativa, após #84R) — E2E real concluído, classificação D

Com `TRADUTOR_OCR_ENGINE_OVERRIDE` corrigido em `.env` por #84R e o modo **fast**
(RapidOCR puro, sem qualquer caminho Paddle), um segundo job real — orçamento explícito
de 1 — rodou até o fim: `job_id 8b1d02f9ecc644599c4540ef45289e65`, `run_id
7d64890b-e303-497b-863f-74e2cd8d5645`, 35 imagens → **72 páginas lógicas**, 97 grupos,
6min 16s, término `review_required`. Proveniência por SHA completo: runtime HEAD,
`JOB.commit_hash` e `run_manifest.commit_hash` todos
`f944ace1392bfe19169b1938cff63b1c2f59a3be`. PDF
`a5943041f85487398ffd07be43b54d0e2607b9d8acf52baf8fd6d9d823ea7092`, 9 976 982 bytes.

**Nota de configuração:** o modo foi `fast`, **não** `quality_optimized` como a missão #84
previa. `run_webtoon.py::_configure_mode()` deriva o motor do **modo de submissão**
(`fast→rapidocr`, `quality→paddle`), nunca de `OCR_ENGINE`; e mesmo em `quality` restavam
fallbacks regionais para Paddle, ausente nesta máquina. DeepL permaneceu o provider de
tradução.

**O que passou, com evidência real:**

* **Página 25** — o retângulo chapado destrutivo **não voltou**. A fumaça texturizada está
  íntegra e a região foi traduzida e renderizada (`PELO FEITIÇO DO PESADELO`), com
  `uniform_light_line_rejected: true`. É o fecho de #81 provado em saída real.
* **Detector de costura** — `seam_suspected: 0` em 97 reconstruções aceitas: nenhum falso
  positivo em produção real, e nenhum patch chapado (`flat_patch: 0`). Nenhuma costura
  óbvia foi classificada como limpa.
* **P068** — o DeepL devolveu **exatamente** o candidato historicamente errado
  (`LEVE ALGUMAS HORAS ... DEPOIS QUE ACORDAR`) e o gate de #82 **rejeitou**
  (`semantic_fidelity_failed_after_retries`). O significado inventado não passou.
* Proveniência, binding, imutabilidade (8 PDFs históricos com SHA-256 inalterado), hash do
  novo PDF inalterado pelo leitor, 1 job, `attempt = 1`, zero rerun, zero hotfix.

**O que reprovou — `SEMANTIC-RUNTIME-001` (novo, bloqueador):**

O validador semântico offline classifica `THAT HAS NOTHING TO DO WITH A SLLM RAT LIKE ME.`
→ `ISSO NÃO TEM NADA A VER COM UM RATO DO SLLM COMO EU.` como
`review / source_ocr_suspicious / ('SLLM',)`. No runtime real a região saiu como
`translation_final_state: translated`, `translation_final_reason: 'ok'`,
`translation_valid: true`, `translation_quality_impact: **none**`, contabilizada entre as
97 traduzidas. O `semantic_review_reason` **é** gravado
(`source_ocr_suspicious:SLLM`), mas não produz impacto de qualidade nem roteia a região
para revisão. Mesma falha em mais duas regiões: página 42 (`COLLD`) e página 46 (`VALLT`).

Ou seja: a severidade `review` de #82 existe no validador e **não chega à aceitação** do
candidato no runtime. O usuário vê português sem sentido contabilizado como limpo — o caso
que a missão nomeia explicitamente como inaceitável. Isso é **classificação D** e é
exatamente o tipo de defeito que só um E2E real expõe: a suíte offline de #82 continua
verde.

**Outros defeitos reais registrados (sem correção, por política):**

* **Texto de história em inglês no PDF final (3 regiões).** Página 5 e página 6 têm
  candidato PT-BR **bom**, descartado por `translation_not_rendered_after_validation`
  porque a reconstrução de arte reprovou (`large_white_patch_on_nonwhite_background`);
  página 68 é o P068. As demais 49 regiões com inglês preservado são SFX, URL, créditos e
  promo — política estabelecida, não defeito.
* **Ghost de origem na página 6**: o contorno branco do lettering original sobreviveu à
  limpeza e aparece atrás do português, com a região ainda reportada
  `art_reconstruction_status: clean`. Não é costura — o detector está correto no seu
  contrato — é `ART-RECON-001` (sub-máscara/halo) ainda aberto.
* **Leitor #83**: `GET /api/ui/reader/<job>` devolveu **401** nesta sessão. Reproduz
  igualmente em capítulo **histórico**, logo é condição de sessão/ambiente após o reinício
  do runtime, **não** regressão do artefato novo nem de #84 — o parser do #83 abre o PDF
  novo normalmente (72 páginas). Registrado como smoke não concluído, não como sucesso.
  **Correção de leitura (#84F8):** o 401 **não** era condição transitória de sessão nem de
  ambiente. É defeito estrutural e permanente: `static/chapter_reader.js` nunca enviava o
  Bearer que o provider real da beta exige, e `<img src>`/`<iframe src>` não conseguem
  enviá-lo. Fechado offline como `READER-SESSION-001` — ver `QUALITY_AND_VALIDATION.md`.

**Prontidão para Setup.exe: NÃO.** `SEMANTIC-RUNTIME-001` é bloqueador visível ao usuário e
precisa ser fechado antes do empacotamento.

#### TDD #84F1 — `SEMANTIC-RUNTIME-001` fechado offline

Missão de reparo funcional, **sem** job real, provedor, rede, mutação remota, Community,
Drive ou publicação de update. Toda a evidência veio dos artefatos persistidos de #84, lidos
em modo somente leitura (`job 8b1d02f9ecc644599c4540ef45289e65`,
`run 7d64890b-e303-497b-863f-74e2cd8d5645`).

**Auditoria das regiões afetadas.** O `progress.json` do run tem exatamente **três** itens
com `semantic_review_reason` não vazio, e os três terminaram
`translated / reason ok / valid true / quality_impact none / redrawn true`:

| Região | Token | Motivo gravado |
| --- | --- | --- |
| p42 `REGION_001` | `COLLD` | `source_ocr_suspicious:COLLD` |
| p43 `REGION_001` | `SLLM` | `source_ocr_suspicious:SLLM` |
| p46 `REGION_002` | `VALLT` | `source_ocr_suspicious:VALLT` |

Nenhuma outra região do run apresenta o padrão. **Inexplicadas: 0.** As três compartilham a
mesma classe de fiação, então um contrato de caminho de produção parametrizado cobre as três.

**Primeira divergência.** `ocr_balloon._fidelity_reason_for()` classifica corretamente o
candidato como `REVIEW`, grava `group.semantic_review_reason` e **retorna string vazia** —
por política de render deliberada. Quem consome esse retorno
(`validate_and_retry_translations`) só enxerga "sem motivo de rejeição", marca
`translation_valid = True` e chama `_set_translation_terminal_state(group, "translated")`,
que derivava `translation_quality_impact` **apenas do estado terminal**. A severidade
morria ali: `semantic_review_reason` seguia adiante como metadado decorativo, sem impacto de
qualidade, sem roteamento para revisão e contado como render limpo.

**Correção.** Detalhe técnico em
[DOCUMENTACAO_TECNICA.md](technical/DOCUMENTACAO_TECNICA.md). Em resumo: o impacto de
qualidade passa a ser derivado do veredito semântico dentro do **único** escritor de estado
terminal; a política de render fica explícita (`REVIEW` + `RENDER_WITH_REVIEW`, `REJECT` não
renderiza); a contabilidade ganha baldes canônicos exclusivos e passa a exigir
`review_required` no capítulo; e o plano de render deixa de contar a região como limpa. O
validador de #82 **não** foi enfraquecido, nada foi especializado por texto/página, e o
controle P068 continua rejeitado.

**Testes.** RED capturado antes da correção em `test_semantic_runtime_acceptance.py`
(10 falhas, incluindo os três sentinelas com `quality_impact none`), verde depois. Gates
completos: `python -m pytest` 4102 passaram / 34 pulados; `python -m unittest discover`
3502 OK; as 14 suítes `.mjs` verdes com `node --experimental-vm-modules` — confirmando que
as falhas JS relatadas antes eram de invocação, não defeito de produto; `pip check`,
`py_compile` e `git diff --check` limpos.

**Prontidão para Setup.exe: NÃO.** `SEMANTIC-RUNTIME-001` está fechado offline, mas
`ART-RECON-001` (halo/sub-máscara na página 6) e a saída de story visível ao usuário nas
páginas 5/6 continuam abertos. Próxima missão: **TDD #84F2 — `ART-RECON-001` + saída de
story P5/P6**, e só depois um novo E2E real.

### TDD #84F2 — ART-RECON-001 local hardening (2026-08-25, base `e5faeaa`)

**Classificação documental:** contrato local **fechado**, saída final P5/P6 real **pendente
de E2E**. A missão separou cinco eixos que estavam acoplados no caminho visual:
validade da tradução, remoção do lettering de origem, segurança da reconstrução da arte,
fidelidade visual e disposição final de render.

**Evidência real usada somente em leitura:** o run
`output/shadow_slave_chapter_1_5/7d64890b-e303-497b-863f-74e2cd8d5645` mostrou P5
`REGION_002` com candidato PT-BR persistido (`NÃO É A PORCARIA SINTÉTICA BARATA...`) retido
por `large_white_patch_on_nonwhite_background`. A família P6 motivou o contrato de outline
/ halo residual: lettering de origem não pode sobreviver e ainda ser contado como arte
limpa.

**Correção local:** `ocr_balloon.source_lettering_footprint()` modela corpo, outline,
halo/antialias e sombra pertencente ao lettering dentro da evidência OCR da própria região.
`residual_source_lettering_metrics()` mede o que sobrevive antes de desenhar o português.
`art_reconstruction_verdict()` agora torna `art_clean` impossível quando há residual físico
ou fidelidade incerta. `render_disposition()` centraliza `render_clean`,
`render_with_review` e `do_not_render`, preservando a política de #84F1:
`SEMANTIC REVIEW` pode renderizar com revisão, `SEMANTIC REJECT` não renderiza como aceito.

**Raiz exata provada (replay offline com paridade de produção).** As regiões reais foram
reprocessadas a partir das páginas e da geometria OCR persistidas do run
`7d64890b-e303-497b-863f-74e2cd8d5645`, sem provider e sem rede, reproduzindo os números do
relatório original (P6 `REGION_002`: máscara 54 492 px e 23 927 px de branco novo, idênticos
ao persistido). A raiz é **uma só** e explica os dois defeitos:

* O lettering de P5/P6 é **glifo escuro com contorno branco grosso**. A máscara de limpeza
  cobria o corpo do glifo e apenas parte do contorno. O contorno branco sobrevivente ficava
  na borda da máscara e **alimentava o Telea**, que repintava o interior das letras com a
  cor do contorno.
* Consequência A (P5 `REGION_002`, P6 `REGION_002`): o resultado era uma silhueta branca das
  letras, `large_white_patch_on_nonwhite_background` disparava corretamente, todas as cinco
  estratégias eram recusadas e o candidato PT-BR bom era descartado com
  `translation_not_rendered_after_validation` — inglês na página final.
* Consequência B (P6 `REGION_001`, o ghost de `ART-RECON-001`): ali o render era aceito com
  `source_owned_geometry_coverage: 0.993`, porque a cobertura era medida contra o **corpo**
  do glifo. O contorno não entrava no denominador, e o OCR pós-render não lê contorno sem
  corpo, então a região saía `art_reconstruction_status: clean` com o contorno branco de
  `SINCE IT COST ME EVERYTHING I HAD LEFT...` legível atrás do português.

O guard de patch branco e o detector de costura estavam **corretos** nos dois casos: o
defeito era o footprint da limpeza, a montante deles. Nenhum dos dois foi enfraquecido.

**Eixo de fidelidade (separado da segurança).** Reconstrução segura e reconstrução fiel são
coisas diferentes. `MIN_ART_FIDELITY_TEXTURE_RATIO` (0.55) marca `art_fidelity_uncertain`
quando a textura reconstruída fica muito abaixo da arte ao redor, sem alterar nenhum
veredito de segurança — o bound destrutivo (`MAX_FLAT_PATCH_TEXTURE_RATIO`, 0.25) continua
onde estava. Nas regiões reais isso separa com folga: P5 0.37 e P6 0.29 caem em revisão,
enquanto P25 2.34 e P6 `REGION_001` 1.40 permanecem `clean`. Uma região que renderiza sob
revisão passa a carregar `translation_quality_impact: review_required` e
`manual_review_required`, de modo que `render_with_review` com qualidade `none` é
impossível e a região continua contabilizada na revisão estruturada.

**Replay offline de produção — resultado visualmente inspecionado:**

| Região | #84 real | Depois (replay offline) |
| --- | --- | --- |
| P5 `REGION_002` | inglês completo, sem PT-BR | `NÃO É A PORCARIA SINTÉTICA BARATA...` renderizado; inglês ausente; sem ghost, sem retângulo, sem costura; `art: review/art_reconstruction_fidelity_uncertain`; `render_with_review` |
| P6 `REGION_002` | inglês completo, sem PT-BR | `É MELHOR QUE VALHA A PENA.` renderizado; inglês ausente; sem contorno/halo; `art: review`; `render_with_review` |
| P6 `REGION_001` | PT-BR **com contorno branco de origem visível**, reportado `clean` | contorno **ausente**; `art: clean`; `render_clean` |
| P25 `REGION_001` | traduzido, arte íntegra | **pixel a pixel idêntico**; `art: clean`; `render_clean` |

A arte reconstruída de P5/P6 fica visivelmente mais suave que o original (o prédio de P5
perde definição) — é exatamente por isso que o veredito é `review` e não `clean`.

**Limite importante:** não houve job real, provider, DeepL, Vortex, rede, Supabase,
Community, Drive ou publicação; o artefato #84 foi lido e nunca reescrito. O replay é
offline com paridade de produção, não um E2E: o fechamento de produto continua aguardando um
E2E real dedicado.

### TDD #84F6R — source analysis preflight recovery (2026-08-25, base `b5cdad2`)

**Classificação documental:** contrato offline fechado; E2E real final ainda pendente. A missão
não executou job real, provider, rede externa, Vortex real, Webtoons real, Supabase, Community,
Drive ou push.

**Escopo fechado.** O roteamento de fonte foi preso por contrato hermético: URL VortexScans
suportada seleciona `VortexScansAdapter`, não chama `canonicalize_webtoons_url` e não depende de
Webtoons. URL Webtoons continua selecionando o adapter Webtoons e seu canonicalizer próprio.
Domínio desconhecido continua no fluxo unsupported/fallback controlado.

**Falha pré-job observável.** Se a análise de fonte falha antes de criar job, o sistema mantém
zero job, zero run e zero histórico fantasma, devolve erro recuperável para a UI, registra log
sanitizado com estágio, host seguro e classe de falha, libera o lock do botão Start e não
reintroduz a superfície azul de processamento no fluxo Beta normal.

**OCR quality.** `quality` / `quality_optimized` agora é RapidOCR-primário. Paddle permanece
fallback opcional; ausência de Paddle não bloqueia o modo qualidade quando RapidOCR está
disponível. Ausência do OCR primário configurado continua falhando fechado. O guard de #84F5
continua no caminho de produção e rejeita fallback vazio que apagaria uma leitura útil do
RapidOCR.

**Status de produto.** `P068-RECOVERY-001` está fechado offline, mas a saída final visível ao
usuário segue **OPEN / pendente de novo E2E real autorizado**. `READY FOR SETUP.EXE: NO`.

### TDD #84F15 — desambiguação de sentido contextual (2026-08-26, base `64b563e`)

**Classificação documental:** contrato offline fechado; E2E real final ainda pendente. A missão
não executou job real, provider, DeepL, rede, Supabase remoto, Community, Drive ou push.

**Classe fechada.** Origem limpa + português fluente + sentido errado da palavra + contabilizado
como `semantic_clean`. Sentinela real: `p046:BALAO_1`, `PRECINCT 7` → `7º DISTRITO ELEITORAL`,
persistido em `.cache/processed` com `translation_valid: true`, `translation_validation_reason:
'ok'`, `semantic_review_reason: ''` e `manual_review_required: false`. A leitura de OCR estava
correta (`raw_text == clean_text == "PRECINCT 7"`, confiança 0.93, `OCR_SUFFICIENT`), o
português é bem formado e todos os invariantes de #82 são satisfeitos — número preservado,
sem nome próprio, sem negação, sem relação temporal. **Primeira divergência:** a escolha de
sentido pelo provedor; nenhuma camada olhava para o sentido.

**Raiz.** `evaluate_local_fidelity()` só via a região isolada. A única "contexto" que existia
(`_fidelity_context()`) é do adjudicador remoto — linhas de personagem e glossário — e nunca
chega à camada local. Sem contexto, um candidato fluente que troca o sentido de um substantivo
ambíguo é indistinguível de uma tradução correta.

**Correção.** `semantic_fidelity.word_sense_conflicts()` + `context_texts` em
`evaluate_local_fidelity()`, alimentado por `TextGroup.page_context_texts` (as demais regiões
da mesma página, montado uma vez em `validate_and_retry_translations`). Contrato de três
evidências obrigatórias, descrito em
[QUALITY_AND_VALIDATION.md](QUALITY_AND_VALIDATION.md) e em
[DOCUMENTACAO_TECNICA.md](technical/DOCUMENTACAO_TECNICA.md). Nenhum
literal de capítulo, nenhuma condição por página, nenhum ramo por termo: a tabela é o dado, e
o teste `test_the_rule_is_the_table_and_nothing_else` prova que remover a entrada apaga o achado.

**Evidência de contexto real.** A página 46 não contém `police` nem `officer`. O que existe é
`EMERGENCY CONTAINMENT VAULT` na região vizinha — domínio de detenção/segurança. É essa
evidência persistida, e não um token inventado, que sustenta o sentido policial e derruba o
eleitoral.

**Falsos positivos, medidos.** Varredura de todas as regiões traduzidas persistidas do run real
de #84F9: a regra marca **uma** região, `p046:BALAO_1`. O detector amplo de balão curto que
marcaria quatro **não** foi promovido.

**Contabilidade.** `word_sense_context_mismatch` entra em `REVIEW_ONLY_FIDELITY_REASON_CODES` e
em `UNUSABLE_REVIEW_REASON_CODES`: renderiza, conta como `semantic_review_unusable`, nunca como
`semantic_clean`, e zera `setup_ready`. O modelo de #84F14 (`clean` / `review_renderable` /
`review_unusable` / `reject`) fica intacto.

**Status de produto.** `TRANSLATION-SEMANTIC-PRECINCT-001` fechado offline; a qualidade semântica
visível ao usuário fica **CLOSED OFFLINE / pendente de E2E real**, e a saída final de história
visível ao usuário segue **OPEN / pendente de novo E2E real autorizado**. `READY FOR SETUP.EXE: NO`.

### TDD #84F17 — recuperação de review semântico inutilizável (2026-08-26, base `9c38c3a`)

**O que faltava.** #84F14 e #84F15 fecharam a **detecção** de duas classes de saída ruim, e
pararam aí. Uma região marcada `review_unusable` era publicada como estava: nunca pedia uma
segunda tradução, nem quando a própria origem era o defeito. `SEMANTIC-RECOVERY-001` fecha o
caminho `candidato ruim → detecta → recuperação limitada → candidato bom se houver → senão
REVIEW_UNUSABLE`, sem nunca produzir `candidato ruim → CLEAN` nem retries infinitos.

**Duas classes, dois caminhos.** `source_ocr_suspicious` recebe reparo de origem *antes* do
retry — `unique_source_repair()` só corrige quando o vocabulário da cena tem **exatamente uma**
palavra a uma edição de distância; ambiguidade, nome próprio, termo protegido, SFX e palavra
já conhecida ficam intocados. `word_sense_context_mismatch` recebe a restrição
`preserve_word_sense` mais as linhas de origem vizinhas como contexto descritivo, pelo campo
`context` do DeepL (uso documentado) e por `contexto_da_cena` no NVIDIA — nunca uma instrução
nomeando a resposta.

**Proveniência.** `group.text` continua sendo o OCR bruto; a forma canônica e sua evidência
vivem em `canonical_source_text` / `source_repairs`. Só o retry vê a forma canônica, então o
detector nunca é silenciado pelo reparo.

**Limite.** Máximo de **2 chamadas de tradução por região** (1 inicial + 1 retry seletivo), teto
de capítulo `ceil(N/8)` inalterado. Nada foi empilhado: terminologia, OCR e sentido disputam o
mesmo orçamento que já existia.

**Replay offline do run real de #84F9** (zero chamadas a provedor): `p042` (`COLLD`) tem reparo
único provado por evidência genérica (`COLLD` → `COULD`); `p043` (`SLLM`) e `p046` (`VALLT`) não
têm candidata única em vocabulário nenhum e **permanecem `REVIEW_UNUSABLE`** — nenhum mapeamento
literal foi codificado para forçá-los; `p046:BALAO_1` (`PRECINCT`) obtém o retry de sentido com
o contexto policial da cena. Recuperação que falha volta ao veredito da detecção: renderiza sob
review, nunca vira `clean`, e não é convertida em rejeição.

**Status de produto.** `SEMANTIC-RECOVERY-001` fechado offline. Detecção semântica: **CLOSED
OFFLINE**. Recuperação semântica: **CLOSED OFFLINE**. Leitor contínuo (#84F16): **CLOSED**. A
saída final de história visível ao usuário segue **OPEN / pendente de novo E2E real
autorizado**. `READY FOR SETUP.EXE: NO`.
