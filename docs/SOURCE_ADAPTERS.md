# Adapters de fonte de capítulo

## Escopo

Esta camada decide como analisar uma URL pública de capítulo. Ela não concede direito de
acesso, não baixa imagens durante a análise e não contorna login, CAPTCHA, Turnstile, DRM ou
paywall. O resultado é uma decisão codificada e metadados sanitizados para a UI e para o job.
URLs, cookies, query strings, headers privados e corpos de resposta não entram no manifesto
público.

Entrada por pasta local é uma fonte distinta da resolução HTTP(S). A UI e a CLI escolhem
exatamente uma origem — URL pública **ou** pasta local — antes de criar o job. A pasta passa por
`LocalFolderChapterAdapter`, política de caminhos e snapshot interno; ela não é convertida em
`file://`, não entra no registro de hosts HTTP e não abre navegador. Veja
[Entrada por pasta local](LOCAL_FOLDER_INPUT.md).

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

Para uma submissão, a resolução é determinística:

1. fonte local declarada: validação/snapshot por `LocalFolderChapterAdapter`;
2. URL: adapter específico registrado que declara `supports(url)`;
3. URL pública não reclamada: `UniversalChapterAdapter`;
4. estado codificado de bloqueio ou revisão quando caminho, URL, cobertura ou confiança não
   forem aceitáveis.

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
- nunca aceita CDN externo como URL de capítulo ou destino de navegação;
- um host de recurso público só pode ser autorizado na instância atual depois de observado e
  validado; essa autorização não vira allowlist global nem vaza para outro job;
- aplica as mesmas validações de URL, redirect, MIME, bytes e limites dos transportes.

Essa política falha fechada. A paginação genérica limitada não é aplicada a adapters específicos:
um reader Vortex que dependa de host de imagem não verificado, paginação própria/interativa,
challenge, autenticação ou estrutura não observável pode parar com estado controlado em vez de
produzir capítulo parcial. Compatibilidade real requer smoke autorizado separado; os
testes padrão usam somente fixtures, drivers/sessões falsos e DNS sintético.

### Pasta local

LocalFolderChapterAdapter é específico, mas não herda o contrato de URL: ele aceita uma pasta
absoluta sob raiz permitida, valida arquivos diretos e gera um snapshot de páginas lógicas.
O job e o output recebem source_type=local_folder, adapter/versão, contagens, fingerprint e
referência de snapshot opaca — não o caminho original. A interface local só aceita esse tipo de
fonte quando o bind e o cliente são loopback.

As páginas já fornecidas como unidades completas marcam logical_pages=true, portanto não passam
por reconstrução Smart Split. A materialização revalida manifesto, hash e bytes antes de copiar
as páginas geradas para output/. A entrada é offline quanto à aquisição; OCR/tradução de uma
execução real continuam sujeitos à configuração normal do pipeline.

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
qualidade de tradução. Para o fallback universal há uma única forma limitada de paginação:
ele pode seguir um `href` explícito, visível e não desabilitado apenas quando origem e path são
idênticos, os demais parâmetros de query são idênticos e uma chave convencional de página numérica
avança exatamente de N para N+1. A navegação usa a URL já validada, nunca `click()` nem handler
da página; é limitada a 24 avanços e 45 segundos, além dos limites normais.

Cada superfície adicional recebe scroll, análise e manifesto próprios antes de os candidatos
aceitos serem agregados. Link ambíguo, query não comprovada, ciclo, redirect, timeout, scroll
incompleto ou análise posterior inutilizável gera `pagination_incomplete`; esse sinal de
cobertura bloqueia a seleção, inclusive pelo caminho de revisão manual, e não vira um download
parcial. O fallback continua sem acionar `load more`,
botões sem `href`, anúncios, carrosséis ou sliders. Esses padrões precisam de adapter específico
com controles de identidade de capítulo, ciclo, quantidade e ordem.

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

