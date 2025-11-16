# ocr_balloon.py
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytesseract
import textwrap
import os

# Se quiser, leia FONT_PATH do seu config.py
try:
    from config import FONT_PATH, TEMP_FOLDER, TEMP_OUT
except Exception:
    FONT_PATH = None
    TEMP_FOLDER = "capitulo_temp"
    TEMP_OUT = TEMP_FOLDER + "_out"

os.makedirs(TEMP_OUT, exist_ok=True)


def detect_balloons_contours(img_bgr, min_area=2000):
    """
    Detecta regiões brancas arredondadas (balões) por threshold + contornos.
    Retorna lista de bounding boxes (x,y,w,h).
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Blur para unir letras e reduzir ruído
    blur = cv2.GaussianBlur(gray, (5, 5), 0)

    # Threshold adaptativo para pegar regiões claras (balões)
    _, th = cv2.threshold(blur, 200, 255, cv2.THRESH_BINARY)

    # Dilation para unir bordas do balão
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    dil = cv2.dilate(th, kernel, iterations=2)

    # Encontrar contornos
    contours, _ = cv2.findContours(dil, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        # Filtrar por área e proporção: evitar banners e imagens grandes
        if area >= min_area and w > 40 and h > 20 and w/h < 6:
            boxes.append((x, y, w, h))

    # Ordena por y (topo->base)
    boxes = sorted(boxes, key=lambda b: b[1])
    return boxes


def extract_text_from_box(img_bgr, box, ocr_lang):
    x, y, w, h = box
    crop = img_bgr[y:y+h, x:x+w]
    # Pré-process: transformar em cinza e threshold para melhorar OCR
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    # Se texto for escuro em fundo claro, invert not necessary; use adaptive threshold
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY, 11, 2)
    # PSM 6 (assume um bloco de texto)
    config = "--psm 6"
    text = pytesseract.image_to_string(th, lang=ocr_lang, config=config)
    return text.strip()


def remove_text_in_box(img_bgr, box):
    """
    Faz inpainting na região onde estava o texto para remover restos.
    """
    x, y, w, h = box
    crop = img_bgr[y:y+h, x:x+w]

    # Detecta texto/preto no crop (assume texto escuro)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)  # texto -> branco na mask
    # dilata para cobrir restos
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3,3))
    mask = cv2.dilate(mask, kernel, iterations=2)

    # Inpaint (Telea é rápido)
    inpainted = cv2.inpaint(crop, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)

    # Suaviza transição
    inpainted = cv2.medianBlur(inpainted, 3)

    # coloca de volta
    img_bgr[y:y+h, x:x+w] = inpainted
    return img_bgr


def draw_translated_in_box(img_bgr, box, translated_text, font_path=None, font_scale_hint=36):
    """
    Desenha o texto traduzido centralizado no box,
    com quebra automática.
    """
    x, y, w, h = box
    pil = Image.fromarray(img_bgr)
    draw = ImageDraw.Draw(pil)

    # Escolhe fonte
    if font_path and os.path.exists(font_path):
        # Ajusta tamanho dinamicamente -> tentaremos fit
        try:
            font_size = max(12, int(min(w, h) / 8))
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = ImageFont.load_default()
    else:
        try:
            font = ImageFont.truetype("arial.ttf", 24)
        except Exception:
            font = ImageFont.load_default()

    # Quebra automática: reduz até caber
    max_width = w - 12
    lines = []
    words = translated_text.split()
    if not words:
        words = [""]

    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)

    # Se linhas muito altas, reduz font_size e refaz
    total_h = sum((draw.textbbox((0,0), line, font=font)[3] - draw.textbbox((0,0), line, font=font)[1]) for line in lines) + (len(lines)-1)*6
    while total_h > h - 8 and isinstance(font, ImageFont.FreeTypeFont):
        # diminuir tamanho
        size = font.size if hasattr(font, "size") else 20
        size = max(10, int(size * 0.9))
        try:
            font = ImageFont.truetype(font.path, size)  # may fail if font has no .path
        except Exception:
            font = ImageFont.truetype("arial.ttf", max(10, size))
        total_h = sum((draw.textbbox((0,0), line, font=font)[3] - draw.textbbox((0,0), line, font=font)[1]) for line in lines) + (len(lines)-1)*6
        if size <= 10:
            break

    # centraliza verticalmente
    y0 = y + max(4, (h - total_h) // 2)

    for line in lines:
        line_w = draw.textlength(line, font=font)
        x0 = x + max(6, (w - line_w) // 2)
        # pintar com leve sombra para legibilidade
        draw.text((x0+1, y0+1), line, font=font, fill=(255,255,255))
        draw.text((x0, y0), line, font=font, fill=(0,0,0))
        y0 += (draw.textbbox((0,0), line, font=font)[3] - draw.textbbox((0,0), line, font=font)[1]) + 6

    return np.array(pil)


def process_image_file(image_path, ocr_lang, translator_func, font_path=None, save_out=True):
    """
    Faz todo o processo em uma única imagem:
    - detecta balões
    - extrai texto (OCR por caixa)
    - traduz (via translator_func(text) -> string)
    - remove texto (inpaint)
    - escreve tradução centralizada
    Retorna caminho do arquivo de saída (ou None).
    """
    img = cv2.imread(image_path)
    if img is None:
        print(f"[ERR] Não abriu: {image_path}")
        return None

    boxes = detect_balloons_contours(img, min_area=1200)
    if not boxes:
        # Fallback: tentar OCR do frame inteiro
        txt = pytesseract.image_to_string(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        if not txt.strip():
            print(f"[WARN] Nenhum balão detectado e nenhum texto no todo: {image_path}")
            return None
        # traduz e sobrescreve toda a imagem (simples fallback)
        translated = translator_func(txt)
        out_img = draw_translated_in_box(img, (0,0,img.shape[1], img.shape[0]), translated, font_path=font_path)
        out_path = image_path.replace(TEMP_FOLDER, TEMP_OUT)
        if save_out:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            cv2.imwrite(out_path, out_img)
        return out_path

    # Para cada balão: extrai texto → traduz → remove original → escreve tradução
    for box in boxes:
        text = extract_text_from_box(img, box, ocr_lang)
        if not text:
            continue
        try:
            translated = translator_func(text)
        except Exception as e:
            print(f"[WARN] Falha tradução: {e}")
            translated = text

        # remove texto original
        img = remove_text_in_box(img, box)
        # desenha o traduzido
        img = draw_translated_in_box(img, box, translated, font_path=font_path)

    out_path = image_path.replace(TEMP_FOLDER, TEMP_OUT)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, img)
    return out_path
