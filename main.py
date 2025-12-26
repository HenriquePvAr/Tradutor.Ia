import os
import tkinter as tk
from tkinter import simpledialog, messagebox
import config  # <--- Importante: Importamos o módulo config para alterar a variável global

from config import FONT_PATH
from manga_translation_pipeline import (
    download_chapter_images,
    translate_chapter_images,
    export_pdf
)
from progress import ProgressWindow


def main():
    root = tk.Tk()
    root.withdraw()

    # === Entrada da URL ===
    url = simpledialog.askstring("Capítulo", "Cole a URL do capítulo:")
    if not url or not url.strip():
        messagebox.showerror("Erro", "Nenhuma URL fornecida!")
        return

    # === Nome do capítulo/PDF ===
    chapter_name = simpledialog.askstring("Saída", "Nome da pasta/PDF de saída:")
    if not chapter_name:
        chapter_name = "capitulo_traduzido"

    output_folder = os.path.join(os.getcwd(), chapter_name)
    os.makedirs(output_folder, exist_ok=True)

    # === Idioma original ===
    lang_choice = simpledialog.askstring(
        "Idioma",
        "Escolha a língua original:\n1 = Japonês\n2 = Coreano\n3 = Inglês"
    )

    if lang_choice not in ("1", "2", "3"):
        messagebox.showerror("Erro", "Idioma inválido!")
        return

    # === NOVO: Escolha do Modelo ===
    mode_choice = simpledialog.askstring(
        "Modelo de Tradução",
        "Qual motor usar?\n\n"
        "1 = Google (Rápido / Online)\n"
        "2 = IA Local (Melhor Qualidade / Offline / Lento)"
    )

    # Configura dinamicamente o modo de tradução
    if mode_choice == "2":
        config.TRANSLATION_MODE = "huggingface"
    else:
        config.TRANSLATION_MODE = "google"  # Padrão se cancelar ou escolher 1

    # ==============================
    #       PROGRESSO 1
    #     BAIXANDO IMAGENS
    # ==============================
    prog1 = ProgressWindow("Baixando páginas")

    saved_images = download_chapter_images(
        url,
        callback=lambda cur, tot: prog1.update(cur, tot, "Baixando páginas")
    )

    prog1.close()

    if not saved_images:
        messagebox.showerror("Erro", "Nenhuma imagem encontrada!")
        return

    # ==============================
    #       PROGRESSO 2
    #     TRADUZINDO PÁGINAS
    # ==============================
    prog2 = ProgressWindow("Traduzindo páginas")

    # Passamos o font_path e o callback de progresso
    translated_images = translate_chapter_images(
        saved_images,
        lang_choice,
        FONT_PATH,
        callback=lambda cur, tot: prog2.update(cur, tot, "Traduzindo...")
    )

    prog2.close()

    if not translated_images:
        messagebox.showerror("Erro", "Falha ao traduzir imagens!")
        return

    # ==============================
    #       PROGRESSO 3
    #        GERANDO PDF
    # ==============================
    prog3 = ProgressWindow("Gerando PDF")

    pdf_path = export_pdf(translated_images, output_folder, chapter_name)

    prog3.update(1, 1, "Concluindo...")
    prog3.close()

    messagebox.showinfo("Concluído", f"PDF gerado com sucesso:\n{pdf_path}")


if __name__ == "__main__":
    main()