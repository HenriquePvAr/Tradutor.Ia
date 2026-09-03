# Instalação

Este guia prepara o ambiente local do Tradutor.IA no cenário atualmente auditado: Windows 64 bits e Python 3.11.

> [Voltar ao README](../README.md) · [Desenvolvimento](DEVELOPMENT.md)
>
> Para setup de desenvolvimento, comandos de teste, dependências externas por tipo e riscos
> conhecidos, use [Desenvolvimento](DEVELOPMENT.md). Este guia cobre a instalação em si.

## Pré-requisitos

| Componente | Requisito | Observação |
| --- | --- | --- |
| Sistema operacional | Windows 11, 64 bits | É a plataforma validada pelo projeto |
| Python | 3.11, 64 bits | O ambiente atual foi auditado com Python 3.11.9 |
| Git | Versão recente | Usado para clonar e atualizar o repositório |
| Google Chrome | Versão recente | **Opcional/fallback.** A descoberta de fonte é HTTP-first; o Chrome só é iniciado quando esse caminho não se aplica à fonte |
| Node.js | 24 | **Somente desenvolvimento**: suítes de frontend `.mjs` e serviço Better Auth. O pipeline não precisa dele |
| Memória | 16 GB recomendados | Modelos de OCR e reconstrução podem elevar bastante o uso de RAM e memória virtual |
| Disco | Espaço para modelos, caches e outputs | Capítulos completos podem gerar muitos arquivos intermediários |
| Chave do provedor de tradução | `DEEPL_API_KEY` | DeepL é o provedor de tradução padrão. NVIDIA (Riva/Nemotron) só é necessária se você selecionar esses providers |

O suporte end-to-end de Linux e macOS ainda não foi validado. O launcher possui um caminho POSIX para grupos de processos, mas isso não equivale a suporte integral do pipeline nessas plataformas.

## 1. Clonar o repositório

```powershell
git clone https://github.com/HenriquePvAr/Tradutor.Ia.git
cd Tradutor.Ia
```

## 2. Criar o ambiente virtual

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Se a política do PowerShell impedir a ativação, não é necessário alterar a configuração global do Windows. Use diretamente o executável do ambiente:

```powershell
.\.venv\Scripts\python.exe --version
```

Nos comandos seguintes, substitua `python` por `.\.venv\Scripts\python.exe` e `pip` por `.\.venv\Scripts\python.exe -m pip`.

## 3. Instalar as dependências

O projeto separa o núcleo das dependências de RapidOCR e da interface:

```powershell
pip install -r requirements.txt
pip install -r requirements-rapidocr.txt
pip install -r requirements-ui.txt
pip install -r requirements-dev.txt
```

Os arquivos têm responsabilidades diferentes:

- `requirements.txt`: processamento de imagem, Selenium, PaddleOCR, tradução e monitoramento;
- `requirements-rapidocr.txt`: RapidOCR e ONNX Runtime usados como OCR primário do fluxo Beta
  nos modos `fast` e `quality`;
- `requirements-ui.txt`: NiceGUI para a interface local.

**OpenCV — instale exatamente uma variante.** O projeto requer `opencv-python`, fixado em
`requirements.txt` na versão auditada. Nenhum módulo `contrib` é usado, então
`opencv-contrib-python`, `opencv-python-headless` e `opencv-contrib-python-headless` não
acrescentam nada e, instalados no mesmo ambiente, apenas criam um segundo `cv2` no
`sys.path` cuja versão depende da ordem dos diretórios. Se `python -m pip list | findstr
opencv` mostrar mais de uma linha, desinstale as variantes extras antes de confiar em
qualquer resultado de qualidade. Ver `OPENCV-THRESHOLD-SENSITIVITY-001` em
[Qualidade e validação](QUALITY_AND_VALIDATION.md).

