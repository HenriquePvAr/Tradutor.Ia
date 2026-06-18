import os
import tkinter as tk
from tkinter import messagebox, simpledialog

import config
from config import FONT_PATH
from manga_translation_pipeline import download_chapter_images, export_pdf, translate_chapter_images
from progress import ProgressWindow


def main():
    root = tk.Tk()
    root.withdraw()

    url = simpledialog.askstring("Capitulo", "Cole a URL do capitulo:")
    if not url or not url.strip():
        messagebox.showerror("Erro", "Nenhuma URL fornecida!")
        return

    chapter_name = simpledialog.askstring("Saida", "Nome da pasta/PDF de saida:")
    if not chapter_name:
        chapter_name = "capitulo_traduzido"

    output_folder = os.path.join(os.getcwd(), chapter_name)
    os.makedirs(output_folder, exist_ok=True)

    lang_choice = simpledialog.askstring(
        "Idioma",
        "Escolha a lingua original:\n1 = Japones\n2 = Coreano\n3 = Ingles",
    )

    if lang_choice not in ("1", "2", "3"):
        messagebox.showerror("Erro", "Idioma invalido!")
        return

    mode_choice = simpledialog.askstring(
        "Modelo de Traducao",
        "Qual motor usar?\n\n"
        "1 = NVIDIA API (lote / Nemotron)\n"
        "2 = Google (rapido / online)\n"
        "3 = IA local (melhor qualidade / offline / lento)\n\n"
        f"Enter/cancelar = manter .env/config ({config.TRANSLATION_MODE})",
    )

    if mode_choice == "1":
        config.TRANSLATION_MODE = "nvidia"
    elif mode_choice == "2":
        config.TRANSLATION_MODE = "google"
    elif mode_choice == "3":
        config.TRANSLATION_MODE = "huggingface"

    prog1 = ProgressWindow("Baixando paginas")

    saved_images = download_chapter_images(
        url,
        callback=lambda cur, tot: prog1.update(cur, tot, "Baixando paginas"),
    )

    prog1.close()

    if not saved_images:
        messagebox.showerror("Erro", "Nenhuma imagem encontrada!")
        return

    prog2 = ProgressWindow("Traduzindo paginas")

    translated_images = translate_chapter_images(
        saved_images,
        lang_choice,
        FONT_PATH,
        callback=lambda cur, tot: prog2.update(cur, tot, "Traduzindo..."),
    )

    prog2.close()

    if not translated_images:
        messagebox.showerror("Erro", "Falha ao traduzir imagens!")
        return

    prog3 = ProgressWindow("Gerando PDF")

    pdf_path = export_pdf(translated_images, output_folder, chapter_name)

    prog3.update(1, 1, "Concluindo...")
    prog3.close()

    messagebox.showinfo("Concluido", f"PDF gerado com sucesso:\n{pdf_path}")


if __name__ == "__main__":
    main()
