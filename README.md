# Tradutor.Ia

Tradutor.Ia baixa imagens de paginas de manga, HQ ou webtoon, detecta baloes,
faz OCR, traduz o texto para portugues do Brasil, redesenha a traducao nos
baloes e gera um PDF final.

## Principais modos

- OCR principal: PaddleOCR.
- OCR fallback opcional: Tesseract, somente se `pytesseract` e o Tesseract do
  Windows estiverem configurados.
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
OCR_FALLBACK_ENGINE=tesseract
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
- A qualidade do OCR depende da resolucao, contraste, fonte e orientacao do
  texto original.
- Textos sem balao, muito estilizados ou sobrepostos a desenhos podem ser
  classificados como `decorative`, `sfx` ou `unknown` e ficar sem traducao.
- O fallback Tesseract exige o executavel do Tesseract instalado no Windows.
- A traducao NVIDIA exige internet, uma chave valida e respeita os limites da
  conta e de requisicoes por minuto.
- Sites podem alterar o HTML ou bloquear automacao, exigindo ajustes no
  downloader ou no ChromeDriver.