Quando a análise precisa de revisão humana, uma imagem DOM visível que o navegador já carregou
pode gerar uma prévia local opcional. A prévia é um `data:image` JPEG/PNG base64 limitado (até 64
itens, 24.000 caracteres por item e 1.000.000 no total), nunca URL de origem; a UI valida essa
forma e a renderiza sem fazer request remoto. Imagem disponível apenas em metadados de rede,
imagem oculta/ausente e superfície CORS/tainted permanecem sem prévia, mas preservam os metadados
seguros do candidato para a decisão humana. A prévia não altera score, seleção ou download.

Enquanto o mesmo job está em `awaiting_source_review`, a UI preserva o DOM da revisão entre
pollings, para não restaurar páginas excluídas nem desfazer a ordem manual na aba atual. A
confirmação continua a enviar somente IDs opacos; recarregar a página ou mudar de job não é um
mecanismo de persistência dessa edição local.

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

Consulte também [Adaptador universal](UNIVERSAL_CHAPTER_ADAPTER.md),
[Transportes de download](DOWNLOAD_TRANSPORTS.md) e [Segurança](SECURITY.md).

## Análise de fonte × cobertura de download

São dois conceitos distintos, e confundi-los produziu um diagnóstico enganoso.

**Análise completa** significa: adapter selecionado, fonte reconhecida, reader/container
identificado, candidatos coletados e aceitos, ordem disponível, sem paginação pendente e sem
falha de segurança. **Não** significa arquivos baixados, bytes validados, hashes ou PDF.

**Download completo** significa: todos os itens esperados foram tentados, os necessários
foram baixados, `Content-Type` válido, magic bytes válidos, imagem decodificável, hashes
produzidos e nenhuma página obrigatória faltando.

| Situação | stage | reason_code |
| --- | --- | --- |
| Driver ausente | `source_analysis` | `source_not_ready` |
| Nenhum candidato | `source_analysis` | `no_chapter_images` |
| Coletor não viu o leitor inteiro | `source_analysis` | `incomplete_source_coverage` |
| Confiança média | `awaiting_source_review` | — (não é `failed`) |
| Download parcial | `downloading_pages` / `validating_pages` | `incomplete_download` |

A combinação `stage=source_analysis` + `reason_code=incomplete_download` é proibida e há
teste que falha se ela reaparecer.

### O defeito corrigido

Um submit real aceitou 167 candidatos e então falhou com `incomplete_download` **dentro de
`source_analysis`** — culpando um download que o runner nem havia iniciado. O sinal do
coletor (`page_limit_exceeded`, `scroll_incomplete`, `pagination_incomplete`) é legítimo,
mas descreve o que o coletor conseguiu enxergar, não bytes que nunca foram buscados. O
rótulo passou a ser `incomplete_source_coverage`; `incomplete_download` continua sendo
produzido apenas pelo downloader (`down.py`).

### Estado verificado do Webtoons

Medido contra o leitor oficial: adapter, normalização (com `title_no`/`episode_no`),
preflight (200), container, seletores (167 imagens), lazy loading, autorização de CDN e o
transport funcionam — comprovado até **duas** imagens baixadas e validadas. O capítulo
inteiro **não** foi processado, e nada aqui afirma suporte completo ao Webtoons.

O driver é opt-in: `TRADUTOR_ALLOW_DRIVER_DOWNLOAD=1` permite ao Selenium Manager oficial
resolver o ChromeDriver. Sem driver e sem a flag, o submit falha com `source_not_ready` —
era essa a causa de ambiente.

### Dívida técnica registrada

`ui_bridge.start()` roda a análise de fonte com Selenium **sincronamente dentro do submit**.
Isso prende a requisição, arrisca timeout e inicia um driver no processo web. Separar isso
(criar o job `queued` e analisar no worker) é uma migração arquitetural com testes próprios,
deliberadamente fora do escopo desta correção.

## Estratégia de cobertura por adapter

O downloader decidia cobertura com uma regra única, cujo próprio docstring dizia valer "for
every adapter type": só considerava completo quando `reached_document_end` **e**
`stabilized`. Isso é correto para uma página desconhecida — qualquer coisa abaixo da última
imagem observada ainda pode ser capítulo, então parar cedo deve falhar fechado.

