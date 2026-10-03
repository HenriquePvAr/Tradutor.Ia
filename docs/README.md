# Documentação do Tradutor IA

> **Base verificada:** `27c1450` (branch `main`) · **Revisado em:** 2026-09-30
>
> O comportamento de produção está sob **[Quality Freeze](QUALITY_FREEZE.md)**.

Esta é a página inicial da documentação. Comece pelo documento certo para o seu papel.

---

## Comece por aqui

| Se você é… | Leia |
| --- | --- |
| 🧑‍🎨 **Scan, tradutor, editor ou testador da Beta** | [**Guia do Usuário**](user/GUIA_DO_USUARIO.md) |
| 🛠️ **Desenvolvedor, mantenedor ou suporte técnico** | [**Documentação Técnica**](technical/DOCUMENTACAO_TECNICA.md) |
| 🤖 **Agente automatizado (Claude/Codex) trabalhando no repositório** | [`CLAUDE.md`](../CLAUDE.md) + [Política de Documentação](DOCUMENTATION_POLICY.md) |
| 📋 **Auditando a documentação** | [Auditoria de Documentação](DOCUMENTATION_AUDIT.md) |

---

## Documentos primários

| Documento | Conteúdo |
| --- | --- |
| [Guia do Usuário](user/GUIA_DO_USUARIO.md) | Instalar, entrar, traduzir, acompanhar, revisar, resolver erros. Linguagem simples. |
| [Saídas e artefatos](OUTPUT_ARTIFACTS.md) | Perfis Automático/QC Profissional, paths, manifests e distinção entre job, run e artifact. |
| [Documentação Técnica](technical/DOCUMENTACAO_TECNICA.md) | Arquitetura, processos, estados, pipeline, segurança, testes, comandos, dívida técnica. |
| [Política de Documentação](DOCUMENTATION_POLICY.md) | Quando e como a documentação deve ser atualizada junto com o código. |
| [Auditoria de Documentação](DOCUMENTATION_AUDIT.md) | O que foi verificado, o que estava desatualizado e o que continua em aberto. |
| [README do projeto](../README.md) | Visão geral curta e início rápido. |

---

## Referência por assunto

### Instalação e configuração

- [Instalação](INSTALLATION.md) — ambiente, dependências, modelos e primeiro teste
- [Configuração](CONFIGURATION.md) — variáveis do `.env.example`, defaults e ajustes
- [Internacionalização](I18N.md) — idiomas da interface

### Arquitetura e execução

- [Arquitetura](ARCHITECTURE.md) — módulos, fluxos, descoberta de fonte, OCR, tradução, entidades, reconstrução, render e artefatos
- [Desenvolvimento](DEVELOPMENT.md) — setup, execução, testes, dependências externas, empacotamento e riscos conhecidos
- [Estrutura do projeto](PROJECT_STRUCTURE.md) — localização do código, testes, ferramentas e pesquisa
- [Fluxo de desenvolvimento](DEVELOPMENT_WORKFLOW.md) — comandos de validação após mudanças
- [Política de artefatos](ARTIFACT_POLICY.md) — conteúdo versionado e outputs externos
- [Fila de worker persistente](WORKER_QUEUE.md) — processos, estados, cancelamento e recuperação

### Fontes de capítulo

- [Adapters de fonte de capítulo](SOURCE_ADAPTERS.md) — como uma URL é analisada
- [Adaptador universal de capítulos](UNIVERSAL_CHAPTER_ADAPTER.md) — fallback controlado, limites e restrições
- [Entrada por pasta local](LOCAL_FOLDER_INPUT.md) — processar imagens já presentes no computador
- [Transportes de download](DOWNLOAD_TRANSPORTS.md) — abstração de transporte e validação por bytes

### PSD e saídas profissionais

- [M15 — matriz de compatibilidade PSD](PSD_COMPATIBILITY_MATRIX.md) — evidência física Photoshop/Photopea; cross-editor round-trip ainda não executado
- [M17 — performance e tamanho PSD](PSD_PERFORMANCE_REPORT.md) — medições do job existente e estimativas separadas

### PSD e saídas profissionais

- [M15 — matriz de compatibilidade PSD](PSD_COMPATIBILITY_MATRIX.md) — evidência física Photoshop/Photopea; cross-editor round-trip ainda não executado
- [M17 — performance e tamanho PSD](PSD_PERFORMANCE_REPORT.md) — medições do job existente e estimativas separadas

### Qualidade

- [Qualidade e validação](QUALITY_AND_VALIDATION.md) — gates, fallbacks, retries, estados terminais e riscos abertos
- [M18.5 — hardening de qualidade de tradução](TRANSLATION_QUALITY_HARDENING.md) — guards determinísticos, retry/re-score e limitação semântica conhecida
- [M16 — replay offline de qualidade](M16_TRANSLATION_QUALITY_REPLAY.md) — cobertura e limitações do replay read-only
- [M20 — cobertura de tradução e integridade CLEAN](M20_RC_BLOCKER_HOTFIX.md) — ordem de reservation/retry, falhas sistêmicas e remoção independente do texto-fonte
- [Quality Freeze](QUALITY_FREEZE.md) — o que está congelado, a evidência e como sair do freeze
- [Taxonomia semântica e auditoria linguística](../SEMANTIC_CLASSIFICATION_AUDIT.md)

