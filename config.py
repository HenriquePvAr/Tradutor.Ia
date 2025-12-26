import os
import pytesseract

# Caminho do Tesseract
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Caminho do ChromeDriver
CHROMEDRIVER_PATH = r"C:\Users\Henrique\Downloads\chromedriver-win64\chromedriver-win64\chromedriver.exe"

# Caminho da fonte (opcional)
FONT_PATH = None  # exemplo: r"C:\Windows\Fonts\arial.ttf"

# Pastas temporárias
TEMP_FOLDER = "capitulo_temp"
TEMP_OUT = TEMP_FOLDER + "_out"

# ⚠️ IMPORTANTE:
# NÃO criar pastas aqui, pois isso gera conflitos no Windows
# Elas serão criadas no down.py com limpeza segura

# Outros parâmetros
MAX_RETRIES_DOWNLOAD = 5
OCR_CONF_THRESHOLD = 15

# -------------------------------------------------------
# 🚀 Configurações do tradutor
# -------------------------------------------------------

# "google" → usar deep_translator
# "huggingface" → usar IA de tradução mais natural
TRANSLATION_MODE = "google"

# Modelo HuggingFace (caso escolha: TRANSLATION_MODE = "huggingface")
HF_MODEL = "Helsinki-NLP/opus-mt-mul-pt"
