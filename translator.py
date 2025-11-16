import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

MODEL_DIR = r"C:\Users\Henrique\Downloads\NLLB_200"

class TranslatorNLLB:
    def __init__(self, src_lang, tgt_lang="por_Latn"):
        print("🔄 Carregando modelo NLLB-200 local...")

        # Carrega tokenizer local
        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_DIR,
            local_files_only=True
        )

        # Carrega modelo local com PyTorch
        self.model = AutoModelForSeq2SeqLM.from_pretrained(
            MODEL_DIR,
            local_files_only=True,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32
        )

        # Move para GPU se existir
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)

        # Idiomas
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang

        # MUITO IMPORTANTE
        self.tokenizer.src_lang = self.src_lang

    def translate(self, text):
        if not text.strip():
            return ""

        # Tokenização
        inputs = self.tokenizer(
            text,
            return_tensors="pt"
        ).to(self.device)

        # Tradução
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                forced_bos_token_id=self.tokenizer.convert_tokens_to_ids(self.tgt_lang),
                max_length=256
            )

        return self.tokenizer.batch_decode(outputs, skip_special_tokens=True)[0]


def get_translator(choice):
    choice = str(choice).strip()

    if choice == "1":  # Japonês
        return TranslatorNLLB("jpn_Jpan", "por_Latn"), "jpn"

    elif choice == "2":  # Coreano
        return TranslatorNLLB("kor_Hang", "por_Latn"), "kor"

    elif choice == "3":  # Inglês
        return TranslatorNLLB("eng_Latn", "por_Latn"), "eng"

    print("Opção inválida, usando inglês.")
    return TranslatorNLLB("eng_Latn", "por_Latn"), "eng"
