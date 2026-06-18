from translator_nvidia import TranslatorNvidiaBatch


def main():
    texts = [
        "Watch out!",
        "I can't believe you came back.",
        "This power... it is different.",
        "Run before the gate closes!",
        "No way. He survived?",
        "I promised I would protect them.",
        "The night is still young.",
        "Don't make me repeat myself.",
    ]

    translator = TranslatorNvidiaBatch(source_language="ingles")

    if not translator.is_configured:
        print("NVIDIA_API_KEY nao configurada.")
        print("Crie um arquivo .env a partir de .env.example e preencha NVIDIA_API_KEY.")
        print("Frases de teste que serao traduzidas quando a chave estiver configurada:")
        for idx, text in enumerate(texts, start=1):
            print(f"{idx}. {text}")
        return

    translations = translator.translate_many(texts)
    for original, translated in zip(texts, translations):
        print(f"{original} -> {translated}")


if __name__ == "__main__":
    main()
