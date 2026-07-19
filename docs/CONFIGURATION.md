# Configuração

Esta referência descreve as opções reais do `.env.example` e as poucas variáveis adicionais consumidas diretamente pelo código atual.

> [Voltar ao README](../README.md)

## Como a configuração é carregada

`local_environment.py` carrega exclusivamente o `.env` localizado na raiz real do projeto,
sem procurar arquivos em diretórios pais. Variáveis já presentes no ambiente do processo
têm precedência (`override=False`); valores ausentes ou vazios usam o default do código.
Os entrypoints chamam esse helper antes de consultar a configuração.

Valores booleanos verdadeiros aceitos: `1`, `true`, `yes` e `on`, sem distinção de caixa. Qualquer outro valor não vazio é interpretado como falso.

## Neste guia

- [Tradução](#tradução)
- [OCR e classificação](#ocr-e-classificação)
- [Downloader e Selenium](#downloader-e-selenium)
- [Máscara, texto e reconstrução](#máscara-texto-e-reconstrução)
- [Validação visual](#validação-visual)
- [Cache e resume](#cache-e-resume)
- [Paralelismo e recursos](#paralelismo-e-recursos)
- [Precheck, observabilidade e debug](#precheck-observabilidade-e-debug)
- [Páginas lógicas e PDF](#páginas-lógicas-e-pdf)
- [Contexto e UI](#contexto-e-ui)

Comece sempre pelo template:

```powershell
Copy-Item .env.example .env
```

Não versione o `.env` e nunca use uma chave real em exemplos, issues ou logs.

## Tradução

| Variável | Default do template | Obrigatória? | Finalidade |
| --- | --- | --- | --- |
| `NVIDIA_API_KEY` | `sua_chave_aqui` | Sim, no modo NVIDIA | Credencial da API; substitua o placeholder localmente |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | Não | Endpoint compatível com OpenAI |
| `NVIDIA_TRANSLATION_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Não | Modelo usado nas requisições |
| `NVIDIA_TRANSLATION_BATCH_SIZE` | `20` | Não | Quantidade máxima de grupos por lote |
| `NVIDIA_MAX_REQUESTS_PER_MINUTE` | `20` | Não | Limite global aplicado pelo cliente |
| `TRANSLATION_MODE` | `nvidia` | Não | Provedor: `nvidia`, `google`, `huggingface`, `local` ou `nllb` |
| `TRANSLATE_SFX` | `False` | Não | Quando falso, preserva grupos classificados como SFX |

NVIDIA é o provedor padrão e o único exposto como fluxo principal pela UI. Os modos Google e NLLB permanecem como compatibilidade; exigem dependências ou modelos próprios e não têm o mesmo contrato de validação end-to-end auditado.

## OCR e classificação

| Variável | Default | Finalidade |
| --- | --- | --- |
| `OCR_ENGINE` | `paddle` | Engine base carregada por `config.py`; a CLI sobrescreve para `rapidocr` no modo `fast` |
| `OCR_FALLBACK_ENGINE` | `paddle` | Família usada quando a leitura inicial precisa de fallback |
| `OCR_HYBRID_FALLBACK` | `True` | Permite combinar engines em vez de aceitar apenas a inicial |
| `RAPIDOCR_ENABLED` | `False` | Habilita RapidOCR; a CLI `fast` define `True` durante a execução |
| `RAPIDOCR_MIN_CONFIDENCE` | `0.55` | Confidence mínima usada nos sinais de suspeita |
| `RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK` | `True` | Aciona fallback diante de texto estruturalmente suspeito |
| `RAPIDOCR_PAGE_FALLBACK` | `True` | Permite fallback da página para Paddle Mobile |
| `OCR_TEXT_REPAIR` | `True` | Habilita reparos conservadores de OCR |
| `OCR_TEXT_REPAIR_MODE` | `conservative` | Modo implementado para os reparos; outros valores desativam esse caminho |
| `OCR_QUALITY_CONTROL` | `True` | Calcula score e motivos de qualidade dos grupos |
| `OCR_REGION_SELECTIVE_FALLBACK` | `True` | Reprocessa apenas regiões suspeitas quando possível |
| `OCR_GROUP_FALLBACK_MAX_GROUPS` | `8` | Máximo de grupos regionais por página |
| `OCR_GROUP_FALLBACK_PADDING` | `34` | Padding, em pixels, ao redor do crop de fallback |
| `OCR_GROUP_MIN_QUALITY_SCORE` | `0.62` | Limiar base para seleção regional |
| `PRIORITIZE_ENCLOSED_TEXT` | `True` | Dá prioridade a evidências de texto em containers |

O fallback não substitui palavras por regras fixas. Ele pede novas leituras e compara candidatos. Ajustar thresholds pode aumentar custo, falsos positivos ou risco visual; mantenha os defaults até ter uma regressão reproduzível.

## Downloader e Selenium

| Variável | Default | Finalidade |
| --- | --- | --- |
| `SELENIUM_QUIT_TIMEOUT_SECONDS` | `20` | Limite para o encerramento normal do driver, restrito internamente a 1–300 s |
| `SELENIUM_CLEANUP_TIMEOUT_SECONDS` | `3` | Janela do cleanup seletivo, restrita a 0,25–30 s |

`CHROMEDRIVER_PATH` é suportada pelo código, mas não aparece no template. Se estiver vazia,
o código procura apenas `chromedriver`/`chromedriver.exe` já presentes no `PATH`; ele não
usa webdriver-manager nem Selenium Manager para baixar um driver. Defina um caminho local
quando o `PATH` não tiver um driver compatível.

## Máscara, texto e reconstrução

| Variável | Default | Finalidade |
| --- | --- | --- |
| `TEXT_MASK_PADDING` | `3` | Expansão inicial da máscara do texto |
| `MAX_MASK_EXPANSION` | `8` | Limite de expansão durante tentativas visuais |
| `STRICT_MASK_BOUNDS` | `True` | Restringe a máscara à região segura |
| `MASK_COMPONENT_BASED` | `True` | Constrói máscaras a partir de componentes conectados |
| `WHITE_BALLOON_FLAT_FILL` | `True` | Permite preenchimento uniforme em balões brancos compatíveis |
| `ALLOW_LARGE_RECTANGLE_MASK` | `False` | Bloqueia por padrão retângulos amplos de limpeza |
| `TEXT_SAFE_PADDING` | `12` | Margem interna reservada para o texto traduzido |
| `MIN_FONT_SIZE` | `12` | Menor fonte permitida, com piso interno de 6 |
| `MAX_FONT_SIZE` | `42` | Maior fonte permitida |
| `MAX_TEXT_OVERFLOW_RATIO` | `0.01` | Overflow máximo antes de reprovação |
| `AUTO_LINE_WRAP` | `True` | Habilita quebra automática de linha |
| `AUTO_FONT_SHRINK` | `True` | Reduz a fonte quando necessário para caber |
| `TEXTURED_CAPTION_OVERLAY` | `True` | Usa overlay controlado em captions texturizadas |
| `CAPTION_OVERLAY_OPACITY` | `0.94` | Opacidade do overlay, limitada internamente a 0,35–0,95 |

`FONT_PATH` também é suportada fora do template. Quando vazia, o código usa a resolução de fonte existente; quando definida, deve apontar para um arquivo local válido.

## Validação visual

| Variável | Default | Finalidade |
| --- | --- | --- |
| `VISUAL_DIFF_VALIDATION` | `True` | Compara página original e reconstruída |
| `VISUAL_QA_STRICT` | `True` | Aplica o contrato visual estrito |
| `MAX_OUTSIDE_CHANGE_RATIO` | `0.002` | Mudança máxima fora da região permitida |
| `MAX_OUTSIDE_COMPONENT_AREA` | `120` | Área máxima de um componente externo novo |
| `MAX_MASK_TO_TEXT_AREA_RATIO` | `3.0` | Relação máxima entre máscara e texto original |
| `REJECT_BALLOON_BORDER_DAMAGE` | `True` | Rejeita dano detectado na borda do container |
| `REJECT_TEXT_OVERFLOW` | `True` | Rejeita texto fora da safe area |
| `WHITE_BACKGROUND_MIN_BRIGHTNESS` | `205` | Brilho mínimo para fundo branco |
| `WHITE_BACKGROUND_MAX_STD` | `38` | Desvio-padrão máximo para fundo branco |
| `WHITE_BACKGROUND_MAX_SATURATION` | `42` | Saturação máxima para fundo branco |
| `WHITE_BACKGROUND_MIN_RATIO` | `0.70` | Proporção mínima de pixels compatíveis |
| `WHITE_BACKGROUND_MAX_TEXTURE` | `10` | Textura máxima aceita como fundo plano |
| `WHITE_BACKGROUND_MAX_EDGE_DENSITY` | `0.12` | Densidade máxima de bordas |
| `WHITE_BACKGROUND_MAX_DIAGONAL_LINES` | `2` | Máximo de linhas diagonais detectadas |
| `WHITE_ENCLOSURE_MIN_BRIGHTNESS` | `195` | Brilho mínimo de enclosure branco comum |
| `WHITE_ENCLOSURE_MIN_RATIO` | `0.70` | Proporção branca mínima do enclosure |
| `WHITE_ENCLOSURE_MAX_DARK_RATIO` | `0.20` | Proporção escura máxima |
| `WHITE_ENCLOSURE_MAX_SATURATION` | `28` | Saturação máxima |
| `WHITE_STYLIZED_ENCLOSURE_MIN_BRIGHTNESS` | `130` | Brilho mínimo para enclosure estilizado |
| `WHITE_STYLIZED_ENCLOSURE_MIN_RATIO` | `0.28` | Proporção clara mínima no modo estilizado |
| `WHITE_STYLIZED_ENCLOSURE_MAX_DARK_RATIO` | `0.45` | Proporção escura máxima no modo estilizado |
| `WHITE_STYLIZED_ENCLOSURE_MAX_SATURATION` | `8` | Saturação máxima no modo estilizado |
| `MAX_TEXTURED_MASK_GROUP_RATIO` | `0.18` | Fração máxima mascarada em grupo texturizado |
| `MAX_TEXTURED_MASK_COMPONENT_RATIO` | `0.10` | Fração máxima por componente texturizado |
| `REJECT_WHITE_PATCH_OUTSIDE_BALLOON` | `True` | Rejeita manchas brancas fora de balões |
| `REJECT_DARK_BLOTCH_ON_TEXTURED_ART` | `True` | Rejeita manchas escuras novas sobre arte |
| `MAX_NEW_DARK_COMPONENT_AREA` | `120` | Área máxima de componente escuro novo |
| `MAX_NEW_DARK_PIXEL_RATIO` | `0.04` | Proporção máxima de pixels escuros novos |

`POST_RENDER_OCR_VALIDATION=False` no template. O modo CLI `fast` o habilita automaticamente para procurar texto-fonte que ainda permaneça visível depois do redraw.

## Cache e resume

| Variável | Default | Finalidade |
| --- | --- | --- |
| `FULL_FAST_MODE` | `True` | Flag de compatibilidade carregada pelo config; não controla diretamente uma etapa no fluxo atual |
| `ENABLE_OCR_CACHE` | `True` | Reutiliza leituras de OCR compatíveis |
| `ENABLE_TRANSLATION_CACHE` | `True` | Reutiliza traduções por texto e configuração |
| `ENABLE_IMAGE_PROCESS_CACHE` | `True` | Reutiliza páginas renderizadas compatíveis |
| `ENABLE_DOWNLOAD_CACHE` | `True` | Reutiliza o manifest e imagens de download válidos |

O diretório padrão é `.cache`, configurável por `CACHE_ROOT` mesmo que essa variável não esteja no template. As chaves incluem hashes e versões internas. Não edite manifests nem apague todo o cache como primeira tentativa de diagnóstico; prefira `--force` quando precisar de reprocessamento controlado.

## Paralelismo e recursos

| Variável | Default | Finalidade |
| --- | --- | --- |
| `OCR_PARALLEL` | `True` | Permite OCR com workers |
| `OCR_WORKERS` | `2` | Quantidade inicial de workers, sempre pelo menos 1 |
| `ADAPTIVE_PARALLELISM` | `False` | Permite ao scheduler ajustar concorrência |
| `RESOURCE_MONITORING` | `False` | Grava amostras e relatórios de recursos |
| `RESOURCE_MONITOR_INTERVAL_SECONDS` | `1.0` | Intervalo entre amostras |
| `MIN_OCR_WORKERS` | `1` | Piso do scheduler adaptativo |
| `MAX_OCR_WORKERS` | `2` | Teto do scheduler adaptativo |
| `OCR_WORKER_INITIAL_PEAK_MB` | `1800` | Estimativa inicial de pico por worker |
| `MEMORY_SAFETY_MARGIN_PERCENT` | `20` | Margem aplicada ao cálculo de capacidade |
| `MIN_SYSTEM_RESERVE_GB` | `4` | RAM reservada ao sistema |
| `PIPELINE_MEMORY_RESERVE_GB` | `1` | Reserva adicional do pipeline |
| `MEMORY_PRESSURE_ELEVATED_PERCENT` | `72` | Limiar de pressão elevada |
| `MEMORY_PRESSURE_HIGH_PERCENT` | `82` | Limiar de pressão alta |
| `MEMORY_PRESSURE_CRITICAL_PERCENT` | `90` | Limiar de pressão crítica |
| `CPU_PRESSURE_HIGH_PERCENT` | `92` | Limiar de CPU alta |
| `WORKER_SCALE_UP_COOLDOWN_SECONDS` | `20` | Cooldown para aumentar workers |
| `WORKER_SCALE_DOWN_COOLDOWN_SECONDS` | `8` | Cooldown para reduzir workers |
| `OCR_QUEUE_MULTIPLIER` | `10` | Multiplicador do limite da fila |
| `TRANSLATION_PARALLEL` | `True` | Permite lotes de tradução concorrentes |
| `TRANSLATION_WORKERS` | `2` | Quantidade máxima de workers de tradução |

Em máquinas com pouca memória, reduza primeiro `OCR_WORKERS` e `TRANSLATION_WORKERS`. Não diminua reservas do sistema apenas para forçar uma execução.

## Precheck, observabilidade e debug

| Variável | Default | Finalidade |
| --- | --- | --- |
| `SKIP_NO_TEXT_IMAGES` | `True` | Evita OCR completo em páginas sem sinais de texto |
| `NO_TEXT_SKIP_CONSERVATIVE` | `True` | Mantém o precheck deliberadamente conservador |
| `CLASSIFICATION_PROFILING` | `False` | Produz perfil JSON/CSV/HTML da classificação |
| `DEBUG_VISUAL` | `False` | Ativa visualizações de diagnóstico |
| `SAVE_FULL_DEBUG` | `False` | Persiste debug completo por página |
| `SAVE_COMPARE_SAMPLES` | `True` | Gera amostras comparativas |
| `SAVE_DEBUG_ONLY_ERRORS` | `True` | Limita debug detalhado a erros |
| `POST_RENDER_OCR_VALIDATION` | `False` | Valida texto residual depois do redraw |

Ativar debug completo aumenta uso de disco e tempo. Os arquivos gerados podem conter páginas processadas e não devem ser publicados automaticamente.

## Páginas lógicas e PDF

| Variável | Default | Finalidade |
| --- | --- | --- |
| `SMART_WEBTOON_PDF_SPLIT` | `True` | Reconstrói fatias do viewer em páginas lógicas |
| `SMART_PDF_TARGET_HEIGHT` | `1800` | Altura-alvo da página lógica |
| `SMART_PDF_MIN_HEIGHT` | `1050` | Altura mínima |
| `SMART_PDF_MAX_HEIGHT` | `2400` | Altura máxima |

O código aplica pisos internos aos três tamanhos e garante que o máximo seja maior que o mínimo.

## Contexto e UI

O contexto do capítulo é controlado por flags da CLI, não por variáveis do template:

- padrão: mantém `session_context.json`;
- `--no-context`: não usa contexto;
- `--delete-context-after`: remove o contexto somente depois que o PDF existe;
- `--keep-context`: explicita o comportamento padrão.

A porta da UI pode ser alterada com uma variável suportada por `app_ui.py`:

```dotenv
TRADUTOR_UI_PORT=8080
```

## Variáveis avançadas fora do template

Estas opções existem no código, mas foram omitidas do `.env.example` para manter o caminho principal menor:

| Variável | Default | Uso |
| --- | --- | --- |
| `CHROMEDRIVER_PATH` | vazio | Driver explícito quando a descoberta automática falha |
| `TESSERACT_CMD` | caminho convencional do Windows | Executável opcional de Tesseract; não é necessário no fluxo principal |
| `FONT_PATH` | vazio | Fonte local opcional para redraw |
| `CACHE_ROOT` | `.cache` | Raiz dos caches versionados |
| `MAX_RETRIES_DOWNLOAD` | `5` | Tentativas por download |
| `OCR_CONF_THRESHOLD` | `15` | Threshold legado usado por caminhos compatíveis |
| `TRANSLATION_VALIDATION` | `True` | Habilita o validator textual |
| `TRANSLATION_RETRY_ON_MIXED_LANGUAGE` | `True` | Solicita retry quando há mistura detectada |
| `TRANSLATION_MAX_RETRIES` | `2` | Máximo de retries de validação |
| `VISUAL_DIFF_THRESHOLD` | `26` | Threshold de diferença visual |
| `TRADUTOR_UI_PORT` | `8080` | Porta do servidor local |

Evite configurar opções legadas ou avançadas sem uma razão reproduzível. Mudanças em thresholds alteram o contrato do cache e podem dificultar comparações.

## Exemplo mínimo recomendado

```dotenv
NVIDIA_API_KEY=sua_chave_real
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_TRANSLATION_MODEL=nvidia/nemotron-3-super-120b-a12b

OCR_QUALITY_CONTROL=True
OCR_REGION_SELECTIVE_FALLBACK=True
TRANSLATE_SFX=False

ENABLE_OCR_CACHE=True
ENABLE_TRANSLATION_CACHE=True
ENABLE_IMAGE_PROCESS_CACHE=True
ENABLE_DOWNLOAD_CACHE=True
```

Para problemas de inicialização ou recursos, consulte [Troubleshooting](TROUBLESHOOTING.md).
