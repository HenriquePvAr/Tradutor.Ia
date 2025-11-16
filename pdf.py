

from PIL import Image

def generate_pdf(image_paths, pdf_path):
    if not image_paths:
        raise ValueError("Nenhuma imagem fornecida para gerar PDF.")

    pil_imgs = []
    
    for path in image_paths:
        try:
            img = Image.open(path)

            # Converter sempre para RGB (evita problemas com PNG e transparência)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            pil_imgs.append(img)

        except Exception as e:
            print(f"❌ Erro ao abrir a imagem {path}: {e}")

    if not pil_imgs:
        raise ValueError("Falha ao carregar imagens para o PDF.")

    # Garantir que o primeiro exista
    first = pil_imgs[0]
    rest = pil_imgs[1:]

    try:
        first.save(
            pdf_path,
            save_all=True,
            append_images=rest
        )
        print(f"✅ PDF gerado com sucesso: {pdf_path}")
    except Exception as e:
        print(f"❌ Erro ao gerar PDF: {e}")
    finally:
        # Evita arquivo corrompido por imagens abertas
        for img in pil_imgs:
            img.close()