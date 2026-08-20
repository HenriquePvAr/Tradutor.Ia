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
| `UI-RESUME-NOT-EXPOSED` | Média | `POST /api/ui/resume` implementado, mas nenhum controle na UI o chama. Um job `interrupted` não tem caminho de recuperação pela interface. | Ausência de `ui/resume` em `static/` e `ui/`; `app_ui.py:1792`; `ui_bridge.py:5460` | Expor "Retomar" para `interrupted`/`resumable` |
| `UI-COPY-NAMES-NVIDIA` | Baixa | A mensagem `environment_not_configured` do frontend diz "Configure o arquivo .env e a `NVIDIA_API_KEY`", mas o provider padrão é DeepL. Copy desatualizada visível ao usuário. | `static/tradutor_ui.js`, mapa `reasonMessages` | Mensagem neutra de provider |
| `PROVIDER-HTTP-TELEMETRY-GAP` | Baixa | Não há telemetria HTTP unificada entre providers; cada um mantém suas próprias `stats`. | `translator_deepl.py`, `translator_nvidia.py` | Se a Beta exigir observabilidade de provider |

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