### Comunidade, autenticação e armazenamento

- [Autenticação Supabase](SUPABASE_AUTH.md)
- [Autorização da comunidade](COMMUNITY_AUTHORIZATION.md)
- [Backend social Supabase](SUPABASE_SOCIAL_BACKEND.md)
- [Schema do banco social](SUPABASE_SOCIAL_SCHEMA.md)
- [Interface da comunidade](SOCIAL_COMMUNITY_UI.md)
- [Publicação explícita de PDFs](EXPLICIT_SOCIAL_PDF_PUBLISHING.md)
- [Retenção e reconciliação de assets sociais](SOCIAL_ASSET_RETENTION_RECONCILIATION.md)
- [Armazenamento privado no Google Drive](COMMUNITY_STORAGE.md)
- [Preparação do Better Auth](BETTER_AUTH_MIGRATION.md)

### Segurança, testes e diagnóstico

- [Segurança e limites de confiança](SECURITY.md)
- [Testes herméticos e smokes manuais](TESTING.md)
- [Troubleshooting](TROUBLESHOOTING.md)

### Release e closed beta

- [Release](RELEASE.md)
- [Checklist de release](RELEASE_CHECKLIST.md)
- [Update e signing](UPDATE_AND_SIGNING.md)
- [E2E em máquina limpa](CLEAN_VM_E2E.md)
- [Guia do tester](CLOSED_BETA_TESTER_GUIDE.md)
- [Bug reporting](BUG_REPORTING.md)
- [Known issues](KNOWN_ISSUES.md)
- [Privacidade e dados](PRIVACY_AND_DATA.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

### Auditorias históricas

Documentos de auditoria pontual, preservados como registro. Descrevem o estado no momento
em que foram escritos e **não** substituem os documentos primários:

- [Auditoria funcional — fontes e submissão](FULL_FUNCTIONAL_AUDIT.md)
- [Taxonomia semântica e auditoria linguística](../SEMANTIC_CLASSIFICATION_AUDIT.md)
- [Plano de rollback da migração de auth](../scripts/auth-migration/rollback-plan.md)

---

## Estado resumido do produto — snapshot histórico

> A tabela abaixo veio de uma avaliação anterior e não foi revalidada como estado atual na M18.
> Suas afirmações de licenciamento, distribuição, instalador, updater e OpenCV são contexto
> histórico, não status de release. A M18 não executou uma auditoria de release. Capacidades
> atuais de fontes e outputs estão nos documentos primários acima.

| Área | Estado |
| --- | --- |
| Pipeline ponta a ponta | ✅ implementado, sob [Quality Freeze](QUALITY_FREEZE.md) |
| Descoberta de fonte HTTP-first | ✅ implementado — usada pelas fontes cujo adapter a suporta |
| Fallback de navegador (Chrome/Selenium) | ✅ implementado — caminho normal das demais fontes |
| OCR RapidOCR como engine primário | ✅ implementado |
| PaddleOCR | ⛔ desligado por padrão — compatibilidade legacy opt-in |
| Tradução DeepL como provider padrão | ✅ implementado |
| Ledger de terminologia e registro de personagens | ✅ implementado |
| Fila persistente e worker independente | ✅ implementado |
| Detecção de crash duro e reconciliação | ✅ implementado |
| Supervisão do worker pelo launcher | ✅ implementado |
| Isolamento hermético dos testes | ✅ implementado |
| Reconstrução visual de arte | ⚠️ funcional; fidelidade em regiões texturizadas continua eixo aberto |
| Qualidade semântica/natural PT-BR | ⚠️ aberta — tradução gramatical pode perder sentido sem gate automático |
| Refinamento natural PT-BR (Nemotron) | ⚠️ **não automático** — sugestão manual e explicitamente autorizada na revisão |
| Leitor PDF integrado | ✅ implementado (aba **Leitor**, ação **LER** no Histórico) |
| Histórico | ✅ implementado |
| Comunidade (Supabase + Drive) | ✅ implementado, fail-closed se não configurado |
| Licenciamento de tester | ✅ schema/RLS/RPC remotos aplicados; primeiro tester real ainda não criado |
| Retomada de job interrompido pela UI | ✅ implementada (TDD #56) — botão **Retomar** para os jobs marcados `can_resume` |
| Instalador para usuário final | ⛔ não existe — sem spec de build no repositório |
| Atualizador | ⚠️ parcial — staging/ativação atômica/rollback existem; canal assinado e UI pendentes |
| Validação em Windows limpo | ⛔ não provada |
| Variante única de OpenCV em runtime | ⛔ não convergida — ver `OPENCV-VARIANT-SHADOWING-001` |

Detalhamento em [Desenvolvimento](DEVELOPMENT.md),
[Documentação Técnica §2](technical/DOCUMENTACAO_TECNICA.md#2-escopo-atual-do-produto)
e [§29 Dívida técnica](technical/DOCUMENTACAO_TECNICA.md#29-dívida-técnica-conhecida).

---

## Roadmap histórico ≠ produto atual

O roadmap na raiz é um snapshot de planejamento anterior às missões M1–M17, não um inventário
atual de capacidades. Preserve-o como histórico; para comportamento atual, use os documentos
primários acima.
