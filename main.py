import os, tkinter as tk
from tkinter import simpledialog, messagebox
from config import TEMP_FOLDER, TEMP_OUT, FONT_PATH
from down import download_images
from ocr import ocr_translate_balloon
from translator import get_translator
from pdf import generate_pdf
from progress import ProgressWindow

def main():
    root = tk.Tk()
    root.withdraw()

    url = simpledialog.askstring("Capítulo", "Cole a URL do capítulo:")
    if not url: return
    folder_name = simpledialog.askstring("Saída", "Nome da pasta/PDF de saída:") or "capitulo_traduzido"
    output_folder = os.path.join(os.getcwd(), folder_name)
    os.makedirs(output_folder, exist_ok=True)

    lang_choice = simpledialog.askstring("Idioma", "1:Japonês 2:Coreano 3:Inglês")
    translator, ocr_lang = get_translator(lang_choice)
    prog_win = ProgressWindow("Baixando e traduzindo capítulo")

    saved_files = download_images(url, progress_callback=lambda v,m,t: prog_win.update(v,m,t))
    processed_files = []
    for idx, img in enumerate(saved_files, start=1):
        out = ocr_translate_balloon(img, ocr_lang, translator, FONT_PATH)
        if out: processed_files.append(out)
        prog_win.update(idx, len(saved_files), "Traduzindo imagens")

    prog_win.close()
    if not processed_files:
        messagebox.showerror("Erro", "Nenhuma imagem processada!")
        return

    pdf_path = os.path.join(output_folder, folder_name + ".pdf")
    generate_pdf(processed_files, pdf_path)
    messagebox.showinfo("Concluído", f"PDF gerado: {pdf_path}")

if __name__ == "__main__":
    main()
