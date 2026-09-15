# Ad provider research — Windows desktop runtime

> **Revisado em:** 2026-09-15
> **Contexto:** Fase B da missão de YK + anúncios, sobre a branch `feat/ads-yk-rewards`.
> **Veredito:** `AD_PROVIDER_BLOCKER = YES` para anúncios passivos.
> Nenhum provider foi integrado. Nenhuma credencial foi criada.

## O runtime que precisa ser servido

O Yomu Sekai não é um app mobile nem um site. É:

- executável **Win32** empacotado com PyInstaller (Python 3.11), **não UWP**;
- UI em **WebView local** servida por FastAPI/NiceGUI em `127.0.0.1:8080`;
- sem domínio público, portanto **sem `ads.txt`** possível para o próprio app.

Essas três características são exatamente as que eliminam quase todo o inventário
existente. Um SDK mobile não roda aqui; um SDK .NET não é consumível a partir do
Python; e inventário web exige um site real.

## Candidatos avaliados

| Provider | Win32 | WebView | Passivo | Rewarded | SSV | Política permite app desktop | Veredito |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Google AdMob | NÃO | NÃO | — | sim (mobile) | sim (mobile) | **NÃO** | Rejeitado |
| Google AdSense / Ad Manager | NÃO | parcial | sim | sim (web) | sim | **NÃO — proibido explicitamente** | Rejeitado |
| Unity Ads / LevelPlay | NÃO | NÃO | — | sim (mobile) | sim (mobile) | — | Rejeitado |
| AppLovin MAX, ironSource, Pangle, Digital Turbine, TradPlus | NÃO | NÃO | — | sim (mobile) | sim (mobile) | — | Rejeitado |
| Microsoft Advertising SDK / pubCenter | UWP | NÃO | sim | NÃO | NÃO | plataforma desligada | **Morto desde 2020-06-01** |
| Pubfinity | UWP | NÃO | sim | NÃO | NÃO | UWP apenas | Rejeitado |
| PubMatic Windows SDK | UWP | NÃO | sim | NÃO | NÃO | legado | Rejeitado — linha atual (OpenWrap SDK) é iOS/Android/Unity |
| AdsJumbo | **SIM** (WinForms/WPF) | NÃO | sim | **NÃO** | **NÃO** | sim | Rejeitado — SDK .NET/NuGet, sem rewarded, sem SSV |
| ayeT-Studios — Rewarded Video HTML5 | — | sim | NÃO | sim | sim (HMAC-SHA1) | exige `ads.txt` no domínio | Rejeitado — o app não tem domínio |
| ayeT-Studios — Web Offerwall | — | n/d | NÃO | **SIM** | **SIM** (HMAC) | **desktop suportado** | **Único viável** — ver abaixo |

### A citação que fecha o caminho mais óbvio

Política de posicionamento do AdSense, verbatim:

> "Publishers are not permitted to distribute Google ads or AdSense for search
> boxes through software applications including, but not limited to toolbars,
> browser extensions, and desktop applications."

Isso vale para a família inteira (AdSense, Ad Manager, AdMob). A única exceção
documentada para Windows é app **Android** rodando via Google Play Games on PC —
que não é o nosso caso.

### AdsJumbo, o quase

É o único que declara inventário de display para desktop Win32 de verdade
(WinForms/WPF via NuGet). Falha em três pontos independentes:

1. SDK **.NET**, não consumível de um processo Python com UI em WebView;
2. **sem rewarded video**;
3. **sem verificação server-side** — e sem SSV não existe evidência de impressão
   que o backend possa confiar para liberar o YK diário.

### ayeT-Studios Web Offerwall, o viável

Documentação oficial confirma:

- **tráfego desktop explicitamente suportado**;
- entregue por **URL hospedada** (`offerwall.ayet.io/offers?adSlot=…&externalIdentifier=…`),
  que pode ser aberta **numa aba do navegador do usuário** — isso evita o problema
  de política de WebView, porque o usuário está num site real, num browser real;
- **postback server-to-server com HMAC**;
- `externalIdentifier` mapeia o usuário; o callback volta com o identificador.

Encaixa no boundary que já existe: o callback assinado entra pelo backend, é
verificado, e chama `credit_rewarded_ad(...)` com um `provider_event_id` único.
Nenhuma linha da fundação de YK precisa mudar para acomodá-lo.

**Mas não é "assistir a um anúncio".** É completar uma oferta. Isso muda a
proposta ao usuário e é decisão comercial do owner, não técnica.

## Consequência para a regra de produto

A missão pedia (itens 18 e 29):

```text
DAILY_ELIGIBLE =
  plan.daily_yk_target > 0
  AND passive_ads_enabled
  AND valid_passive_ad_activity
```

`valid_passive_ad_activity` **não é implementável hoje**. Não existe provider
que entregue anúncio passivo neste runtime *e* devolva prova verificável
server-side da impressão. Implementar essa condição sem provider exigiria
fabricar prova de anúncio — proibido pela própria missão.

O que a fundação já garante e continua valendo:

- o YK diário só é **utilizável** com `passive_ads_enabled = true`;
- a preferência é server-authoritative e `false` por padrão.

O que fica pendente de decisão do owner:

- se o rewarded da closed beta será **offerwall** (ayeT-Studios) em vez de vídeo;
- se a closed beta abre mão de anúncios passivos e concede o YK diário apenas
  com base na preferência, sem exigir impressão;
- ou se a monetização por anúncios sai do escopo da beta.

## O que NÃO foi feito, de propósito

- nenhum provider integrado;
- nenhuma conta criada, nenhuma credencial gerada;
- os stubs `rewarded-ad-session` e `rewarded-ad-callback` continuam 503;
- `beta_feature_flags.rewarded_ads_enabled` continua `false`;
- `#passiveAdsSettings` continua o placeholder "Em breve";
- nenhuma impressão real de anúncio, em sandbox ou produção.

## Fontes

- [AdSense — políticas de posicionamento](https://support.google.com/adsense/answer/1346295)
- [AdMob — comunidade oficial sobre apps Windows](https://support.google.com/admob/thread/244252861/admob-for-windows-application)
- [Microsoft Learn — Advertising SDK para UWP](https://learn.microsoft.com/en-us/windows/uwp/monetize/display-ads-in-your-app)
- [Windows Central — fim da plataforma de monetização UWP](https://www.windowscentral.com/uwp-apps-lose-microsoft-ad-monetization-platform-june)
- [Pubfinity](https://pubfinity.com/)
- [AdsJumbo — desktop apps](https://adsjumbo.com/monetize-desktop-apps)
- [ayeT-Studios — Rewarded Video SDK for HTML5](https://docs.ayetstudios.com/v/product-docs/rewarded-video/web-integrations/rewarded-video-sdk-for-html5)
- [ayeT-Studios — Web Offerwall](https://docs.ayetstudios.com/v/product-docs/offerwall/web-integrations/web-offerwall)
- [PubMatic — OpenWrap SDK (linha atual)](https://pubmatic.com/products/openwrap-sdk/)
