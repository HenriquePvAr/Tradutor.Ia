# Tradutor.Ia

Tradutor.Ia baixa imagens de paginas de manga, HQ ou webtoon, detecta baloes,
faz OCR, traduz o texto para portugues do Brasil, redesenha a traducao nos
baloes e gera um PDF final.

## Principais modos

- OCR principal e padrao seguro: PaddleOCR.
- RapidOCR/ONNX opcional e experimental, com fallback hibrido para PaddleOCR.
- Tesseract continua disponivel como fallback manual quando instalado.
- Traducao padrao: NVIDIA API com `nvidia/nemotron-3-super-120b-a12b`.
- Traducao em lote: ate 20 baloes por request.
- Modos antigos preservados: Google (`deep-translator`) e HuggingFace/local.

## Instalacao no Windows

Crie ou entre na pasta do projeto:

```powershell
cd C:\Users\henrique.araujo\Projetos\Tradutor.Ia
```

Crie o ambiente virtual com Python 3.11:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
```

Instale as dependencias principais:

```powershell
pip install -r requirements.txt
```

O arquivo inclui PaddleOCR, PaddlePaddle 3.2.2, o cliente da NVIDIA e as
dependencias dos modos Google e HuggingFace/local.

## Configuracao do .env

Copie `.env.example` para `.env` e preencha sua chave:

```powershell
Copy-Item .env.example .env
notepad .env
```

Exemplo:

```env
NVIDIA_API_KEY=sua_chave_aqui
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_TRANSLATION_MODEL=nvidia/nemotron-3-super-120b-a12b
NVIDIA_TRANSLATION_BATCH_SIZE=20
NVIDIA_MAX_REQUESTS_PER_MINUTE=20
OCR_ENGINE=paddle
OCR_FALLBACK_ENGINE=paddle
OCR_HYBRID_FALLBACK=True
RAPIDOCR_ENABLED=False
TRANSLATE_SFX=False
PRIORITIZE_ENCLOSED_TEXT=True
TRANSLATION_MODE=nvidia
```

`.env` esta no `.gitignore` e nao deve ser commitado.

## Como usar PaddleOCR

PaddleOCR e o OCR principal quando:

```env
OCR_ENGINE=paddle
```

Use `paddlepaddle==3.2.2`. A versao `3.3.1` pode falhar durante o OCR com
erro interno de atributo PIR em algumas instalacoes Windows.

O idioma original escolhido no programa e mapeado assim:

- `1` = japones -> PaddleOCR `japan`
- `2` = coreano -> PaddleOCR `korean`
- `3` = ingles -> PaddleOCR `en`

Para confirmar que o pacote esta funcionando:

```powershell
python -c "from paddleocr import PaddleOCR; print('paddleocr ok')"
```

## RapidOCR experimental

RapidOCR e opcional e nao faz parte de `requirements.txt`. Instale-o somente
quando quiser testar o motor ONNX:

```powershell
pip install -r requirements-rapidocr.txt
```

O padrao continua sendo PaddleOCR:

```env
OCR_ENGINE=paddle
RAPIDOCR_ENABLED=False
```

Para ativar o modo hibrido experimental:

```env
OCR_ENGINE=rapidocr
RAPIDOCR_ENABLED=True
OCR_FALLBACK_ENGINE=paddle
OCR_HYBRID_FALLBACK=True
RAPIDOCR_MIN_CONFIDENCE=0.55
RAPIDOCR_SUSPICIOUS_TEXT_FALLBACK=True
RAPIDOCR_PAGE_FALLBACK=True
OCR_TEXT_REPAIR=True
OCR_TEXT_REPAIR_MODE=conservative
```

O RapidOCR retorna os mesmos boxes e coordenadas usados pelo restante do
pipeline. Paginas suspeitas passam primeiro pelo PaddleOCR mobile; somente
grupos ainda suspeitos usam o PaddleOCR completo. O reparo de texto e
conservador: ele corrige apenas repeticoes muito provaveis, como
`REALLY RFALLY` para `REALLY REALLY`, e registra original, resultado e motivo
nos JSONs.

Tambem e possivel ativar apenas nesta execucao:

```powershell
python test_pipeline_webtoon.py --url "<URL>" --max-images 20 --fast --benchmark --force --ocr-engine rapidocr
```

Para executar o capitulo completo do zero:

```powershell
python test_pipeline_webtoon.py --full --fast --benchmark --force --ocr-engine rapidocr
```

Para executar o capitulo completo usando cache e resume:

```powershell
python test_pipeline_webtoon.py --full --fast --benchmark --ocr-engine rapidocr
```

### Benchmark RapidOCR hibrido

Plus One, 9 paginas:

- PaddleOCR: 516,76s
- RapidOCR hibrido: 179,78s
- Reducao total: 65,21%
- Reducao OCR: 73,65%
- Linhas OCR: 46 em ambos
- Grupos traduzidos: 15 em ambos
- Erros: 0

Capitulo antigo, 20 paginas:

- PaddleOCR: 1.030,86s
- RapidOCR hibrido: 228,27s
- Reducao total: 77,86%
- Reducao OCR: 93,65%
- Grupos traduzidos: 19 em ambos
- Erros: 0

Capitulo antigo, 50 paginas:

- RapidOCR hibrido: 359,09s
- OCR: 105,25s
- PDF: 50 paginas
- Erros: 0

RapidOCR ainda e experimental. Para maxima qualidade e comportamento mais
conservador, use PaddleOCR.

## Como ativar NVIDIA

No `.env`:

```env
TRANSLATION_MODE=nvidia
NVIDIA_API_KEY=sua_chave_aqui
```

O tradutor NVIDIA usa a API compativel com OpenAI em:

```env
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_TRANSLATION_MODEL=nvidia/nemotron-3-super-120b-a12b
```

Ele envia os baloes em lotes de ate `NVIDIA_TRANSLATION_BATCH_SIZE` textos,
mantem os IDs e espera JSON. Se a API falhar, o projeto mantem os textos
originais para nao quebrar a geracao do PDF.

Por padrao, efeitos sonoros e textos decorativos isolados ficam fora da
traducao. Esse comportamento pode ser ajustado no `.env`:

```env
TRANSLATE_SFX=False
PRIORITIZE_ENCLOSED_TEXT=True
```

Com `TRANSLATE_SFX=True`, onomatopeias classificadas como `sfx` tambem podem
ser enviadas para traducao. A deteccao de baloes e caixas continua sendo
apenas apoio heuristico; a fonte principal permanece OCR-first.

Cada bloco reconhecido e classificado antes da traducao como `speech`,
`narration`, `sfx`, `decorative` ou `unknown`.

Para testar somente a traducao NVIDIA:

```powershell
python test_nvidia_translation.py
```

Para executar o teste visual controlado com 20 imagens:

```powershell
python test_pipeline_webtoon.py --max-images 20
```

Para uma verificacao mais rapida:

```powershell
python test_pipeline_webtoon.py --max-images 5 --fast
```

Depois de validar o teste controlado, o mesmo script pode processar todas as
imagens validas do capitulo:

```powershell
python test_pipeline_webtoon.py --full
```

O modo `--full` nao limita downloads. Ele gera debug, compares e um PDF
`debug\webtoon_full_NNN.pdf`. A interface normal tambem continua processando o
capitulo completo com `python main.py`.

## Performance e cache

O pipeline usa caches independentes para download, OCR, traducao e imagem
processada. A primeira execucao ainda demora porque precisa executar PaddleOCR
e chamar a NVIDIA. As execucoes seguintes podem reutilizar cada etapa e
continuar um capitulo interrompido pelo `progress.json`.

O modo `--fast` evita debug visual pesado. `TRANSLATE_SFX=False` preserva
onomatopeias e efeitos sonoros por padrao. A traducao NVIDIA pode usar dois
workers, com rate limit global e retry/backoff para erros temporarios.

O OCR paralelo usa dois processos quando ha memoria suficiente. Em maquinas
com pouca memoria, o pipeline faz fallback automatico para OCR sequencial,
evitando falhas ou perda de qualidade. Para desativar o paralelismo
manualmente, configure:

```env
OCR_PARALLEL=False
```

Para medir o capitulo completo do zero, ignorando todos os caches:

```powershell
python test_pipeline_webtoon.py --full --fast --benchmark --force
```

Para reutilizar cache e resume:

```powershell
python test_pipeline_webtoon.py --full --fast --benchmark
```

Use `--force` somente quando quiser medir uma execucao limpa. Sem `--force`,
downloads, OCRs, traducoes e paginas finais validas sao reaproveitados.

### Benchmark real - Webtoon Episode 1

- Imagens validas: 108
- Paginas no PDF: 108
- Linhas OCR: 367
- Grupos formados: 124
- Grupos traduzidos: 104
- SFX/decorative ignorados: 14
- Erros finais: 0

Versao funcional anterior:

- Tempo total: 35min29s
- Media: 19,72s/imagem

Versao otimizada:

- Tempo total forcado: 25min32s
- Media: 14,19s/imagem
- Reducao: 28,05%
- Gargalo: OCR
- OCR: 1.188s
- NVIDIA: 139,87s

Execucao com cache:

- Tempo total: 9,12s
- Media: 0,084s/imagem
- Chamadas NVIDIA: 0
- Reducao: 99,57%

## Como voltar para Google ou local

Para Google:

```env
TRANSLATION_MODE=google
```

Para HuggingFace/local:

```env
TRANSLATION_MODE=huggingface
NLLB_MODEL_DIR=C:\caminho\para\NLLB_200
```

O modo local precisa dos arquivos do modelo no caminho configurado.

## Como rodar

```powershell
.\.venv\Scripts\Activate.ps1
python main.py
```

O programa pedira:

- URL do capitulo.
- Nome da pasta/PDF de saida.
- Idioma original: `1` japones, `2` coreano, `3` ingles.
- Motor de traducao: NVIDIA, Google ou IA local.

## ChromeDriver no Windows

O downloader tenta:

1. Usar `CHROMEDRIVER_PATH` do `.env`, se existir e for valido.
2. Baixar/configurar via `webdriver-manager`.
3. Usar Selenium Manager automaticamente.

Se isso falhar, defina manualmente no `.env`:

```env
CHROMEDRIVER_PATH=C:\caminho\para\chromedriver.exe
```

## Tesseract opcional

Tesseract nao e mais o OCR principal. Para usa-lo como fallback, instale o
software do Tesseract no Windows, instale `pytesseract` no venv e configure:

```env
OCR_FALLBACK_ENGINE=tesseract
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

## Limitacoes conhecidas

- A primeira execucao do PaddleOCR pode baixar modelos e demorar mais.
- RapidOCR e experimental e pode exigir fallback para PaddleOCR em fontes
  estilizadas, nomes proprios ou textos com baixa confianca.
- A qualidade do OCR depende da resolucao, contraste, fonte e orientacao do
  texto original.
- Textos sem balao, muito estilizados ou sobrepostos a desenhos podem ser
  classificados como `decorative`, `sfx` ou `unknown` e ficar sem traducao.
- O fallback Tesseract exige o executavel do Tesseract instalado no Windows.
- A traducao NVIDIA exige internet, uma chave valida e respeita os limites da
  conta e de requisicoes por minuto.
- Sites podem alterar o HTML ou bloquear automacao, exigindo ajustes no
  downloader ou no ChromeDriver.
