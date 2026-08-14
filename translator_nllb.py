from deep_translator import GoogleTranslator

import config


class TranslatorNLLB:
    def __init__(self, src_lang, tgt_lang="por_Latn"):
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        print(f"Carregando modelo NLLB-200 local ({src_lang})...")
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained(config.NLLB_MODEL_DIR, local_files_only=True)
        self.model = AutoModelForSeq2SeqLM.from_pretrained(
            config.NLLB_MODEL_DIR,
            local_files_only=True,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None,
        )

        if not torch.cuda.is_available():
            self.model.to(self.device)

        self.src_lang = src_lang
        self.tgt_lang = tgt_lang

        try:
            self.tokenizer.src_lang = self.src_lang
        except Exception:
            pass

    def translate(self, text):
        text = str(text or "").strip()
        if not text:
            return ""

        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, padding=True).to(
            self.model.device
        )
        with self.torch.no_grad():
            output = self.model.generate(
                **inputs,
                forced_bos_token_id=self.tokenizer.convert_tokens_to_ids(self.tgt_lang),
                max_length=256,
                num_beams=4,
                early_stopping=True,
            )
        return self.tokenizer.batch_decode(output, skip_special_tokens=True)[0]


class TranslatorGoogle:
    def __init__(self, src_lang_code):
        self.source = src_lang_code
        self.translator = GoogleTranslator(source=self.source, target="pt")

    def translate(self, text):
        text = str(text or "").strip()
        if not text:
            return ""

        try:
            return self.translator.translate(text)
        except Exception as exc:
            print(f"Erro no Google Translate. Mantendo original: {exc}")
            return text


def get_translator(choice, *, translation_provider=None):
    choice = str(choice).strip()

    if choice == "1":
        nllb_lang = "jpn_Jpan"
        google_lang = "ja"
        ocr_code = "jpn"
        source_language = "japones"
    elif choice == "2":
        nllb_lang = "kor_Hang"
        google_lang = "ko"
        ocr_code = "kor"
        source_language = "coreano"
    else:
        nllb_lang = "eng_Latn"
        google_lang = "en"
        ocr_code = "eng"
        source_language = "ingles"

    mode = (config.TRANSLATION_MODE or "google").lower()

    if mode == "nvidia":
        from translator_nvidia import TranslatorNvidiaBatch

        provider = str(translation_provider or "").strip().lower()
        if provider and provider not in {"nemotron", "riva"}:
            raise ValueError("nvidia_translation_provider_invalid")
        model = (
            config.NVIDIA_RIVA_TRANSLATION_MODEL
            if provider == "riva"
            else config.NVIDIA_TRANSLATION_MODEL
        )
        print(f"Usando NVIDIA API ({model}) - Origem: {source_language}")
        translator = TranslatorNvidiaBatch(
            source_language=source_language,
            translation_provider=provider or None,
        )
        naturalization_mode = str(
            getattr(config, "PTBR_NATURALIZATION_MODE", "selective") or "selective"
        ).lower()
        if naturalization_mode not in {"off", "selective"}:
            raise ValueError("ptbr_naturalization_mode_invalid")
        if naturalization_mode == "selective":
            import natural_ptbr_refinement

            translator.ptbr_naturalizer = natural_ptbr_refinement.RuntimePtBrNaturalizer(
                natural_ptbr_refinement.RefinementService(
                    natural_ptbr_refinement.NvidiaRefinementProvider(translator)
                ),
                model=translator.model,
            )
            translator.stats["naturalization_enabled"] = True
            translator.stats["naturalization_mode"] = "selective"
        else:
            translator.ptbr_naturalizer = None
            translator.stats["naturalization_enabled"] = False
            translator.stats["naturalization_mode"] = "off"
        return translator, ocr_code

    if mode == "google":
        print(f"Usando Google Translator (online) - Origem: {google_lang}")
        return TranslatorGoogle(google_lang), ocr_code

    if mode in ("huggingface", "local", "nllb"):
        print(f"Usando IA local (NLLB) - Origem: {nllb_lang}")
        return TranslatorNLLB(nllb_lang, "por_Latn"), ocr_code

    print(f"TRANSLATION_MODE desconhecido ({mode}). Usando Google Translator.")
    return TranslatorGoogle(google_lang), ocr_code
