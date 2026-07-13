# Instalação

Este guia prepara o ambiente local do Tradutor.IA no cenário atualmente auditado: Windows 64 bits, Python 3.11 e execução com Chrome.

> [Voltar ao README](../README.md)

## Pré-requisitos

| Componente | Requisito | Observação |
| --- | --- | --- |
| Sistema operacional | Windows 11, 64 bits | É a plataforma validada pelo projeto |
| Python | 3.11, 64 bits | O ambiente atual foi auditado com Python 3.11.9 |
| Git | Versão recente | Usado para clonar e atualizar o repositório |
| Google Chrome | Versão recente | Necessário para a coleta via Selenium |
| Memória | 16 GB recomendados | PaddleOCR pode elevar bastante o uso de RAM e memória virtual |
| Disco | Espaço para modelos, caches e outputs | Capítulos completos podem gerar muitos arquivos intermediários |
| NVIDIA API | Chave válida | Necessária para o provedor de tradução padrão |

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
- `requirements-rapidocr.txt`: RapidOCR e ONNX Runtime usados pelo modo `fast`;
- `requirements-ui.txt`: NiceGUI para a interface local.

Se você pretende usar somente o modo `quality` pela CLI, RapidOCR não é obrigatório. Se pretende usar somente a CLI, NiceGUI também é opcional. Para o fluxo completo recomendado, instale os três conjuntos.

## 4. Criar a configuração local

```powershell
Copy-Item .env.example .env
```

Abra `.env` e substitua apenas o placeholder da chave:

```dotenv
NVIDIA_API_KEY=sua_chave_real
```

Não versione `.env`, não cole a chave em comandos e não a inclua em relatórios. Os demais defaults são documentados em [Configuração](CONFIGURATION.md).

## 5. Modelos de OCR

PaddleOCR é instalado pelo arquivo principal de requisitos. Na primeira inicialização de uma variante de modelo, a biblioteca pode buscar os arquivos oficiais correspondentes. O código usa:

- PaddleOCR completo no modo `quality` e em fallbacks de maior qualidade;
- `PP-OCRv4_mobile_det` e reconhecimento mobile nos fallbacks leves;
- RapidOCR/ONNX Runtime no modo `fast`.

Planeje a primeira execução com conexão disponível e espaço em disco. O projeto não exige que modelos sejam copiados manualmente para uma pasta interna do repositório.

Tesseract está presente apenas como caminho opcional de compatibilidade. Ele não é o OCR principal e não precisa ser instalado para os modos documentados no README.

## 6. Chrome e ChromeDriver

O downloader inicia o Chrome em modo headless. A resolução do driver segue esta ordem:

1. `CHROMEDRIVER_PATH`, quando configurado e válido;
2. `webdriver-manager`;
3. Selenium Manager, como alternativa automática.

Na maioria dos ambientes basta ter o Chrome atualizado. Se a detecção automática falhar, defina um caminho genérico no `.env`:

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

O modo `fast` seleciona RapidOCR com salvaguardas e fallback Paddle. Para iniciar diretamente com PaddleOCR:

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
