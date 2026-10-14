# CLAUDE.md

Instruções permanentes para agentes (Claude, Codex e equivalentes) que trabalham neste
repositório.

> **Criado em:** 2026-08-19, sobre o commit `c81c798`.
> Este arquivo é aditivo. Ao editá-lo, **preserve** as seções existentes; não substitua
> instruções de agente não relacionadas.

---

## Sobre o projeto

**Tradutor IA** — aplicação local (Windows, Python 3.11) que traduz capítulos ilustrados do
inglês para português do Brasil: download, OCR, tradução, reconstrução visual, PDF e gates
de qualidade.

Leia antes de mudar qualquer coisa:

- [`docs/technical/DOCUMENTACAO_TECNICA.md`](docs/technical/DOCUMENTACAO_TECNICA.md) — arquitetura, processos, estados, segurança, testes
- [`docs/user/GUIA_DO_USUARIO.md`](docs/user/GUIA_DO_USUARIO.md) — o que o usuário realmente vê
- [`docs/DOCUMENTATION_POLICY.md`](docs/DOCUMENTATION_POLICY.md) — regras completas de manutenção da documentação
- [`docs/README.md`](docs/README.md) — índice

### Comandos essenciais

```powershell
python start_tradutor.py            # worker supervisionado + UI (canônico)
python start_tradutor.py status     # saúde do worker e da fila
python start_tradutor.py stop       # parada graciosa
python -m pytest -q                 # suíte hermética
python run_webtoon.py --help        # flags reais da CLI
```

### Regras de segurança do repositório

- **Nunca** leia `.env` ou `.env.local` por inteiro (`cat`, `type`, `Get-Content`). Se
  precisar de uma chave específica, use busca direcionada apenas pelo **nome** da variável.
- **Nunca** escreva valor real de chave, token, senha, cookie, service-role key ou conteúdo
  de token do Drive em código, documentação, log, commit ou captura de tela.
- Use placeholders: `DEEPL_API_KEY=<sua-chave>`, `<repo>`, `<URL_DO_CAPITULO>`.
- Não use caminhos com nome real de pessoa (`C:\Users\<pessoa>\...`).
- Testes não podem tocar o banco de jobs de produção, lançar o worker de produção nem
  chamar provider real. O guard hermético existe para isso — não o contorne.

---

## CONTRATO DE SINCRONIZAÇÃO DA DOCUMENTAÇÃO

**Obrigatório em toda tarefa que muda o projeto.** A documentação viaja junto com o
comportamento: quando prático, código e documentação exigida por ele vivem no **mesmo
commit**.

### Antes de concluir qualquer tarefa

1. **Inspecione `git diff`** — todos os arquivos alterados.
2. **Determine o impacto na documentação** usando os gatilhos abaixo.
3. **Atualize a documentação técnica** quando a mudança tocar: arquitetura, schema de banco,
   estados de job, ciclo de vida do worker/launcher, supervisão de processos, configuração
   de runtime, variáveis de ambiente, integração com provider, segurança, cache, testes,
   API HTTP, empacotamento ou updater.
4. **Atualize o guia do usuário** quando a mudança tocar: instalação, tela, rótulo de botão,
   fluxo de uso, recurso disponível, mensagem de erro ou de status, login, atualização,
   licença, local/nome dos arquivos de saída ou comportamento do PDF.
5. **Atualize as capturas de tela** se a UI visível documentada mudou de forma que a imagem
   existente induza erro. Nunca fabrique uma captura; se não puder capturar com segurança,
   documente a lacuna.
6. **Nunca documente comportamento planejado como implementado.** Classifique como
   IMPLEMENTADO / PARCIAL / PLANEJADO / DEPRECIADO. Documentação de usuário jamais instrui
   o uso de recurso inexistente.
7. **Atualize os metadados de verificação** (`Base verificada` / `Revisado em`) dos
   documentos que você tocou. Use a SHA de base auditada, nunca tente embutir a SHA do
   próprio commit.
8. **Se nenhuma mudança de documentação for necessária, diga isso explicitamente** com
   justificativa.

### Verifique antes de escrever

Nada entra na documentação sem verificação contra o repositório: comandos devem ser
executados (ao menos `--help`), caminhos de módulo devem existir no índice do Git, estados
devem ser lidos de `job_store.py`, variáveis devem estar em `.env.example` e no consumidor,
rótulos de UI devem ser lidos de `ui/ui_shell.html` ou `static/*.js`. Um comando que o
repositório não suporta mais **não pode** aparecer na documentação.

### Portão final obrigatório

Toda missão de código termina com este bloco no relatório:

```text
DOCUMENTATION IMPACT

TECHNICAL DOC:   UPDATED / NOT REQUIRED
USER GUIDE:      UPDATED / NOT REQUIRED
SCREENSHOTS:     UPDATED / NOT REQUIRED
WHY:             <uma linha por decisão>
```

Quando nada muda:

```text
DOC IMPACT: NONE
WHY: <motivo — ex.: refatoração interna sem mudança de comportamento, API, config ou UI>
```

**Ausência dessa declaração significa tarefa incompleta.**

### Atualizações futuras já contratadas

- Quando `Setup.exe` existir: o guia do usuário ganha download, instalação, primeira
  execução, avisos do Windows, atalhos, desinstalação, reparo e atualização — na **mesma**
  mudança — e as instruções temporárias de instalação por repositório saem do guia.
- Quando o updater assinado existir: doc técnica ganha manifest, verificação de assinatura
  e de SHA, versão mínima, substituição atômica, rollback e estados de falha; o guia do
  usuário ganha apenas como a atualização aparece, o que clicar e o que fazer se falhar.
- Quando o licenciamento de tester existir: doc técnica ganha arquitetura, fronteira de
  confiança e expiração/revogação/dispositivo; o guia do usuário ganha login, status,
  expiração e mensagem de renovação. Nenhum segredo administrativo em nenhum dos dois.

### Estado do produto e evidência

O texto de estado abaixo era específico de um commit antigo e foi removido para não contradizer
o runtime modular. Consulte o [snapshot atual da reconciliação](docs/consolidation/YOMU_CURRENT_PRODUCT_STATE.md),
a [matriz de proveniência](docs/consolidation/YOMU_SOURCE_RECONCILIATION_MATRIX.md) e o
[board operacional](docs/status/board/index.html). Esses documentos distinguem código integrado,
testes offline/frozen, verificação física, gates externos e itens deferred. A branch de
reconciliação é local e não constitui publicação ou aceitação beta.

### Main-first e preservação

- `main` é a fonte canônica de código concluído. Baseie novas tarefas na `main` atual; use branch temporária para trabalho arriscado e, após testes/documentação/board, prepare integração normal de volta à `main`.
- Não mantenha linhas paralelas permanentes nem descarte worktrees, arquivos não rastreados, evidências ou trabalho de outro agente sem uma decisão explícita e registrada.
- Uma branch de integração local não é merge remoto, beta ou release. Push, merge remoto, instalador e publicação exigem autorização própria.
- Para Supabase, versionar migrations e testes; não alterar produção sem autorização. Preservar fail-closed e não incluir segredos administrativos no cliente.

Detalhes técnicos e dívida histórica:
[Documentação Técnica §29](docs/technical/DOCUMENTACAO_TECNICA.md#29-dívida-técnica-conhecida).
