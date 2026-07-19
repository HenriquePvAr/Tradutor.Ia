# Adapters de fonte de capítulo

## Escopo

Esta camada decide como analisar uma URL pública de capítulo. Ela não concede direito de
acesso, não baixa imagens durante a análise e não contorna login, CAPTCHA, Turnstile, DRM ou
paywall. O resultado é uma decisão codificada e metadados sanitizados para a UI e para o job.
URLs, cookies, query strings, headers privados e corpos de resposta não entram no manifesto
público.

Entrada por pasta local é uma fonte distinta e deve ser resolvida antes de qualquer URL quando
esse adapter estiver habilitado. Este documento descreve o registro HTTP(S) atual; ele não
declara que uma pasta local já seja aceita por esse registro.

## Contrato v1

`ChapterSourceAdapter` v1 mantém o núcleo de URL e adiciona hooks de análise. `BaseAdapter`
fornece defaults conservadores para que um adapter específico sobrescreva somente a parte que
conhece, sem colocar regras de site no downloader.

| Grupo | Métodos/campos | Regra |
| --- | --- | --- |
| Identidade | `name`, `adapter_version`, `allowed_hosts` | Versão é metadado de compatibilidade; não é URL, cookie nem segredo. |
| URL | `supports`, `normalize_url`, `validate_url`, `validate_navigation_url`, `validate_observed_url`, `validate_path` | Valida HTTP(S), host, DNS e path antes de uma etapa irreversível. |
| Evidência | `wait_until_ready`, `collect_dom_candidates`, `collect_network_candidates`, `collect_json_candidates` | Coleta limitada; não faz request adicional fora do navegador já aberto. |
| Decisão | `analyze`, `cluster_candidates`, `score_cluster`, `build_page_manifest` | Score explicável; manifesto público contém IDs opacos, não URLs remotas. |
| Execução | `build_command`, `reader_selectors`, `classify_candidate`, `exclude_candidate`, `authorize_related_url` | Comando é descrição estruturada, não shell; o adapter continua dono de seletores e hosts. |
| Erro | `sanitize_error` | Expõe somente código conhecido ou classe, jamais traceback, URL, header ou cookie. |

`analyze()` não baixa páginas selecionadas. Um adapter pode devolver evidência para revisão
manual, mas somente um resultado completo e autorizado permite construir o conjunto de
download. O transporte revalida cada URL efetivamente buscada.

## Resolução

Para URLs HTTP(S), a resolução é determinística:

1. adapter específico registrado que declara `supports(url)`;
2. `UniversalChapterAdapter` para URL pública não reclamada;
3. estado codificado de bloqueio ou revisão quando URL, cobertura ou confiança não forem
   aceitáveis.

Um adapter específico sempre vence o fallback universal. A presença de um host no fallback não
o transforma em fonte homologada nem cria allowlist global.

## Adapters específicos

### Webtoons

`WEBTOONS` preserva seus seletores e hosts de recurso explicitamente autorizados. O downloader
não contém seletores do Webtoons; mudanças deste reader pertencem ao adapter e a fixtures
herméticas correspondentes.

### VortexScans

`VortexScansAdapter` é deliberadamente restrito:

- aceita somente o host literal `vortexscans.org`; `www`, subdomínios, lookalikes e CDNs não
  são tratados como Vortex;
- aceita somente path genérico de capítulo no formato `/series/<slug>/chapter-<slug>`;
- possui seletores próprios, sem reutilizar seletores do Webtoons;
- não autoriza CDN externo apenas porque ele foi visto na página;
- aplica as mesmas validações de URL, redirect, MIME, bytes e limites dos transportes.

Essa política falha fechada. Um reader que dependa de host de imagem não verificado, paginação
interativa, challenge, autenticação ou estrutura não observável pode parar com estado controlado
em vez de produzir capítulo parcial. Compatibilidade real requer smoke autorizado separado; os
testes padrão usam somente fixtures e DNS falso.

