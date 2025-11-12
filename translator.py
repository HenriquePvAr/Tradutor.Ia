from deep_translator import GoogleTranslator

def get_translator(choice):
    if choice == "1":
        return GoogleTranslator(source="ja", target="pt"), "jpn"
    elif choice == "2":
        return GoogleTranslator(source="ko", target="pt"), "kor"
    else:
        return GoogleTranslator(source="en", target="pt"), "eng"
