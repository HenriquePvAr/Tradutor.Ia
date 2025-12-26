import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytesseract
import os

# Tenta importar configurações
try:
    from config import FONT_PATH, TEMP_FOLDER, TEMP_OUT
except Exception:
    FONT_PATH = None
    TEMP_FOLDER = "capitulo_temp"
    TEMP_OUT = TEMP_FOLDER + "_out"

def get_font(font_path, size):
    try:
        return ImageFont.truetype(font_path, size)
    except:
        return ImageFont.load_default()

def detect_balloons_contours(img_bgr, min_area=3000):
    """
    Detecta APENAS balões claros e definidos.
    Usa 'solidez' para diferenciar um balão redondo de um texto solto espalhado.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (7, 7), 0)
    # Threshold alto para pegar apenas o branco do papel do balão
    _, th = cv2.threshold(blur, 210, 255, cv2.THRESH_BINARY)
    
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    dil = cv2.dilate(th, kernel, iterations=2)
    
    contours, _ = cv2.findContours(dil, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        
        # Filtros mais rígidos para considerar como balão
        if area >= min_area and w > 50 and h > 25:
            # Solidez = Area do contorno / Area do Bounding Box
            solidity = float(area) / (w * h)
            # Balões reais costumam ser sólidos (>0.6). Texto solto é espalhado (<0.5).
            if solidity > 0.45: 
                boxes.append((x, y, w, h))
                
    return sorted(boxes, key=lambda b: b[1])

def extract_and_translate(img_crop, ocr_lang, translator_func):
    """Função auxiliar para OCR e tradução de um recorte"""
    gray = cv2.cvtColor(img_crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 11, 2)
    
    # --psm 6 assume um bloco de texto uniforme
    text = pytesseract.image_to_string(th, lang=ocr_lang, config="--psm 6")
    if not text.strip():
        return None
        
    try:
        translated = translator_func(text)
    except:
        translated = text
    return translated

def draw_text_in_box(pil_img, box, text, font_path, color=(0,0,0), outline=False):
    """
    Desenha texto ajustado ao box. 
    Se outline=True, faz borda branca (para texto solto sobre arte).
    """
    x, y, w, h = box
    draw = ImageDraw.Draw(pil_img)
    
    # Estimativa inicial de fonte
    font_size = int(h * 0.4) 
    font = get_font(font_path, font_size)
    
    # Loop para ajustar tamanho da fonte até caber
    lines = []
    while font_size > 8:
        font = get_font(font_path, font_size)
        words = text.split()
        lines = []
        current_line = ""
        
        # Algoritmo simples de quebra de linha
        for word in words:
            test_line = f"{current_line} {word}".strip()
            bbox = draw.textbbox((0,0), test_line, font=font)
            line_w = bbox[2] - bbox[0]
            if line_w < (w - 4):
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        if current_line: lines.append(current_line)
        
        # Verifica altura total
        total_h = sum([draw.textbbox((0,0), l, font=font)[3] - draw.textbbox((0,0), l, font=font)[1] + 4 for l in lines])
        
        if total_h <= (h - 4):
            break
        font_size -= 2
        
    # Desenhar centralizado
    total_h = sum([draw.textbbox((0,0), l, font=font)[3] - draw.textbbox((0,0), l, font=font)[1] + 4 for l in lines])
    curr_y = y + (h - total_h) // 2
    
    for line in lines:
        bbox = draw.textbbox((0,0), line, font=font)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        curr_x = x + (w - line_w) // 2
        
        if outline:
            # Desenha borda grossa branca para texto solto (simula Stroke)
            offsets = [(-2, -2), (-2, 2), (2, -2), (2, 2), (0, 2), (0, -2), (2, 0), (-2, 0)]
            for ox, oy in offsets:
                draw.text((curr_x + ox, curr_y + oy), line, font=font, fill=(255,255,255))
        
        draw.text((curr_x, curr_y), line, font=font, fill=color)
        curr_y += line_h + 4

    return pil_img

def remove_text_content(img_bgr, x, y, w, h):
    """Inpainting simples na região para apagar o texto original"""
    roi = img_bgr[y:y+h, x:x+w]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # Máscara: tudo que não é fundo claro/papel vira máscara
    _, mask = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    
    # Dilata para cobrir bem as letras
    kernel = np.ones((3,3), np.uint8)
    mask = cv2.dilate(mask, kernel, iterations=1)
    
    # Inpaint (reconstroi o fundo)
    res = cv2.inpaint(roi, mask, 3, cv2.INPAINT_TELEA)
    img_bgr[y:y+h, x:x+w] = res
    return img_bgr

def process_image_file(image_path, ocr_lang, translator_func, font_path=None, save_out=True):
    img = cv2.imread(image_path)
    if img is None: return None
    
    # 1. Detectar Balões (Regiões brancas grandes)
    balloons = detect_balloons_contours(img)
    
    # Máscara para marcar onde já mexemos (para não processar duas vezes)
    processed_mask = np.zeros(img.shape[:2], dtype=np.uint8)
    
    # --- FASE 1: Balões (Fundo Branco) ---
    for (x, y, w, h) in balloons:
        crop = img[y:y+h, x:x+w]
        translated = extract_and_translate(crop, ocr_lang, translator_func)
        if translated:
            img = remove_text_content(img, x, y, w, h)
            
            # Converte para PIL para escrever
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            # Outline False = Texto normal em balão branco
            pil_img = draw_text_in_box(pil_img, (x, y, w, h), translated, font_path, color=(0,0,0), outline=False)
            img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            
            # Marca área como processada
            cv2.rectangle(processed_mask, (x, y), (x+w, y+h), 255, -1)

    # --- FASE 2: Texto Solto (Sobre a Arte) ---
    try:
        # Pega todos os blocos de texto da imagem inteira
        data = pytesseract.image_to_data(img, lang=ocr_lang, output_type=pytesseract.Output.DICT)
        n_boxes = len(data['text'])
        
        # Agrupa blocos de texto (Parágrafos/Blocos)
        blocks = {}
        for i in range(n_boxes):
            # Confiança > 30 e texto não vazio
            if int(data['conf'][i]) > 30 and data['text'][i].strip():
                x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
                
                # Se o centro desse texto cai num balão que JÁ processamos, ignora
                cx, cy = x + w//2, y + h//2
                if processed_mask[cy, cx] != 0:
                    continue
                
                # Chave única para o bloco (BlockNum_ParNum)
                blk_id = f"{data['block_num'][i]}_{data['par_num'][i]}"
                if blk_id not in blocks: 
                    blocks[blk_id] = {'text': [], 'box': [x, y, x+w, y+h]}
                
                blocks[blk_id]['text'].append(data['text'][i])
                
                # Expande o retângulo do bloco para caber todas as palavras
                bx = blocks[blk_id]['box']
                bx[0] = min(bx[0], x)
                bx[1] = min(bx[1], y)
                bx[2] = max(bx[2], x+w)
                bx[3] = max(bx[3], y+h)

        # Processa cada bloco solto encontrado
        for b in blocks.values():
            original_text = " ".join(b['text'])
            x1, y1, x2, y2 = b['box']
            w, h = x2-x1, y2-y1
            
            if w < 20 or h < 10: continue 

            # Traduz
            try:
                translated = translator_func(original_text)
            except:
                translated = original_text
            
            # Apaga o texto original (tentando manter o fundo da arte)
            img = remove_text_content(img, x1, y1, w, h)
            
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            
            # Aumenta um pouco a área de desenho (padding)
            draw_box = (max(0, x1-10), max(0, y1-5), w+20, h+10)
            
            # Outline True = Texto com borda branca (estilo legenda/pensamento)
            pil_img = draw_text_in_box(pil_img, draw_box, translated, font_path, color=(0,0,0), outline=True)
            img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    except Exception as e:
        print(f"Erro no processamento de texto solto: {e}")

    # Salvar resultado
    out_path = image_path.replace(TEMP_FOLDER, TEMP_OUT)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, img)
    return out_path