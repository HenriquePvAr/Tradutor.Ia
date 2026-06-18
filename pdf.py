from PIL import Image


def to_rgb(img):
    if img.mode == "RGB":
        return img.copy()

    if img.mode in ("RGBA", "LA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg

    if img.mode == "P" and "transparency" in img.info:
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg

    return img.convert("RGB")


def generate_pdf(image_paths, pdf_path):
    if not image_paths:
        raise ValueError("Nenhuma imagem fornecida para gerar PDF.")

    pil_imgs = []

    for path in image_paths:
        try:
            with Image.open(path) as img:
                pil_imgs.append(to_rgb(img))
        except Exception as exc:
            print(f"Erro ao abrir imagem para PDF, pulando {path}: {exc}")

    if not pil_imgs:
        raise ValueError("Nenhuma imagem valida foi carregada para gerar o PDF.")

    first = pil_imgs[0]
    rest = pil_imgs[1:]

    try:
        first.save(pdf_path, "PDF", save_all=True, append_images=rest)
        print(f"PDF gerado com sucesso: {pdf_path}")
    except Exception as exc:
        raise RuntimeError(f"Erro ao gerar PDF: {exc}") from exc
    finally:
        for img in pil_imgs:
            img.close()
