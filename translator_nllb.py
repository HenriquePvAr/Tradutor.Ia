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
        # Legacy compatibility object. The shipped product path is DeepL; the
        # unmaintained deep-translator package is intentionally not installed.
        self.translator = None

    def translate(self, text):
        text = str(text or "").strip()
        if not text:
            return ""

        print("Google Translator legado indisponível; mantendo original.")
        return text


def ocr_code_for_choice(choice):
    """Return the OCR language code without constructing a translator."""
    return {"1": "jpn", "2": "kor"}.get(str(choice).strip(), "eng")


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

    # The desktop backend provider is injected by the job runner with its
    # job-scoped auth/runtime context.  Never construct a local provider here.
    if translation_provider is not None and hasattr(translation_provider, "translate_batch"):
        return translation_provider, ocr_code

    from ui_helpers import DEFAULT_TRANSLATION_PROVIDER, normalize_translation_provider

    provider = normalize_translation_provider(translation_provider)
    # A job that named no provider gets the canonical beta default.  Scoped to the
    # NVIDIA family because TRANSLATION_MODE is the older, orthogonal axis: an
    # install that deliberately runs google/huggingface must not be seized by a
    # provider default it never opted into.
    if not provider and mode == "nvidia":
        provider = DEFAULT_TRANSLATION_PROVIDER

    if provider in {"yomu_backend", "backend", "yomu"}:
        raise ValueError("yomu_backend_provider_requires_injected_instance")

    # An explicitly requested provider identifies its own backend, so DeepL is
    # resolved before TRANSLATION_MODE (which only ever selected between the
    # local/Google/NVIDIA families).  Nothing here may fall back to another
    # provider: a DeepL job that cannot reach DeepL fails, it does not become
    # a Riva job.
    if provider == "deepl":
        from translator_deepl import DeepLTranslator

        print(f"Usando DeepL ({config.DEEPL_MODEL_TYPE}) - Origem: {source_language}")
        # No naturalizer is attached: DeepL's benchmark output needed none, and
        # bolting an LLM pass onto a ~2s provider path would silently turn it
        # into a slow multi-provider pipeline.
        return DeepLTranslator(source_language=source_language), ocr_code

    if mode == "nvidia":
        from translator_nvidia import TranslatorNvidiaBatch

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
