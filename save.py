import os
import time
import base64
import requests
import tkinter as tk
from tkinter import simpledialog, messagebox, ttk
from PIL import Image, ImageDraw, ImageFont
import pytesseract
from deep_translator import GoogleTranslator
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import cv2

# ---------------- CONFIG ----------------
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
FONT_PATH = None  # opcional: r"C:\Windows\Fonts\arial.ttf"
TEMP_FOLDER = "capitulo_temp"
TEMP_OUT = TEMP_FOLDER + "_out"
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(TEMP_OUT, exist_ok=True)

CHROMEDRIVER_PATH = r"C:\Users\Henrique\Downloads\chromedriver-win64\chromedriver-win64\chromedriver.exe"
MAX_RETRIES_DOWNLOAD = 5
OCR_CONF_THRESHOLD = 15  # reduzido para capturar mais palavras

# ---------------- FUNÇÕES ----------------
def preprocess_for_ocr(img_path):
    img_cv = cv2.imread(img_path)
    if img_cv is None:
        return None
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 11, 2)
    th = cv2.medianBlur(th, 3)
    pre_path = img_path.replace(".png", "_pre.png")
    cv2.imwrite(pre_path, th)
    return pre_path

def download_images(url, progress_callback=None, max_retries=3):
    tmp_files = []
    failed_candidates = []

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(service=Service(CHROMEDRIVER_PATH), options=chrome_options)

    driver.get(url)
    time.sleep(3)

    # Scroll até o final da página
    last_height = driver.execute_script("return document.body.scrollHeight")
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height

    # Captura todas as imagens
    imgs = driver.find_elements(By.TAG_NAME, "img")
    candidates = []
    for img in imgs:
        try:
            w, h = img.size.get("width", 0), img.size.get("height", 0)
            loc = img.location
            src = img.get_attribute("src") or ""
            if w >= 200 and h >= 200:
                candidates.append({"el": img, "y": loc.get("y", 0), "src": src})
        except: 
            pass
    candidates = sorted(candidates, key=lambda x: x["y"])

    fetch_blob_script = """
    var img = arguments[0];
    var callback = arguments[1];
    var url = img.src;
    fetch(url).then(r => r.blob()).then(function(b){
        var reader = new FileReader();
        reader.onloadend = function() { callback(reader.result.split(',')[1]); };
        reader.readAsDataURL(b);
    }).catch(function(e){ callback(null); });
    """

    def save_bytes(bts, path):
        with open(path, "wb") as f:
            f.write(bts)

    def try_save(el, src, path):
        saved = False
        if src.startswith("http"):
            try:
                r = requests.get(src, headers={"User-Agent":"Mozilla/5.0"}, timeout=15)
                if r.status_code == 200:
                    save_bytes(r.content, path)
                    saved = True
            except: pass
        if not saved:
            try:
                b64 = driver.execute_async_script(fetch_blob_script, el)
                if b64:
                    save_bytes(base64.b64decode(b64), path)
                    saved = True
            except: pass
        if not saved:
            try:
                el.screenshot(path)
                saved = True
            except: pass
        return saved

    # Salva imagens
    for i, c in enumerate(candidates, start=1):
        path = os.path.join(TEMP_FOLDER, f"{i:03}.png")
        if try_save(c["el"], c["src"], path):
            tmp_files.append(path)
        else:
            failed_candidates.append((i, c))
        if progress_callback:
            progress_callback(i, len(candidates), "Baixando imagens")

    # Retry imagens falhadas
    for attempt in range(1, max_retries+1):
        if not failed_candidates:
            break
        still_failed = []
        for i, c in failed_candidates:
            path = os.path.join(TEMP_FOLDER, f"{i:03}.png")
            if try_save(c["el"], c["src"], path):
                tmp_files.append(path)
            else:
                still_failed.append((i, c))
            if progress_callback:
                progress_callback(i, len(candidates), f"Tentativa {attempt} de recuperação")
        failed_candidates = still_failed

    tmp_files = sorted(tmp_files)
    driver.quit()
    return tmp_files