É errado para um leitor registrado. Medição real do leitor oficial: o reader termina em
~212530 e o documento em 218644 — cerca de 6000px de recomendações e rodapé vêm **depois**
do leitor. O scroll estabiliza no fim do reader e o fim do documento nunca é alcançado. Além
disso, as 167 imagens já chegam resolvidas no primeiro snapshot, então não há crescimento
nenhum a observar. As duas coisas são normais, e a regra antiga classificava um capítulo
completo como truncado.

| Estratégia | Adapter | Evidência de cobertura |
| --- | --- | --- |
| `reader_container` | Webtoons (registrado) | o coletor viu o reader parar de mudar pelas rodadas exigidas |
| `generic_document` | Universal (padrão) | fim do documento **e** estabilização |

A decisão pertence ao adapter (`coverage_strategy`), não ao downloader: não há
`if host == "webtoons.com"` espalhado, e há teste que falha se um host aparecer nessa função.

Ausência de crescimento **não** implica incompletude. O que distingue os casos é
`stabilized`: uma execução que apenas esgotou rodadas ou tempo enquanto ainda mudava não
está estabilizada e continua avisando `scroll_incomplete`.

O `UniversalChapterAdapter` não herda a regra permissiva — há teste aplicando exatamente a
forma medida do Webtoons ao universal e exigindo que ele continue recusando.

## Estratégia de coleta por adapter

O `BaseAdapter.analyze` executava `_collected_payload` — a varredura genérica (DOM amplo +
rede/CDP + JSON + iframes) — para **todo** adapter. Um job real do Webtoons registrou **823
candidatos, 90 aceitos**, com `cross_origin_iframe`, `network_log_limit` e
`network_json_limit`, numa página cujo reader tem 167 elementos e **zero iframes**. O adapter
específico era selecionado, mas quem coletava era a máquina genérica.

| Estratégia | Adapter | Coleta |
| --- | --- | --- |
| `adapter_specific` | Webtoons, VortexScans | somente o container do reader |
| `generic_multisource` | Universal (padrão) | DOM amplo + rede/CDP + JSON + clusterização + score |
| `local_snapshot` | LocalFolder | snapshot local, sem navegador |

Medido contra o leitor oficial após a correção: `collector=webtoons_reader`, **167**
candidatos, **0** de rede, **0** de JSON, **nenhum warning**. A decisão pertence ao adapter —
há teste que falha se um nome de host aparecer no roteamento do `analyze`.

### Lazy-load: 167 elementos não são 167 páginas

Medição da distribuição por host, ~5s após o load e **sem** scroll:

| Host | Elementos | Dimensões |
| --- | --- | --- |
| `webtoons-static.pstatic.net` | 164 | 1×1 |
| `webtoon-phinf.pstatic.net` | 3 | 800×1280 |

Os 164 são **placeholders** de lazy-load, não páginas. Um registro anterior afirmando "167
imagens válidas" media depois do scroll e inspecionava apenas as primeiras da amostra;
a distribuição nunca havia sido medida.

Consequências: `webtoons-static.pstatic.net` **não** é autorizado em `resource_hosts` — é o
host dos placeholders, e `exclude_candidate` já os descarta como `tracking_pixel`. E a
coleta específica precisa ocorrer **depois** de o lazy-load resolver, não logo após o load.
Isso explica os 90 aceitos do job real: parte das páginas ainda não havia resolvido.

Este ponto permanece **em aberto**: a coleta ainda não aguarda a resolução completa antes de
contar candidatos.

## Slots do leitor e lazy loading

O leitor entrega primeiro elementos de *placeholder*: o slot existe, guarda seu índice no
DOM e só depois troca pela página real. Classificar esse slot como descartado na primeira
leitura encolhe o capítulo em silêncio.

| Estado | Significado |
| --- | --- |
| `pending_lazy` | slot dentro do leitor que ainda não resolveu (sem URL, ou dimensões ≤ 2px) |
| `resolved_page` | URL final, host autorizado, dimensões acima do mínimo |
| `rejected` | mobília de interface, host não autorizado, ou imagem pequena já resolvida |

