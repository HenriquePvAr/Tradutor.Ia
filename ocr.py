

import os, cv2
from PIL import Image, ImageDraw, ImageFont
import pytesseract
from config import TEMP_FOLDER, TEMP_OUT
import textwrap

def preprocess_for_ocr(img_path):
    img_cv = cv2.imread(img_path)
    if img_cv is None:
        return None

    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)

    th = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 11, 2
    )

    th = cv2.medianBlur(th, 3)

    pre_path = img_path.replace(".png", "_pre.png")
    cv2.imwrite(pre_path, th)
    return pre_path


def fit_text_in_box(draw, text, font_path, box_w, box_h,
                    max_font_size=48, min_font_size=10):

    text = str(text)
    # diminui quebra baseado no tamanho do box
    wrap_width = max(10, int(box_w / 12))

    for font_size in range(max_font_size, min_font_size - 1, -1):
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()

        lines = textwrap.wrap(text, width=wrap_width)
        if not lines:
            continue

        total_h = sum(
            draw.textbbox((0, 0), line, font=font)[3]
            - draw.textbbox((0, 0), line, font=font)[1]
            for line in lines
        )

        max_line_w = max(
            draw.textlength(line, font=font)
            for line in lines
        )

        if max_line_w <= box_w and total_h <= box_h:
            return font, lines

    # fallback mínimo
    font = ImageFont.truetype(font_path, min_font_size) if font_path else ImageFont.load_default()
    lines = textwrap.wrap(text, width=wrap_width)
    return font, lines


def draw_text_with_shadow(draw, x, y, text, font,
                          fill=(0, 0, 0),
                          shadow_color=(255, 255, 255),
                          offset=2):

    # sombra ao redor
    for dx in range(-offset, offset + 1):
        for dy in range(-offset, offset + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=shadow_color)

    draw.text((x, y), text, font=font, fill=fill)


def ocr_translate_balloon(img_path, ocr_lang, translator, font_path=None):
    pre_path = preprocess_for_ocr(img_path)
    if not pre_path:
        return None

    img = Image.open(img_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        data = pytesseract.image_to_data(
            pre_path,
            output_type=pytesseract.Output.DICT,
            lang=ocr_lang
        )
    except Exception:
        return None

    n_boxes = len(data["text"])

    for i in range(n_boxes):
        text = data["text"][i]
        if not text or not text.strip():
            continue

        text = text.strip()

        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])

        # Segurança: ignora caixas muito pequenas (ruído)
        if w < 15 or h < 15:
            continue

        # tradução com fallback
        try:
            translated = translator.translate(text) or text
        except:
            translated = text

        translated = str(translated)

        # limpa o balão
        draw.rectangle([x, y, x + w, y + h], fill=(255, 255, 255))

        # ajusta fonte
        font, lines = fit_text_in_box(draw, translated, font_path, w, h)

        # mede altura total
        line_heights = [
            draw.textbbox((0, 0), line, font=font)[3]
            - draw.textbbox((0, 0), line, font=font)[1]
            for line in lines
        ]
        total_h = sum(line_heights)

        current_y = y + (h - total_h) / 2

        for idx_line, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            line_w = bbox[2] - bbox[0]

            tx = x + (w - line_w) / 2

            draw_text_with_shadow(draw, tx, current_y, line, font)
            current_y += line_heights[idx_line]

    out_path = img_path.replace(TEMP_FOLDER, TEMP_OUT)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path