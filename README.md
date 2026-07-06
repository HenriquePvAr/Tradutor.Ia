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

## Comando simples para capitulos

`run_webtoon.py` executa o pipeline completo sem exigir os argumentos internos
do benchmark. O modo `fast` usa RapidOCR hibrido, fallback regional para
PaddleOCR e todas as validacoes de qualidade. O modo `quality` usa PaddleOCR,
que continua sendo o motor padrao e mais conservador.

```powershell
python run_webtoon.py "URL_DO_CAPITULO" --mode fast
python run_webtoon.py "URL_DO_CAPITULO" --mode quality
```

Para ignorar os caches de download, OCR, traducao e renderizacao:

```powershell
python run_webtoon.py "URL_DO_CAPITULO" --mode fast --force
```

Para reutilizar cache/resume ou escolher a pasta de saida:

```powershell
python run_webtoon.py "URL_DO_CAPITULO" --mode fast --cache
python run_webtoon.py "URL_DO_CAPITULO" --mode quality --output "meu_capitulo"
```

Sem argumentos, o script abre um modo interativo e pergunta URL, modo,
cache/force e nome da pasta. `--open-output` abre a pasta ao terminar. Para um
teste curto, use `--max-images 5`.

### Contexto temporario por capitulo

Por padrao, o runner cria:

```text
output/<nome_do_capitulo>/session_context.json
```

O arquivo e gerado dinamicamente a partir do OCR do capitulo e guarda possiveis
nomes proprios/personagens, termos recorrentes, traducoes ja usadas, regras de
preservacao e o estilo de portugues brasileiro natural para webtoon/manhwa.
Esse contexto compacto e enviado nos prompts da NVIDIA para manter nomes e
termos consistentes, sem hardcode de obras ou personagens.

- `--no-context`: nao cria nem usa contexto.
- `--keep-context`: mantem o JSON explicitamente (comportamento padrao).
- `--delete-context-after`: remove o JSON depois que o PDF for gerado.

Todo o contexto fica dentro de `output/`, que e ignorado pelo Git.

## Interface local

O Tradutor.Ia inclui uma interface web local para quem prefere processar
capitulos sem montar comandos no PowerShell. O frontend preserva o layout e as
animacoes da identidade visual oficial, enquanto o backend NiceGUI usa
`run_webtoon.py` por baixo: OCR, traducao, cache, contexto e validacoes
continuam no pipeline existente.

Instale somente as dependencias da interface:

```powershell
pip install -r requirements-ui.txt
```

Inicie o aplicativo:

```powershell
python app_ui.py
```

Depois abra [http://localhost:8080](http://localhost:8080). A interface tambem
escuta na rede local, portanto pode ser acessada pelo celular usando o IP do
computador e a porta `8080`, desde que o firewall permita a conexao.

Na aba **Nova traducao**:

- cole a URL; nome do capitulo e pasta de saida sao sugeridos automaticamente;
- escolha **Rapido** para RapidOCR hibrido com fallback Paddle e gate de
  qualidade;
- escolha **Qualidade** para usar PaddleOCR;
- selecione capitulo completo ou teste parcial com `3`, `5`, `20`, `50` ou
  outra quantidade de paginas;
- **Usar cache** reaproveita resultados validos e **Forcar reprocessamento**
  executa as etapas novamente;
- o contexto temporario fica ativado por padrao.

A aba **Fila** processa URLs reais em sequencia, uma por vez. A aba **Capitulos
traduzidos** usa o historico local `.cache/ui_history.json` e permite abrir PDF,
pasta, relatorio, compare sheet e `session_context.json`, alem de preparar uma
nova execucao. **Inicio** resume somente dados reais; **Comunidade** permanece
como area visual "em breve", sem postagens simuladas. **Configuracoes** mostra
versoes e disponibilidade reais sem revelar a chave NVIDIA. **Logs** recebe
stdout/stderr do subprocesso em tempo real e mascara tokens. O perfil local e
salvo em `.cache/ui_profile.json`. Avatar e banner aceitam PNG, JPG, WEBP, GIF,
MP4 e WEBM; os arquivos ficam somente em `.cache/ui_profile/`, nunca no Git.

Na tela inicial, traducoes tecnicas da mesma obra sao agrupadas pela URL real
em uma unica serie. A biblioteca de series oferece busca e ordenacao, e a
atividade recente usa exclusivamente o historico local real.

O HTML visual fica em `ui/ui_shell.html`, com CSS e JavaScript em `static/`.
`ui_bridge.py` faz a comunicacao entre o navegador e o Python por endpoints
locais. Nenhum progresso e inventado: quando o pipeline nao fornece contador,
a etapa aparece como indeterminada ate surgir um valor real.

Os capitulos permanecem em `output/<nome_do_capitulo>/`. Tanto `output/`
quanto `.cache/` sao ignorados pelo Git.

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

## Camada de qualidade do RapidOCR

RapidOCR continua sendo um motor opcional e experimental; PaddleOCR permanece
o padrao mais seguro. No modo rapido, o pipeline tenta RapidOCR primeiro. Se o
texto ou os boxes parecerem suspeitos, somente a regiao afetada passa pelo
PaddleOCR Mobile. Se a qualidade ainda for insuficiente, a mesma regiao usa o
PaddleOCR completo.

Os reparos OCR sao genericos e conservadores. Eles usam confianca, distancia
de edicao, vocabulario, contexto e concordancia entre motores, sem traducoes
prontas ou regras de frases especificas. A resposta da NVIDIA tambem e
validada para impedir texto vazio ou mistura indevida de portugues e ingles.

Antes de aceitar uma pagina, o pipeline valida a mascara, a area segura e a
imagem renderizada. Alteracoes fora da regiao permitida, manchas, borroes,
dano ao contorno do balao e texto fora do balao causam rollback seletivo do
grupo. SFX e textos decorativos continuam preservados com
`TRANSLATE_SFX=False`.

Grupos que nao podem ser redesenhados com seguranca sao enviados para a
auditoria Categoria A/B:

- Categoria A: SFX, decorativo, nome proprio ou texto cuja remocao danificaria
  a arte; pode permanecer original.
- Categoria B: fala, pensamento ou narracao importante; deve ser corrigido
  antes da aprovacao do gate.

Comando recomendado para um capitulo completo em modo rapido:

```powershell
python test_pipeline_webtoon.py --url "URL_DO_CAPITULO" --full --fast --benchmark --force --ocr-engine rapidocr
```

### Benchmark Lookism EP 50

- Paginas processadas/PDF: 81/81
- Tempo total: 8min11s
- Media: 6,06s/pagina
- Grupos traduzidos: 185
- SFX preservados: 25
- Paginas vazias ou corrompidas: 0
- Falhas visuais graves: 0
- Texto misturado ou fora da regiao: 0
- Gate global: aprovado
- Testes de regressao: 19 aprovados

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