Um placeholder 1×1 aciona a regra de `tracking_pixel` só pelo tamanho — isso não é evidência
de que o slot é mobília, apenas de que ele não carregou. Por isso `slot_state` trata
`tracking_pixel` de forma diferente dos demais motivos de exclusão.

### Ordem

A ordem vem do índice do elemento no leitor, nunca da ordem em que as imagens terminam de
carregar. Há teste com slots resolvendo embaralhados e o manifesto saindo em ordem DOM.

### Hosts

`webtoon-phinf.pstatic.net` é host de página. `webtoons-static.pstatic.net` é o host dos
placeholders e **não** está em `resource_hosts` — um slot que "resolva" com dimensões reais
nesse host continua `rejected`, porque o host nunca foi autorizado.

### Cobertura

`reader_coverage_complete` exige pelo menos uma página resolvida e **zero** slots pendentes.
Slots `rejected` (recomendações, rodapé) não bloqueiam. Quando sobram pendentes, o
diagnóstico traz `total`, `resolved`, `pending`, `rejected` e os índices pendentes, e o
código é `incomplete_source_coverage` — nunca `incomplete_download`, já que nenhum download
ocorreu.

### Limitação

A classificação por estado está implementada e testada. O **laço de resolução** — rolar o
leitor em passos, reler os slots e esperar o lazy loading concluir — não foi implementado
nesta tarefa: percorrer o leitor inteiro força o carregamento das 167 imagens e produz o
manifesto completo do capítulo, que é a peça imediatamente anterior à captura. A arquitetura
está pronta para receber esse laço quando ele for aplicado a uma fonte com direitos.

## Resolvedor de slots lazy

`lazy_slot_resolver.resolve_lazy_reader_slots` visita as regiões pendentes do leitor, relê os
mesmos índices DOM e atualiza apenas aqueles slots, até nada ficar pendente ou o orçamento
acabar.

Todas as operações de navegador são injetadas — `read_slots`, `scroll_to`, `reader_bounds`,
`clock`, `sleep` — então o algoritmo roda em teste sem Selenium e sem rede. Ele observa
estado de elemento e **nunca busca bytes de imagem**.

| Garantia | Como |
| --- | --- |
| Ordem | saída sempre por índice DOM, nunca pela ordem de carregamento |
| Não regressão | slot já resolvido não volta a pendente numa releitura |
| DOM instável | inserção inesperada gera `reader_dom_changed`, sem renumerar em silêncio |
| Orçamento | `max_rounds`, `timeout_seconds`, `stable_rounds` — ao estourar, os slots ficam pendentes com índices reportados |
| Cancelamento | checado antes do scroll, depois do scroll, durante a espera e antes da releitura |
| Progresso | `counter_stage=source_lazy_resolution`, com `current`/`total` próprios — nunca contadores de download |

Fim do leitor, não fim do documento: o scroll fica dentro de `reader_bounds`.

### Não integrado

O resolvedor **não** está ligado ao `collect_reader_payload`. Ligá-lo faria a análise
percorrer o leitor inteiro forçando o carregamento de todas as páginas, o que produz o
manifesto completo do capítulo — a peça imediatamente anterior à captura. A peça está pronta
para ser conectada a uma fonte com direitos, inclusive à entrada por pasta local.

## Dívida técnica: análise de fonte no submit

`ui_bridge.start` continua executando a análise com Selenium dentro da requisição HTTP, o que
mede 93–101s na prática e deixa a UI em "Analisando fonte" durante o await.

A migração para o worker não foi feita: `start` tem 532 linhas e 23 pontos ligados ao caminho
síncrono, e mover isso significa mudar o contrato STAGING→QUEUED, levar a análise para o laço
de claim, tratar retomada de `awaiting_source_review` a partir de outro processo, idempotência
após restart e cancelamento entre processos. É uma migração que merece uma sessão própria, não
o fim de uma longa.

Desenho pretendido: `start` valida payload, calcula fingerprint, bloqueia duplicidade, cria o
job em `queued`, garante worker e devolve `job_id` imediatamente; o worker executa
`source_validation` → `browser_loading` → `source_analysis` → `source_lazy_resolution` →
`source_selection` e segue para o download.
