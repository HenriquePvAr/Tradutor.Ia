# Troubleshooting

Este guia reúne problemas comprovados e caminhos de diagnóstico não destrutivos. Preserve os relatórios antes de alterar configuração ou repetir uma execução.

> [Voltar ao README](../README.md)

## Índice rápido

- [Ambiente virtual](#o-ambiente-virtual-não-ativa)
- [Dependências](#import-ou-dependência-ausente)
- [PaddleOCR](#paddleocr-não-carrega)
- [RapidOCR](#rapidocr-não-está-disponível)
- [NVIDIA](#chave-nvidia-ausente-ou-inválida)
- [ChromeDriver](#chrome-ou-chromedriver-não-inicia)
- [Memória](#pouca-memória)
- [`review_required`](#a-execução-terminou-como-review_required)
- [Launcher](#a-execução-foi-interrompida)
- [Cache](#cache-parece-incompatível)
- [PDF](#o-pdf-não-foi-gerado)

## Diagnóstico inicial

Confirme o ambiente e consulte a ajuda dos entrypoints:

```powershell
python --version
python run_webtoon.py --help
python process_launcher.py --help
git status --short
```

Depois, procure os artefatos do output ativo:

- `progress.json` para status e última página registrada;
- `timing_report.json` para etapa, contagens e PDF;
- `quality_report.html` para revisão visual rápida;
- `download_report.html` para coleta e teardown;
- stdout/stderr do launcher para falhas de processo.

Não apague `.cache` ou `output/` como primeira tentativa. Eles contêm evidências úteis e podem permitir resume seguro.

## O ambiente virtual não ativa

No PowerShell, confirme se o arquivo existe:

```powershell
Test-Path .\.venv\Scripts\python.exe
```

Se existir, use o Python do ambiente sem ativação:

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Isso evita mudanças globais de política no Windows.

## Import ou dependência ausente

Instale o conjunto correspondente dentro da venv:

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-rapidocr.txt
python -m pip install -r requirements-ui.txt
```

Não misture o Python global com o da `.venv`. Confira o executável ativo:

```powershell
python -c "import sys; print(sys.executable)"
```

## PaddleOCR não carrega

Situações comuns:

- primeira inicialização ainda baixando modelos;
- instalação incompleta de `paddlepaddle==3.2.2` ou `paddleocr`;
- memória insuficiente para carregar o modelo;
- cache de pacote externo indisponível.

Verifique a importação antes de executar um capítulo:

```powershell
python -c "import paddle; from paddleocr import PaddleOCR; print(paddle.__version__)"
```

Se o processo encerrar por memória, reduza workers e feche aplicativos de usuário dispensáveis. Não desative proteções do Windows e não reduza reservas do sistema apenas para forçar o carregamento.

## RapidOCR não está disponível

O modo `fast` depende do arquivo separado de requisitos:

```powershell
python -m pip install -r requirements-rapidocr.txt
python -c "from rapidocr_onnxruntime import RapidOCR; print('RapidOCR: OK')"
```

Como alternativa temporária, use `--mode quality`, que inicia com PaddleOCR.

## Chave NVIDIA ausente ou inválida

Confirme que `.env` existe e que o valor não é o placeholder:

```powershell
Test-Path .env
```

O arquivo deve conter, localmente:

```dotenv
NVIDIA_API_KEY=sua_chave_real
```

Não imprima a chave no terminal. A UI verifica apenas se há um valor configurado; erros de autenticação aparecem no log da execução.

## Rate limit ou falha temporária de tradução

`translator_nvidia.py` aplica limite global e retry/backoff. Se o provedor responder com rate limit:

- preserve stdout e `timing_report.json`;
- confirme `NVIDIA_MAX_REQUESTS_PER_MINUTE` e o batch configurado;
- espere a janela do provedor antes de uma nova execução autorizada;
- reutilize cache em vez de forçar todas as etapas.

Reduzir o limite de requisições é mais seguro do que aumentar retries sem evidência.

## Chrome ou ChromeDriver não inicia

Atualize o Chrome e confirme que ele abre normalmente. O downloader tenta
`CHROMEDRIVER_PATH` e depois apenas `chromedriver`/`chromedriver.exe` já presente no
`PATH`; ele não baixa drivers automaticamente.

Quando a descoberta automática falhar, o job deve terminar com `chromedriver_unavailable`.
Configure um driver compatível:

```dotenv
CHROMEDRIVER_PATH=C:\ferramentas\chromedriver.exe
```

Leia `download_report.json`, especialmente o bloco `teardown`. Não encerre todos os processos Chrome por nome: o projeto distingue processos próprios de navegadores externos.

## Pouca memória

PaddleOCR pode consumir vários gigabytes e provocar paginação. Antes de iniciar:

1. encerre apenas aplicativos comuns cuja propriedade e ausência de trabalho não salvo estejam claras;
2. preserve UI, terminal, acesso remoto, segurança e processos do sistema;
3. reduza a concorrência no `.env`:

```dotenv
OCR_WORKERS=1
TRANSLATION_WORKERS=1
ADAPTIVE_PARALLELISM=True
RESOURCE_MONITORING=True
```

Mantenha `MIN_SYSTEM_RESERVE_GB` e `PIPELINE_MEMORY_RESERVE_GB` em valores conservadores. Um erro real de alocação exige preservar logs e investigar; não repita automaticamente uma execução completa.

## A execução terminou como `review_required`

Esse status significa que o processamento técnico concluiu, mas o quality gate encontrou revisão pendente. O PDF pode estar disponível e o exit code técnico pode ser `0`.

Abra:

- `quality_report.html` para itens de revisão;
- `quality_report.json` para reasons e métricas;
- `timing_report.json` para o status consolidado;
- `progress.json` para a visão por página.

Não mude manualmente o status para `finished`. Resolva ou aceite os itens por um fluxo de revisão separado.

## A execução foi interrompida

Quando o launcher oficial é usado, consulte o diretório de runtime:

- `exit_code.txt`: código persistido após o término;
- `launcher_error.txt`: erro do launcher, quando houver;
- `launcher_events.jsonl`: criação, associação, cancelamento e cleanup;
- `child_pid.txt`: identificador registrado do filho;
- stdout/stderr: saída preservada do processo.

Os códigos próprios são `130` para cancelamento, `251` para falha de launch e `252` para falha crítica de cleanup. Arquivo ausente significa que a execução pode ainda estar ativa ou que o launcher não chegou à persistência final; arquivo vazio é legado e não prova sucesso.

Antes de retomar, confirme que não existe outro pipeline ativo. Não inicie duas execuções para o mesmo output.

## Cache parece incompatível

Os caches usam hashes, configuração e versões internas. Para um reprocessamento controlado, use uma saída nova e a flag existente:

```powershell
python run_webtoon.py "<URL_DO_CAPITULO>" --mode fast --force --output "nova_execucao"
```

`--force` ignora caches de download, OCR, tradução e renderização nessa execução. Ele não apaga o cache global. Evite editar manifests ou remover `.cache` inteiro; isso destrói evidências e pode provocar downloads e traduções desnecessários.

## O PDF não foi gerado

Verifique nesta ordem:

1. `download_gate.passed` em `downloaded_images.json`;
2. páginas com `status=error` em `progress.json`;
3. caminho `pdf_path` em `timing_report.json`;
4. arquivos inválidos ou em branco no bloco `quality_validation`;
5. traceback no stderr ou stdout.

Uma falha técnica deve terminar como `error`, não como `review_required`.

## A UI não abre

Confirme a dependência e a porta:

```powershell
python -c "import nicegui; print(nicegui.__version__)"
$env:TRADUTOR_UI_PORT = "8080"
python app_ui.py
```

Se a porta estiver ocupada, escolha outra porta temporária. Não inicie uma segunda instância na mesma porta.

## O que anexar a um relatório de bug

Compartilhe somente dados sem segredos e sem material protegido:

- versão do Python e sistema operacional;
- comando com URL, chave e tokens removidos;
- traceback ou trecho mínimo do log;
- status e reason relevantes dos JSONs;
- nomes e versões das dependências envolvidas;
- passos mínimos para reproduzir com fixture própria ou autorizada.

Não publique `.env`, outputs de terceiros, capítulos, cookies, tokens ou caminhos pessoais desnecessários.
