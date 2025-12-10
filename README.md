Este projeto implementa uma solução completa de tradução automatizada para conteúdos visuais (como capítulos de mangás, HQs, ou webtoons) disponíveis em páginas web. O objetivo é extrair as imagens, identificar o texto nos balões de fala, traduzir e substituir o texto original pela tradução, e, por fim, gerar um PDF de saída. O projeto foi estruturado em módulos Python, permitindo flexibilidade na escolha do motor de tradução (online ou IA local).

⚙️ Módulos Principais e Funcionalidades
O pipeline é orquestrado pelo main.py e executado em etapas por módulos dedicados:

1 - config.py 
Função: Centraliza todas as variáveis de configuração obrigatórias e opcionais.
Conteúdo: Define os caminhos essenciais para softwares externos (pytesseract.tesseract_cmd, CHROMEDRIVER_PATH) e as configurações de pastas temporárias (TEMP_FOLDER, TEMP_OUT). Também controla o modo de tradução (TRANSLATION_MODE: "google" ou "huggingface").

2 - down.py
Função: Responsável pelo Web Scraping e download das imagens do capítulo.
Mecanismo: Utiliza Selenium com ChromeDriver para abrir a URL, simular o scroll infinito da página, encontrar elementos <img> e baixá-los para a pasta TEMP_FOLDER.

3- translator_nllb.py / translator.py
Função: Gerencia a tradução usando modelos de IA local.
Mecanismo: Se configurado para o modo HuggingFace, carrega o modelo NLLB-200 localmente usando PyTorch e a biblioteca Hugging Face Transformers.

4 - ocr_balloon.py
Função: O coração do processamento de imagem, tradução e redesenho.
Mecanismo: Detecção: Usa OpenCV para aplicar blur, thresholding e detecção de contornos brancos para isolar os balões de fala.
Extração: Executa o Tesseract OCR apenas nas regiões dos balões detectados.
Remoção: Utiliza Inpainting do OpenCV para remover o texto original.
Desenho: Redesenha a tradução no balão limpo, centralizando e ajustando a quebra de linha/tamanho de fonte usando PIL (Pillow).

5 - pdf.py
Função: Reúne todas as imagens processadas.
Mecanismo: Cria um único arquivo PDF de saída a partir da sequência de imagens traduzidas, utilizando a biblioteca PIL (Pillow).

🛠️ Pré-requisitos e Instalação
Para rodar o projeto, você precisará de softwares externos e bibliotecas Python específicas.
É obrigatório ter os seguintes programas instalados e configurados corretamente: Python V3.11, Tesseract OCR: O software de OCR deve estar instalado no seu sistema. Você deve fornecer o caminho exato do executável tesseract.exe na variável pytesseract.pytesseract.tesseract_cmd dentro de config.py., ChromeDriver: É o driver do navegador Google Chrome usado pelo Selenium. O caminho para o executável deve ser definido na variável CHROMEDRIVER_PATH em config.py.

Dependências Python : 
pip install numpy Pillow deep-translator
pip install opencv-python pytesseract
pip install selenium
pip install torch transformers

Configurações Específicas (IA Local) :
Se você optar pelo modo de tradução TRANSLATION_MODE = "huggingface" em config.py, você deve: 
Baixar o Modelo NLLB-200: O modelo (e seu tokenizer) deve ser baixado do Hugging Face e salvo localmente. 
Ajustar Caminho: O caminho para a pasta local do modelo (MODEL_DIR) precisa ser ajustado nos arquivos translator_nllb.py e/ou translator.py.

▶️ Execução : 
1- Verifique se todos os caminhos em config.py estão corretos.
2- Execute o arquivo principal: python main.py
3- O programa solicitará as seguintes informações via interface gráfica:
URL do capítulo: Link para a página web com o conteúdo.
Nome do PDF: Nome do arquivo de saída.
Idioma Original: Escolha 1 (Japonês), 2 (Coreano) ou 3 (Inglês).

O processo será acompanhado por uma janela de progresso e o PDF final será salvo na pasta de saída.

📅 Última Atualização e Status do Projeto
Última Atualização Realizada: 10/12/2025
Status: Em Andamento