def ocr_translate_balloon(img_path, ocr_lang, translator, font_path=None, font_size=36):
    try:
        pre_path = preprocess_for_ocr(img_path)
        if not pre_path:
            return None
        img = Image.open(img_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()

        # OCR com dados completos
        data = pytesseract.image_to_data(pre_path, output_type=pytesseract.Output.DICT, lang=ocr_lang)
        blocks = {}
        for i, word in enumerate(data["text"]):
            if word.strip():
                block_num = data["block_num"][i]
                if block_num not in blocks:
                    blocks[block_num] = {"words": [], "coords": []}
                blocks[block_num]["words"].append(word)
                blocks[block_num]["coords"].append((data["left"][i], data["top"][i], data["width"][i], data["height"][i]))

        if not blocks:
            # fallback OCR completo
            full_text = pytesseract.image_to_string(pre_path, lang=ocr_lang)
            if full_text.strip():
                translated_text = translator.translate(full_text)
                draw.rectangle([0,0,img.width,img.height], fill=(255,255,255))
                draw.text((10,10), translated_text, font=font, fill=(0,0,0))
            out_path = img_path.replace(TEMP_FOLDER, TEMP_OUT)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            img.save(out_path)
            return out_path

        # Processa cada bloco separadamente
        for block in blocks.values():
            texto_original = " ".join(block["words"])
            x_min = min(c[0] for c in block["coords"])
            y_min = min(c[1] for c in block["coords"])
            x_max = max(c[0]+c[2] for c in block["coords"])
            y_max = max(c[1]+c[3] for c in block["coords"])

            try:
                texto_trad = translator.translate(texto_original)
            except:
                texto_trad = texto_original

            draw.rectangle([x_min, y_min, x_max, y_max], fill=(255,255,255))

            # Quebra texto em linhas
            palavras = texto_trad.split()
            linhas = []
            linha = ""
            max_width = x_max - x_min - 4
            for palavra in palavras:
                teste = linha + " " + palavra if linha else palavra
                w_text, h_text = draw.textbbox((0,0), teste, font=font)[2:]
                if w_text <= max_width:
                    linha = teste
                else:
                    linhas.append(linha)
                    linha = palavra
            if linha:
                linhas.append(linha)

            # Desenha centralizado no bloco
            total_h = sum(draw.textbbox((0,0), l, font=font)[3] for l in linhas)
            y_text = y_min + (y_max - y_min - total_h) / 2
            for l in linhas:
                w_text, h_text = draw.textbbox((0,0), l, font=font)[2:]
                x_text = x_min + (x_max - x_min - w_text) / 2
                draw.text((x_text, y_text), l, font=font, fill=(0,0,0))
                y_text += h_text

        out_path = img_path.replace(TEMP_FOLDER, TEMP_OUT)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        img.save(out_path)
        return out_path
    except Exception as e:
        print(f"Erro OCR/traducao: {img_path} -> {e}")
        return None

def generate_pdf(image_paths, pdf_path):
    pil_imgs = [Image.open(p).convert("RGB") for p in image_paths]
    if pil_imgs:
        first, rest = pil_imgs[0], pil_imgs[1:]
        first.save(pdf_path, save_all=True, append_images=rest)
        print(f"✅ PDF gerado: {pdf_path}")

class ProgressWindow:
    def __init__(self, title="Progresso"):
        self.root = tk.Toplevel()
        self.root.title(title)
        self.root.geometry("400x100")
        self.label = tk.Label(self.root, text="")
        self.label.pack(pady=10)
        self.progress = ttk.Progressbar(self.root, length=350, mode="determinate")
        self.progress.pack(pady=10)
        self.root.update()

    def update(self, value, maximum, text=""):
        self.progress["maximum"] = maximum
        self.progress["value"] = value
        self.label.config(text=text)
        self.root.update()

    def close(self):
        self.root.destroy()

# ---------------- MAIN ----------------
def main():
    root = tk.Tk()
    root.withdraw()

    url = simpledialog.askstring("Capítulo", "Cole a URL do capítulo:")
    if not url: return
    folder_name = simpledialog.askstring("Saída", "Nome da pasta/PDF de saída:") or "capitulo_traduzido"
    output_folder = os.path.join(os.getcwd(), folder_name)
    os.makedirs(output_folder, exist_ok=True)

    lang_choice = simpledialog.askstring("Idioma", "1:Japonês 2:Coreano 3:Inglês")
    if lang_choice=="1": ocr_lang="jpn"; translator_source="ja"
    elif lang_choice=="2": ocr_lang="kor"; translator_source="ko"
    else: ocr_lang="eng"; translator_source="en"

    translator = GoogleTranslator(source=translator_source, target="pt")

    prog_win = ProgressWindow("Baixando e traduzindo capítulo")

    saved_files = download_images(url, progress_callback=lambda v,m,t: prog_win.update(v,m,t))
    if not saved_files:
        prog_win.close()
        messagebox.showerror("Erro","Nenhuma imagem salva!")
        return

    processed_files = []
    total = len(saved_files)
    for idx, img in enumerate(saved_files, start=1):
        out = ocr_translate_balloon(img, ocr_lang, translator, FONT_PATH)
        if out: processed_files.append(out)
        prog_win.update(idx, total, "Traduzindo imagens")

    prog_win.close()

    if not processed_files:
        messagebox.showerror("Erro","Nenhuma imagem processada!")
        return

    pdf_path = os.path.join(output_folder, folder_name+".pdf")
    generate_pdf(processed_files, pdf_path)
    messagebox.showinfo("Concluído", f"PDF gerado: {pdf_path}")

if __name__ == "__main__":
    main()
