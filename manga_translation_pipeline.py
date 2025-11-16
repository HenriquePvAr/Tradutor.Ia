# manga_translation_pipeline.py

import os
import glob
import time

from down import download_images
from ocr_balloon import process_image_file
from translator_nllb import get_translator
from pdf import generate_pdf

from config import TEMP_FOLDER, TEMP_OUT, FONT_PATH


# ======================================================
# 1) Baixar imagens do capítulo
# ======================================================
def download_chapter_images(url, callback=None):
    """
    Baixa todas as imagens do capítulo usando down.py e as salva em TEMP_FOLDER.
    """
    if os.path.exists(TEMP_FOLDER):
        for f in glob.glob(TEMP_FOLDER + "/*"):
            os.remove(f)

    imgs = download_images(url, progress_callback=lambda v, m, t: callback(v, m) if callback else None)
    return imgs


# ======================================================
# 2) Traduzir todas as imagens do capítulo
# ======================================================
def translate_chapter_images(image_list, lang_choice, font_path, callback=None):
    """
    Processa cada imagem:
        - OCR
        - Tradução com NLLB
        - Redesenha texto no balão
    Salva o resultado em TEMP_OUT
    """
    translator, ocr_lang = get_translator(lang_choice)
    translate_fn = translator.translate

    if not os.path.exists(TEMP_OUT):
        os.makedirs(TEMP_OUT)

    out_files = []
    total = len(image_list)

    for idx, img_path in enumerate(image_list, start=1):
        out = process_image_file(
            img_path,
            ocr_lang,
            translate_fn,
            font_path=font_path,
            save_out=True
        )

        if out:
            out_files.append(out)

        if callback:
            callback(idx, total)

    return out_files


# ======================================================
# 3) Criar PDF final
# ======================================================
def export_pdf(translated_images, output_folder, chapter_name):
    """
    Gera um PDF no diretório de saída com nome chapter_name.pdf
    """
    pdf_path = os.path.join(output_folder, chapter_name + ".pdf")
    generate_pdf(translated_images, pdf_path)
    return pdf_path