> **Inconsistência aberta, não resolvida.** No ambiente de desenvolvimento que produziu a
> baseline atual as três variantes coexistem, e foi observado que o `cv2` efetivamente
> importado vem do site-packages **do usuário** — onde está a variante `-headless` — e não do
> pacote declarado como contratual. A suíte passa inteira porque as duas distribuições
> 5.0.0.93 expõem a mesma versão de `cv2`; isso não é evidência de que o contrato está sendo
> respeitado em runtime. Registrado como `OPENCV-VARIANT-SHADOWING-001` em
> [Desenvolvimento](DEVELOPMENT.md#riscos-conhecidos). Verifique com:
>
> ```powershell
> python -m pip list | Select-String opencv
> python -c "import cv2; print(cv2.__version__, cv2.__file__)"
> ```

Para o fluxo completo recomendado, instale os três conjuntos. Se pretende usar somente a CLI,
NiceGUI é opcional. PaddleOCR continua útil para fallbacks de maior qualidade, mas não é
requisito para executar o modo `quality` quando RapidOCR está disponível.

## 4. Criar a configuração local

```powershell
Copy-Item .env.example .env
```

Abra `.env` e substitua o placeholder da chave do provedor de tradução padrão:

```dotenv
DEEPL_API_KEY=<sua-chave>
```

`NVIDIA_API_KEY` só é necessária se você selecionar os providers Riva ou Nemotron.

Não versione `.env`, não cole a chave em comandos e não a inclua em relatórios. Os demais defaults são documentados em [Configuração](CONFIGURATION.md).

## 5. Modelos de OCR

PaddleOCR é instalado pelo arquivo principal de requisitos. Na primeira inicialização de uma variante de modelo, a biblioteca pode buscar os arquivos oficiais correspondentes. O código usa:

- RapidOCR/ONNX Runtime como OCR primário dos modos `fast` e `quality`;
- recuperação regional limitada, também com RapidOCR, para regiões duvidosas;
- PaddleOCR apenas como compatibilidade legacy, opt-in por `OCR_LEGACY_PADDLE_FALLBACK`
  (desligado por padrão) — ele não roda automaticamente em regiões suspeitas;

Planeje a primeira execução com conexão disponível e espaço em disco. O projeto não exige que modelos sejam copiados manualmente para uma pasta interna do repositório.

Tesseract está presente apenas como caminho opcional de compatibilidade. Ele não é o OCR principal e não precisa ser instalado para os modos documentados no README.

## 6. Chrome e ChromeDriver

O Chrome é o **caminho de fallback**, não o principal. Para fontes cujo adapter suporta
descoberta por HTTP, o pipeline resolve as páginas com um GET limitado e sem cookies e não
inicia navegador nenhum. Consulte
[Arquitetura § Descoberta de fonte](ARCHITECTURE.md#descoberta-de-fonte).

> Problema conhecido em máquina de desenvolvimento: em sistemas afetados por problemas de
> Microsoft Platform Crypto Provider / TPM, o Chrome pode falhar ao iniciar e inutilizar o
> fallback. A descoberta HTTP evita essa dependência para as fontes que a suportam.

Quando o fallback é usado, o downloader inicia o Chrome em modo headless. A resolução do
driver segue esta ordem:

1. `CHROMEDRIVER_PATH`, quando configurado e válido;
2. `chromedriver` ou `chromedriver.exe` já disponível no `PATH`;
3. Selenium Manager oficial, cacheado pelo Selenium, quando nenhum driver local existe.

Essa terceira etapa evita que uma instalação Windows limpa dependa de download manual de
ChromeDriver. Em testes herméticos, a resolução automática fica desligada por padrão; use
`TRADUTOR_ALLOW_DRIVER_DOWNLOAD=1` somente quando quiser permitir esse resolvedor no teste.
Se a resolução automática falhar, defina um caminho genérico no `.env`:

```dotenv
CHROMEDRIVER_PATH=C:\ferramentas\chromedriver.exe
```

Não copie caminhos pessoais da documentação para sua máquina; use o local real da sua instalação.

## 7. Verificar a instalação

Os testes abaixo são locais e não baixam capítulos:

```powershell
python -m py_compile run_webtoon.py app_ui.py process_launcher.py
python test_run_webtoon.py
python test_process_launcher.py
python test_ocr_quality_regressions.py
```

Para uma verificação rápida das bibliotecas:

```powershell
python -c "import cv2, paddle, PIL, selenium; print('dependências principais: OK')"
python -c "from rapidocr_onnxruntime import RapidOCR; print('RapidOCR: OK')"
python -c "import nicegui; print('NiceGUI: OK')"
```

Esses comandos confirmam importação e testes offline; não validam a chave de tradução nem o acesso ao site de origem.

## 8. Primeira inicialização

### Interface local

```powershell
python app_ui.py
```

Abra `http://127.0.0.1:8080`. A UI valida a existência do `.env` e da `NVIDIA_API_KEY` antes de iniciar um processamento.

### Linha de comando

```powershell
python run_webtoon.py "<URL_DO_CAPITULO>" --mode fast --no-context
```

RapidOCR é o engine primário nos dois modos. O modo `fast` mantém os fallbacks pesados
desativados; o modo `quality` habilita os caminhos de recuperação e validação mais caros,
incluindo validação de OCR pós-render:

```powershell
python run_webtoon.py "<URL_DO_CAPITULO>" --mode quality --no-context
```

Use somente conteúdo que você tenha autorização para processar. Para uma primeira experiência, acompanhe o consumo de memória e os relatórios de saída.

## 9. Launcher supervisionado

Para execuções longas iniciadas fora da UI, o repositório inclui `process_launcher.py`. Ele mantém stdout e stderr separados, persiste o exit code e controla a árvore de processos no Windows. Um exemplo completo está em [Arquitetura](ARCHITECTURE.md#launcher-supervisionado).

O launcher PowerShell inline usado em experimentos antigos não faz parte do fluxo documentado e não deve ser usado para capturar o exit code.

## Próximos passos

- revise as opções em [Configuração](CONFIGURATION.md);
- entenda os estados em [Qualidade e validação](QUALITY_AND_VALIDATION.md);
- consulte [Troubleshooting](TROUBLESHOOTING.md) se algum componente não carregar.
