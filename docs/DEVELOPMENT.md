# Desenvolvimento

> **Base verificada:** `a6a46e4` (branch `fix/main-e2e-findings`) · **Revisado em:** 2026-09-03
>
> [Voltar ao índice](README.md)

Este documento é o ponto de entrada de quem vai **rodar e testar** o repositório. A visão
geral do produto está no [README](../README.md); o desenho interno está em
[Arquitetura](ARCHITECTURE.md); os gates de qualidade estão em
[Qualidade e validação](QUALITY_AND_VALIDATION.md).

## Neste guia

- [Pré-requisitos](#pré-requisitos)
- [Instalação](#instalação)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Executar](#executar)
- [Testes](#testes)
- [Estrutura de diretórios](#estrutura-de-diretórios)
- [Dados de runtime e saída do usuário](#dados-de-runtime-e-saída-do-usuário)
- [OpenCV: contrato e inconsistência conhecida](#opencv-contrato-e-inconsistência-conhecida)
- [Dependências externas](#dependências-externas)
- [Empacotamento e distribuição](#empacotamento-e-distribuição)
- [Riscos conhecidos](#riscos-conhecidos)

---

## Pré-requisitos

| Item | Versão auditada | Tipo |
| --- | --- | --- |
| Windows 64 bits | 10/11 | ambiente auditado |
| Python | 3.11 | **DEV + RUNTIME** |
| Node.js | 24 (`node-version: "24"` na CI) | **DEV** — só para as suítes JS e o serviço Better Auth |
| Google Chrome + chromedriver | — | **OPCIONAL/FALLBACK** — só quando a descoberta HTTP não se aplica |
| Tesseract | — | **OPCIONAL** — caminho de OCR alternativo, não usado no padrão Beta |
| Runtime MSVC | — | implícito nas wheels; não é instalação manual |

Outros sistemas operacionais não fazem parte do contrato validado atual.

## Instalação

```powershell
git clone https://github.com/HenriquePvAr/Tradutor.Ia.git
cd Tradutor.Ia

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel

python -m pip install -r requirements.txt
python -m pip install -r requirements-rapidocr.txt
python -m pip install -r requirements-ui.txt
python -m pip install -r requirements-dev.txt      # somente para rodar os testes
```

Os manifests são a fonte de verdade das dependências. Não replique a lista aqui:

| Manifest | Conteúdo |
| --- | --- |
| `requirements.txt` | stack principal, incluindo os pins de imagem/CV (`numpy`, `Pillow`, `opencv-python`) e `nicegui` |
| `requirements-rapidocr.txt` | RapidOCR + onnxruntime (engine de OCR padrão) |
| `requirements-ui.txt` | pin de NiceGUI espelhado, para as duas listas não divergirem |
| `requirements-dev.txt` | pytest e o cliente ASGI offline usado pela bateria de segurança |
| `requirements-optional.txt` | transportes opcionais, importados de forma preguiçosa e desligados por padrão |
| `apps/auth-service/package.json` | serviço Better Auth (Node/TypeScript) |

## Variáveis de ambiente

- **Fonte de verdade dos nomes:** `.env.example`.
- **Fonte de verdade dos defaults:** `config.py` (e `ui_helpers.py` para provider de
  tradução).
- **Referência comentada:** [Configuração](CONFIGURATION.md).

Regras:

```powershell
Copy-Item .env.example .env
```

- `.env` e `.env.local` **não** são versionados e nunca devem ser lidos por inteiro em log,
  captura de tela ou commit.
- `.env.example` contém apenas placeholders e **nunca é carregado pela aplicação** — é
  documentação executável, não configuração ativa.
- Chaves de API vivem apenas no processo servidor. O frontend nunca recebe segredo.

Mínimo para uma execução completa: `DEEPL_API_KEY` (provider de tradução padrão) e as
variáveis do Supabase exigidas pelo login. As demais têm defaults conservadores.

Detalhes de fronteira de confiança em [Segurança](SECURITY.md).

## Executar

```powershell
python start_tradutor.py            # canônico: worker supervisionado + UI
python start_tradutor.py status     # saúde do worker e da fila
python start_tradutor.py stop       # parada graciosa do worker
```

A aplicação escuta em `http://127.0.0.1:8080` por padrão (`TRADUTOR_UI_PORT`, lido em
`app_ui.py`). No Windows há também `start_tradutor.bat`.

`python app_ui.py` sobe **apenas** a interface. Sem worker, os capítulos ficam `queued`.

CLI:

```powershell
python run_webtoon.py "<URL_DO_CAPITULO>" --mode fast --no-context
python run_webtoon.py --help
```

## Testes

Os testes padrão são **offline**: um guard de processo bloqueia socket antes de qualquer
request. Smokes de rede vivem em `scripts/`, exigem opt-in explícito e estão documentados em
[Testes](TESTING.md).

### Python

```powershell
python -m pytest                    # suíte hermética (marcadores network/manual excluídos)
python -m pytest --collect-only -q  # o que a CI coleta
python -m unittest discover         # mesma base, runner unittest
```

`pytest.ini` exclui `network` e `manual` por padrão e mantém a coleta fora de `output`,
`.cache`, `.venv`, `tmp` e `node_modules`.

Para um teste isolado:

```powershell
python -m pytest test_<nome>.py -q
```

### JavaScript

As 14 suítes `.mjs` na raiz cobrem o frontend (leitor, sessão, comunidade, health).

```powershell
node --experimental-vm-modules test_chapter_reader.mjs
```

> **A flag `--experimental-vm-modules` é obrigatória.** Onze das quatorze suítes montam os
> módulos do frontend com `vm.SourceTextModule`, que só existe sob essa flag. Sem ela o Node
> não falha na importação — cada teste falha individualmente com
> `vm.SourceTextModule is not a constructor`, o que parece defeito de produto e não é.
> As duas suítes que documentam `// Run: node <arquivo>.mjs` no cabeçalho (`test_chapter_reader.mjs`,
> `test_service_health.mjs`) rodam sem a flag; usar a flag em todas é o caminho uniforme.

Rodar todas:

```powershell
Get-ChildItem test_*.mjs | ForEach-Object { node --experimental-vm-modules $_.Name }
```

Verificação de sintaxe (o que a CI faz):

```powershell
node --check static/tradutor_ui.js
```

### Serviço Better Auth

```powershell
cd apps/auth-service
npm ci
npm run typecheck
npm test
npm run build
```

### CI

O workflow `Hermetic Tests` (`.github/workflows/tests.yml`) roda em `windows-latest` com
Python 3.11 e Node 24, instala `requirements.txt` + `requirements-dev.txt`, confirma que
nenhum opt-in de smoke de rede está autorizado, roda `pytest`, `node --check`, o serviço
Better Auth e `py_compile` dos módulos centrais. **A CI não executa as suítes `.mjs`** —
elas são responsabilidade local.

## Estrutura de diretórios

O código Python de produção é plano, na raiz do repositório.

```text
<repo>/
├── *.py                     # pipeline, UI, worker, adapters, validação (módulos de topo)
├── test_*.py                # suíte hermética Python
├── test_*.mjs               # suítes de frontend
├── test_support/            # helpers compartilhados de teste
├── test_fixtures/           # artefatos fixos usados por testes
├── scripts/                 # smokes manuais com opt-in, assinatura de release, migração de auth
├── static/                  # JS/CSS do frontend, i18n, assets da UI
├── ui/                      # shell HTML da interface
├── apps/auth-service/       # serviço Better Auth (Node/TypeScript)
├── supabase/                # migrations SQL e testes de banco
├── docs/                    # esta documentação
└── .github/workflows/       # CI
```

## Dados de runtime e saída do usuário

Quatro categorias distintas, todas fora do controle de versão exceto a primeira:

| Categoria | Onde | Versionado |
| --- | --- | --- |
| **Código-fonte** | raiz do repositório | sim |
| **Cache** | `.cache/` (download, OCR, tradução, render, `ui_history.json`) | não |
| **Runtime** | `.cache/runtime/` (banco de jobs `jobs.sqlite3`, logs por job) e `.runtime/` | não |
| **Saída do usuário** | `output/<slug>/<run_id>/` (páginas, relatórios, PDF) | não |

Todos são caminhos relativos ao repositório, resolvidos em `ui_helpers.py`
(`OUTPUT_ROOT`, `HISTORY_PATH`). Modelos locais de reconstrução de arte ficam fora do
repositório, sob `%LOCALAPPDATA%\TradutorIA\models` por padrão
(`ADVANCED_ART_INPAINT_MODEL_PATH`).

O modelo de instalação previsto pelo updater (`update_installer.py`) mantém `data/` fora de
`versions/` justamente para que ativação e rollback nunca alcancem dados do usuário.

## OpenCV: contrato e inconsistência conhecida

### Contrato declarado

`requirements.txt` fixa **`opencv-python==5.0.0.93`** e declara explicitamente que
`opencv-contrib-python` e `opencv-python-headless` **não** fazem parte do contrato. O pin é
parte do contrato de qualidade, não preferência de versão: a decisão
`open_light_art_caption` compara razões de pixel contra limiares literais calibrados contra
a saída de `cv2` no ambiente congelado.

### Estado real observado no ambiente de desenvolvimento

**Isto é uma inconsistência aberta e não resolvida.** No ambiente que produziu a baseline do
freeze coexistem três distribuições que fornecem o mesmo módulo `cv2`:

| Distribuição | Versão | Local |
| --- | --- | --- |
| `opencv-python` (contratual) | 5.0.0.93 | site-packages do sistema |
| `opencv-python-headless` | 5.0.0.93 | site-packages **do usuário** |
| `opencv-contrib-python` | 4.10.0.84 | site-packages do sistema |

Como o site-packages do usuário precede o do sistema em `sys.path`, o `cv2` efetivamente
importado vem da árvore onde está instalado o pacote **headless** — não necessariamente o
pacote declarado como contratual. As duas distribuições 5.0.0.93 expõem a mesma versão de
`cv2`, e é por isso que a suíte passa inteira; isso **não** prova que a variante correta está
sendo carregada.

Consequências:

- a suíte verde não é evidência de que o contrato de OpenCV está sendo respeitado em runtime;
- nenhum pacote foi removido e nenhum manifest foi alterado para "consertar" isso — fazer
  isso durante o Quality Freeze invalidaria a evidência de qualidade existente;
- o empacotamento **deve** convergir para uma única distribuição de OpenCV selecionada e
  provar isso com instalação limpa + E2E, não com a suíte.

Registrado como `OPENCV-VARIANT-SHADOWING-001` em [Riscos conhecidos](#riscos-conhecidos).

## Dependências externas

Diferencie o tipo antes de agir sobre qualquer item:

| Dependência | Tipo | Observação |
| --- | --- | --- |
| Python 3.11 | DEV + RUNTIME | versão auditada |
| Node 24 | DEV | suítes `.mjs` e serviço Better Auth; o pipeline não precisa dele |
| Chrome + chromedriver | OPCIONAL/FALLBACK | usado só quando a descoberta HTTP não se aplica à fonte |
| Tesseract | OPCIONAL | caminho de OCR alternativo, fora do padrão Beta |
| Modelo local de reconstrução de arte | OPCIONAL/RUNTIME | asset externo verificado por SHA256; ausência vira revisão, não render forçado |
| Fontes do Windows | PACKAGING CONCERN | `font_fidelity.ROLE_FONT_FILES` resolve cadeias de fontes **locais** do sistema (Trebuchet, Comic Sans, Segoe Print, Calibri e similares). Nada é baixado. A disponibilidade dessas fontes numa instalação Windows limpa não foi provada — `NEEDS_VERIFICATION` |
| Runtime MSVC | PACKAGING CONCERN | implícito nas wheels |

Nada nesta tabela é uma instrução de instalação para usuário final. O objetivo declarado do
empacotamento é justamente empacotar ou eliminar essas dependências.

### Candidatos a limpeza de dependência

Declarados em `requirements.txt` e sem nenhum `import` no repositório:

- `webdriver-manager`
- `tqdm`
- `accelerate`
- `sentencepiece`

**Não remova durante o Quality Freeze.** São candidatos a reconciliação na fase de
empacotamento; remover um pacote muda o ambiente resolvido e invalida a baseline.

## Empacotamento e distribuição

| Item | Estado |
| --- | --- |
| Baseline de empacotamento | **PRONTA** — pipeline congelado, manifests pinados, evidência de E2E registrada |
| Instalação limpa em Windows | **NÃO PROVADA** — não existe validação em máquina limpa |
| Instalador para usuário final | **NÃO EXISTE** — não há spec de build nem `Setup.exe` no repositório |
| Modelo de atualização | **PARCIAL** — `update_installer.py`/`update_manifest.py`/`update_transport.py` implementam staging, ativação atômica e rollback; canal assinado e superfície de UI pendentes |
| Distribuição Beta | **EM PREPARAÇÃO** |

Próxima fase planejada: distribuição única de OpenCV, runtime empacotado, instalador, teste
em Windows limpo, updater e distribuição Beta controlada. Nada disso é implementado hoje.

## Riscos conhecidos

| ID | Risco | Estado |
| --- | --- | --- |
| `OPENCV-THRESHOLD-SENSITIVITY-001` | Limiares literais de pixel calibrados contra o `cv2` congelado; outra versão pode virar a classificação em regiões de fronteira. Trocar versão **ou variante** exige reexecutar o E2E de qualidade, não só a suíte. | **aberto** — detalhe em [Qualidade e validação](QUALITY_AND_VALIDATION.md) |
| `OPENCV-VARIANT-SHADOWING-001` | Três variantes de OpenCV coexistem; shadowing de `sys.path` faz a variante `-headless` fornecer `cv2`, não a declarada como contratual. | **aberto** — ver [seção acima](#opencv-contrato-e-inconsistência-conhecida) |
| `CLEAN-INSTALL-NOT-YET-PROVEN` | Nenhuma instalação limpa em Windows foi validada a partir dos manifests. | **aberto** |
| Chrome/Selenium × TPM | Em máquinas afetadas por problemas de Microsoft Platform Crypto Provider / TPM, o Chrome pode falhar ao iniciar, inutilizando o fallback de navegador. A descoberta HTTP evita essa dependência para as fontes que a suportam. | **aberto, ambiente de desenvolvimento** — não altere TPM/BIOS/Windows Hello por causa disto |
| Reconstrução de arte em textura | Regiões texturizadas/open-art podem render sob revisão de fidelidade. | **aberto** |
| Naturalidade semântica PT-BR | Tradução gramatical mas semanticamente errada não é detectada por gate automático. | **aberto** |
| Dependências declaradas e não usadas | Ver [candidatos a limpeza](#candidatos-a-limpeza-de-dependência). | **aberto, adiado para empacotamento** |

## Troubleshooting

Diagnóstico de execução, artefatos e erros comuns: [Troubleshooting](TROUBLESHOOTING.md).

Pontos rápidos:

- capítulo fica `queued`: não há worker: use `python start_tradutor.py`, não `python app_ui.py`;
- suíte `.mjs` falha com `vm.SourceTextModule is not a constructor`: falta a flag
  `--experimental-vm-modules`;
- `review_required` não é falha: veja [Filosofia de qualidade](../README.md#filosofia-de-qualidade).
