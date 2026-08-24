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
