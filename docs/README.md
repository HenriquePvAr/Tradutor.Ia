# Documentação do Tradutor IA

> **Base verificada:** branch `fix/main-e2e-findings` · **Revisado em:** 2026-08-20

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

- [Arquitetura](ARCHITECTURE.md) — módulos, fluxos, cache, launcher e artefatos
- [Fila de worker persistente](WORKER_QUEUE.md) — processos, estados, cancelamento e recuperação

### Fontes de capítulo

- [Adapters de fonte de capítulo](SOURCE_ADAPTERS.md) — como uma URL é analisada
- [Adaptador universal de capítulos](UNIVERSAL_CHAPTER_ADAPTER.md) — fallback controlado, limites e restrições
- [Entrada por pasta local](LOCAL_FOLDER_INPUT.md) — processar imagens já presentes no computador
- [Transportes de download](DOWNLOAD_TRANSPORTS.md) — abstração de transporte e validação por bytes

### Qualidade

- [Qualidade e validação](QUALITY_AND_VALIDATION.md) — gates, fallbacks, retries e estados terminais
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

### Auditorias históricas

Documentos de auditoria pontual, preservados como registro. Descrevem o estado no momento
em que foram escritos e **não** substituem os documentos primários:

- [Auditoria funcional — fontes e submissão](FULL_FUNCTIONAL_AUDIT.md)
- [Taxonomia semântica e auditoria linguística](../SEMANTIC_CLASSIFICATION_AUDIT.md)
- [Plano de rollback da migração de auth](../scripts/auth-migration/rollback-plan.md)

---

## Estado atual do produto

| Área | Estado |
| --- | --- |
| Pipeline ponta a ponta | ✅ implementado |
| Fila persistente e worker independente | ✅ implementado |
| Detecção de crash duro e reconciliação | ✅ implementado |
| Supervisão do worker pelo launcher | ✅ implementado |
| Isolamento hermético dos testes | ✅ implementado |
| Qualidade / story-text / resíduo físico | ✅ **QUALITY CLOSED — REAL POST-#75 E2E VALIDATED** no TDD #76: `ordinary_story_physical_residual_count=0`, 100 story regions renderizadas limpas, 4 reviews preservados como SFX/OCR ambíguo não-story |
| Comunidade (Supabase + Drive) | ✅ implementado, fail-closed se não configurado |
| Licenciamento de tester | ✅ fundação local/offline implementada no TDD #77; integração Supabase remota pendente |
| Retomada de job interrompido pela UI | ⚠️ parcial — API existe, controle na interface não |
| Instalador para usuário final | ⏳ em desenvolvimento |
| Atualizador assinado | ⚠️ parcial — canal/chave/UI pendentes |
| Validação em VM Windows limpa | ⏳ em desenvolvimento |

Detalhamento em [Documentação Técnica §2](technical/DOCUMENTACAO_TECNICA.md#2-escopo-atual-do-produto)
e [§29 Dívida técnica](technical/DOCUMENTACAO_TECNICA.md#29-dívida-técnica-conhecida).

---

## Roadmap ≠ documentação

O roadmap do projeto vive no [README da raiz](../README.md#roadmap). Ele descreve
**intenções**, não comportamento disponível. Nenhum item de roadmap deve aparecer nesta
documentação como recurso existente.
