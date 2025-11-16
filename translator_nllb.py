# translator_nllb.py
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import os

# Ajuste o caminho se necessário; você também pode importar de config.py
MODEL_DIR = os.getenv("NLLB_MODEL_DIR", r"C:\Users\Henrique\Downloads\NLLB_200")

class TranslatorNLLB:
    def __init__(self, src_lang, tgt_lang="por_Latn"):
        print("🔄 Carregando modelo NLLB-200 local... (isso pode demorar alguns segundos)")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

        # Carrega tokenizer e modelo local (local_files_only para forçar uso offline)
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, local_files_only=True)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(
            MODEL_DIR,
            local_files_only=True,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None
        )

        if not torch.cuda.is_available():
            self.model.to(self.device)

        self.src_lang = src_lang
        self.tgt_lang = tgt_lang

        # NLLB requer que tokenizer saiba a língua de origem
        try:
            self.tokenizer.src_lang = self.src_lang
        except Exception:
            pass

    def translate(self, text):
        text = text.strip()
        if not text:
            return ""

        # Tokeniza e manda pro device
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, padding=True).to(self.model.device)

        with torch.no_grad():
            output = self.model.generate(
                **inputs,
                forced_bos_token_id=self.tokenizer.convert_tokens_to_ids(self.tgt_lang),
                max_length=256,
                num_beams=4,
                early_stopping=True
            )

        translated = self.tokenizer.batch_decode(output, skip_special_tokens=True)[0]
        return translated


def get_translator(choice):
    choice = str(choice).strip()
    if choice == "1":
        return TranslatorNLLB("jpn_Jpan", "por_Latn"), "jpn"
    elif choice == "2":
        return TranslatorNLLB("kor_Hang", "por_Latn"), "kor"
    elif choice == "3":
        return TranslatorNLLB("eng_Latn", "por_Latn"), "eng"
    else:
        print("Idioma inválido — usando inglês por padrão.")
        return TranslatorNLLB("eng_Latn", "por_Latn"), "eng"
