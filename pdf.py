from PIL import Image

def generate_pdf(image_paths, pdf_path):
    pil_imgs = [Image.open(p).convert("RGB") for p in image_paths]
    if pil_imgs:
        first, rest = pil_imgs[0], pil_imgs[1:]
        first.save(pdf_path, save_all=True, append_images=rest)
        print(f"✅ PDF gerado: {pdf_path}")
