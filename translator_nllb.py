import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import os
from deep_translator import GoogleTranslator
import config  # Importa o módulo de configuração inteiro

# Caminho do modelo local
MODEL_DIR = os.getenv("NLLB_MODEL_DIR", r"C:\Users\Henrique\Downloads\NLLB_200")

# --- Classe para Tradução Local (NLLB) ---
class TranslatorNLLB:
    def __init__(self, src_lang, tgt_lang="por_Latn"):
        print(f"🔄 Carregando modelo NLLB-200 local ({src_lang})...")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        
        try:
            self.tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True)
            self.model = AutoModelForSeq2SeqLM.from_pretrained(
                MODEL_DIR,
                local_files_only=True,
                torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                device_map="auto" if torch.cuda.is_available() else None
            )
        except Exception as e:
            print(f"❌ Erro ao carregar NLLB local: {e}")
            raise e

        if not torch.cuda.is_available():
            self.model.to(self.device)

        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        
        try:
            self.tokenizer.src_lang = self.src_lang
        except:
            pass

    def translate(self, text):
        text = text.strip()
        if not text: return ""
        
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, padding=True).to(self.model.device)
        with torch.no_grad():
            output = self.model.generate(
                **inputs,
                forced_bos_token_id=self.tokenizer.convert_tokens_to_ids(self.tgt_lang),
                max_length=256,
                num_beams=4,
                early_stopping=True
            )
        return self.tokenizer.batch_decode(output, skip_special_tokens=True)[0]

# --- Classe Wrapper para Google Tradutor ---
class TranslatorGoogle:
    def __init__(self, src_lang_code):
        # Mapeia códigos do seu menu para códigos do Google
        self.source = src_lang_code 
        self.translator = GoogleTranslator(source=self.source, target='pt')

    def translate(self, text):
        if not text.strip(): return ""
        try:
            return self.translator.translate(text)
        except Exception as e:
            print(f"⚠️ Erro no Google Translate: {e}")
            return text

# --- Função Principal de Escolha ---
def get_translator(choice):
    choice = str(choice).strip()
    
    # Define códigos de idioma para cada motor
    if choice == "1":   # Japonês
        nllb_lang = "jpn_Jpan"
        google_lang = "ja"
        ocr_code = "jpn"
    elif choice == "2": # Coreano
        nllb_lang = "kor_Hang"
        google_lang = "ko"
        ocr_code = "kor"
    else:               # Inglês/Outros
        nllb_lang = "eng_Latn"
        google_lang = "en"
        ocr_code = "eng"

    # Verifica a configuração (que será alterada dinamicamente pelo main.py)
    if config.TRANSLATION_MODE == "google":
        print(f"🌍 Usando Google Translator (Online) - Origem: {google_lang}")
        return TranslatorGoogle(google_lang), ocr_code
    else:
        print(f"🤖 Usando IA Local (NLLB) - Origem: {nllb_lang}")
        return TranslatorNLLB(nllb_lang, "por_Latn"), ocr_code