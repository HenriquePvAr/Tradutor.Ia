import pytest
from translation_proxy import MockTranslationProvider, TranslationProxy, DeepLProductionProvider, ProviderNotConfigured

def test_proxy_is_idempotent_and_fail_closed():
    p=MockTranslationProvider(); proxy=TranslationProxy(p); assert proxy.execute(request_id="r",text="x",source="ja",target="pt") == "[pt] x"; assert proxy.execute(request_id="r",text="x",source="ja",target="pt") == "[pt] x"; assert p.calls == 1
    with pytest.raises(ProviderNotConfigured): DeepLProductionProvider(None).translate("x",source="ja",target="pt")
