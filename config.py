import os
import pytesseract

# Caminhos principais
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
CHROMEDRIVER_PATH = r"C:\Users\Henrique\Downloads\chromedriver-win64\chromedriver-win64\chromedriver.exe"
FONT_PATH = None  # exemplo: r"C:\Windows\Fonts\arial.ttf"

# Pastas temporárias
TEMP_FOLDER = "capitulo_temp"
TEMP_OUT = TEMP_FOLDER + "_out"
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(TEMP_OUT, exist_ok=True)

# Outros parâmetros
MAX_RETRIES_DOWNLOAD = 5
OCR_CONF_THRESHOLD = 15