## UniversalChapterAdapter

O fallback universal aceita somente URL HTTP(S) pública validada. Ele observa DOM, dados JSON
reconhecidos e eventos de rede já gerados pelo navegador, forma clusters e calcula confiança
explicável.

| Resultado | Significado |
| --- | --- |
| `supported_specific_adapter` | Adapter específico encontrou evidência utilizável. |
| `supported_generic_high_confidence` | Cluster genérico completo atingiu o limiar automático. |
| `review_required_medium_confidence` | Há evidência, mas seleção humana é necessária antes do download. |
| `unsupported_low_confidence` | Nenhum cluster atingiu confiança suficiente. |
| `incomplete_download` | Limite, scroll, rede ou cobertura impede afirmar capítulo completo. |
| `challenge_required`, `authentication_required`, `source_access_denied`, `source_rate_limited` | Barreira ou resposta de fonte; não há evasão. |
| `unsupported_canvas_reader` | Canvas do reader não pôde ser capturado de forma íntegra. |
| `unsupported_cross_origin_reader` | Iframe externo com evidência de reader não pode ser inspecionado sem contorno. |

Score alto é evidência de agrupamento, não prova de direito de acesso, estabilidade, OCR ou
qualidade de tradução. O fallback não clica genericamente em next, load more, anúncios,
carrosséis ou sliders. Esses padrões precisam de adapter específico com controles de identidade
de capítulo, ciclo, quantidade e ordem.

## Evidência e privacidade

O coletor DOM considera `img`, `currentSrc`, `srcset`, `picture/source`, lazy attributes,
`data-page`, links diretos de imagem, `background-image`, Shadow DOM aberto e iframe same-origin.
Ele guarda somente a evidência necessária para agrupar: posição, ordem, dimensões, visibilidade,
container, atributos presentes e origem. Diagnósticos públicos reduzem container e caminho a
identificadores ou fingerprints quando necessário.

Eventos de performance/CDP podem complementar o DOM com respostas que o navegador já recebeu.
Persistem apenas host, fingerprint do path, `Content-Type`, `Content-Length` limitado, status,
ordem, initiator e tempo relativo. Query strings, cookies, `Authorization`, JWTs, request IDs,
headers privados e corpo não são persistidos. JSON pequeno e reconhecido pode ser analisado em
memória para extrair páginas; ele é descartado depois e nunca é executado como JavaScript.

Iframe cross-origin decorativo gera diagnóstico sem ser atravessado. Quando a moldura tem
evidência de reader, o resultado é `unsupported_cross_origin_reader`; o sistema não usa uma
parte irmã do DOM para declarar capítulo completo. Shadow DOM fechado e canvas tainted também
não são contornados.

## Perfis por domínio

Perfis reutilizáveis são dicas locais sanitizadas por host exato. Não concedem acesso, não
promovem score e não substituem observação, validação DNS, clusterização ou revisão fresca.
Perfil incompatível, vencido ou associado a análise incompleta é ignorado. Nenhum perfil guarda
URL completa, query, cookie, token, imagem ou conteúdo do capítulo.

## Limitações de segurança

Validação de URL e redirect recusa esquemas não HTTP(S), credenciais, hosts locais e endereços
não globais, e revalida DNS. Isso protege a navegação pré-validada e downloads selecionados, mas
**não é garantia completa de SSRF para URLs arbitrárias não confiáveis**: um navegador pode pedir
subrecursos antes da análise e uma conexão HTTP comum ainda possui janela DNS TOCTOU. Produção que
aceite URLs arbitrárias precisa de proxy/firewall de egress ou política de rede equivalente; sem
isso, use somente fontes confiáveis e autorizadas.

Consulte também [Adaptador universal](UNIVERSAL_CHAPTER_ADAPTER.md) e
[Transportes de download](DOWNLOAD_TRANSPORTS.md).
