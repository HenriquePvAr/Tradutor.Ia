# Adaptador universal de capítulos

## Objetivo e escopo

`UniversalChapterAdapter` é um fallback **controlado** para uma URL pública HTTP(S) que
não corresponde a um adapter específico. Ele não declara que um domínio, um leitor ou
qualquer site seja suportado. O objetivo é analisar a página que o usuário pediu,
encontrar evidência suficiente de um conjunto de páginas e só então permitir uma coleta
limitada.

Adapters específicos continuam tendo prioridade. Eles mantêm seus próprios seletores e
conhecimento do leitor. O adapter universal só entra depois deles e não altera as regras
de um adapter específico.

Use somente conteúdo que você tenha o direito de acessar. O fallback não contorna login,
CAPTCHA, paywall, DRM, bloqueios anti-bot ou proteção de canvas.

O contrato de fontes v1 está em [SOURCE_ADAPTERS.md](SOURCE_ADAPTERS.md). O adapter universal
implementa hooks genéricos, mas não substitui conhecimento de reader que deve pertencer a um
adapter específico.

## Decisão antes do OCR

A UI faz uma análise de fonte antes de criar saída de capítulo, iniciar OCR ou colocar um
runner para trabalhar. A análise devolve somente metadados sanitizados e uma das decisões
abaixo:

| Resultado | Regra | Efeito |
| --- | --- | --- |
| `supported_specific_adapter` | o adapter específico venceu e há evidência de página | segue pelo caminho específico |
| `supported_generic_high_confidence` | melhor cluster genérico com score `>= 0,85` | seleção automática; job pode entrar na fila |
| `review_required_medium_confidence` | score `>= 0,60` e `< 0,85` | job fica em `awaiting_source_review`; o usuário confirma as páginas antes do OCR |
| `unsupported_low_confidence` | score `< 0,60` | falha fechada; nenhum OCR ou download de capítulo é iniciado |
| cobertura incompleta | a observação, scroll, paginação ou resolução lazy não comprovou cobertura completa, ou o conjunto excedeu um limite | falha fechada; o worker persiste `incomplete_source_coverage`, não oferece seleção parcial nem inicia OCR |
| `no_chapter_images`, `challenge_required`, `authentication_required`, `unsupported_canvas_reader` ou `unsupported_cross_origin_reader` | não há evidência utilizável ou há uma barreira | falha fechada com motivo codificado |

Na revisão, o cliente recebe IDs opacos e metadados de candidatos, não URLs remotas. Para uma
imagem DOM já visível e já carregada pelo navegador, pode receber também uma miniatura opcional:
um `data:image` local gerado no próprio navegador de análise. Ela é limitada a 64 candidatos,
24.000 caracteres por item e 1.000.000 de caracteres no conjunto; a UI aceita apenas URI JPEG/
PNG base64 dentro desses limites e não faz fetch remoto para exibir a prévia. Imagem somente de
rede, DOM ausente/oculto, CORS/canvas tainted ou qualquer falha de geração simplesmente deixam o
candidato sem miniatura; ID, ordem e dimensões continuam disponíveis para revisão.

A confirmação precisa escolher um subconjunto não vazio dos IDs. Enquanto o mesmo job permanece
em `awaiting_source_review`, os ticks de polling não substituem o DOM já renderizado: exclusões e
reordenação feitas na aba atual são preservadas até confirmar ou cancelar. Isso não é persistência
de uma edição de revisão após recarregar a página ou trocar de job. Quando o worker for executar,
ele observa o leitor novamente e confere a seleção contra a nova evidência; uma página que sumiu
ou mudou não é buscada silenciosamente.

Um resultado de revisão de fonte não é um PDF com `review_required`: é uma pausa anterior
ao OCR. `review_required` continua sendo o estado terminal de qualidade de uma execução
que gerou seus artefatos.

## De onde vêm os candidatos

A coleta genérica observa o leitor carregado pelo navegador, sem executar JavaScript de
terceiros adicional. Ela procura, de forma limitada, evidências como:

- `img`, `currentSrc`, atributos lazy, `data-page` e `srcset`/`picture`;
- links diretos para imagens e `background-image` CSS;
- raízes de *shadow DOM* abertas e iframes da mesma origem;
- recursos de imagem já observados pelo navegador, inclusive metadados CDP/performance
  sanitizados quando o driver os oferece;
- manifestos JSON inline pequenos, atributos `data-*` reconhecidos e respostas JSON pequenas
  já recebidas pelo navegador;
- canvas visível que o próprio navegador consegue exportar localmente.

Os candidatos são deduplicados, descartam sinais claros de interface (logo, avatar,
thumbnail, anúncio, pixel de tracking e dimensões pequenas) e são agrupados. O score usa
evidências combinadas, por exemplo tamanho de página, sequência de nomes, ordem vertical,
container de leitor, domínio comum e recursos já observados. Nenhum sinal isolado torna um
domínio autorizado.

Scroll incremental ajuda leitores com lazy loading. Se a coleta atingir um teto de DOM,
JSON, canvas ou candidatos, exceder 400 páginas, ou não comprovar fim **e** estabilização
do scroll, a fase de fonte termina como `incomplete_source_coverage`; nunca se apresenta esse
subconjunto como um capítulo completo.

No Webtoons, que é um adapter específico de reader-container, placeholders 1x1 são tratados como
slots `pending_lazy`, não como páginas nem como host de recurso. `webtoons_reader_bridge.py`
conecta o driver Selenium já aberto a `lazy_slot_resolver.resolve_lazy_reader_slots`: ele relê
apenas `#_imageList`/`img._images`, rola dentro dos bounds do reader e preserva a ordem DOM. Se
todos os slots resolvem, a seleção contém somente páginas reais; se sobram pendentes, ocorre
timeout ou o DOM muda, o runner não inicia e os índices pendentes ficam no diagnóstico público.

Iframes de mesma origem têm conjunto de documentos visitados, profundidade e quantidade
limitados. Um ciclo ou teto de iframe também vira cobertura incompleta de fonte, em vez de
consumir o tempo da análise ou afirmar cobertura completa. Iframe cross-origin nunca é atravessado:
quando há evidência de que ele seja o reader, o resultado é
`unsupported_cross_origin_reader`, e não um sucesso baseado em parte visível da página.

Há uma exceção estreita para paginação do fallback universal: ele pode seguir, sem clicar, um
`a[href]` explícito quando a URL tem mesma origem e mesmo path, os parâmetros não relacionados
à página permanecem idênticos e uma chave convencional de query numérica avança exatamente de
N para N+1. A navegação usa `driver.get(href)` depois de revalidar URL/path; não executa handler
fornecido pela página. Há no máximo 24 avanços, 45 segundos para a sequência e scroll limitado em
cada superfície adicional. Somente candidatos aceitos de todas as superfícies reanalisadas podem
formar o manifesto agregado.

Controle ambíguo, query não comprovada, ciclo, redirect inesperado, timeout, scroll incompleto ou
falha de análise posterior marca `pagination_incomplete`, que é uma falha de cobertura e não
permite seleção, inclusive pelo caminho de revisão manual, nem resulta em capítulo parcial.
O adaptador continua sem acionar `load more`, botões sem
`href`, carrosséis ou sliders; também não atravessa shadow DOM fechado ou iframe cross-origin e
não usa miniaturas remotas. A única prévia visual possível é o `data:image` local e limitado de
uma imagem DOM já carregada, que não participa da seleção nem do download. Pode ler metadados de
respostas XHR/fetch que o navegador já recebeu e, sob limites, extrair somente URLs de estruturas
JSON reconhecidas; não executa texto extraído nem persiste headers, query strings, request IDs ou
corpos. Esses formatos interativos ainda precisam de adapter específico quando forem necessários.

## Política de rede e limites

Antes de abrir o navegador, a URL enviada é validada. Só HTTP e HTTPS sem credenciais na
URL são aceitos. `localhost`, endereços loopback, privados, link-local, multicast,
reservados, não especificados ou hosts que resolvam para esses intervalos são recusados.
Um host que não resolve também é recusado.

O redirecionamento de navegação é pré-validado em hops limitados e sem cookies. Cada URL
do redirecionamento é resolvida e validada novamente. Para o adapter universal, mudanças
no conjunto de respostas DNS de um host durante a execução são tratadas como possível
DNS rebinding e a operação falha fechada.

Depois da análise, somente hosts de recursos que pertencem ao cluster vencedor e foram
observados naquele leitor são autorizados em memória para aquela execução. Cada hop de
redirecionamento de imagem também é revalidado. A autorização não é gravada nem se torna
uma allowlist global.

Os limites atuais de transporte são compartilhados pelo capítulo inteiro:

| Limite | Valor padrão |
| --- | --- |
| conexão / leitura | 10 s / 30 s |
| redirects por requisição | 3 |
| bytes por arquivo | 32 MiB |
| bytes totais | 1 GiB |
| arquivos | 400 |
| duração total de download | 900 s |
| paginação genérica por query | até 24 avanços adicionais / 45 s; apenas `href` same-origin/same-path N+1 |

As imagens passam por verificação de tipo, tamanho e decodificação. Respostas HTML,
challenges e arquivos que não se comportam como imagem são rejeitados; um teto excedido
termina a coleta com motivo explícito em vez de ampliar tentativas indefinidamente.

Há dois transportes normais: `requests` e, quando necessário, uma sessão de requests com
cookies de sessão do navegador. Os cookies ficam apenas em memória, são limitados ao
domínio da página e são descartados no fechamento. `CloudscraperTransport` é opcional e
só entra após `requests` e `browser_session` quando
`ENABLE_CLOUDSCRAPER_TRANSPORT=1`. Mesmo nesse caso usa os mesmos limites, revalidação e
detecção de challenge; um desafio interativo encerra o fluxo como `challenge_required`,
sem tentativa de evasão.

Essas garantias cobrem a navegação pré-validada e as requisições de imagem selecionadas
pelo downloader. Elas **não** transformam o Chrome em um sandbox completo de todos os
subrecursos que uma página pode pedir durante sua renderização: uma página pública pode
solicitar um subrecurso antes da validação posterior ao carregamento. Além disso, a
validação DNS do transporte `requests` não fixa o IP da conexão até o fim (há uma janela
TOCTOU). Sem proxy/firewall de egress ou interceptação de rede no navegador, isso não é
uma garantia de SSRF de produção para URLs arbitrárias não confiáveis. Iframes de outra
origem não são atravessados; a URL deve ser submetida apenas para fontes confiáveis e
autorizadas.

## Canvas

Canvas só é considerado quando está visível e tem área de leitor. A coleta tenta exportar
um PNG local com `toDataURL`; o dado fica apenas em memória, tem teto de 16 MiB e não é
colocado no relatório público, no banco ou em um perfil. Se a exportação for impossível
(por exemplo, canvas tainted ou protegido), a análise registra um aviso e não tenta
contornar a proteção. Se só houver canvas não capturável, o resultado é
`unsupported_canvas_reader`.

Uma captura válida ainda passa pela mesma validação de imagem e pelo mesmo agrupamento que
uma página de rede. Isso não constitui suporte genérico a leitores canvas, DRM ou conteúdo
protegido.

## Diagnóstico, privacidade e perfis

Relatórios de análise persistem o adapter, resultado, score, sinais, contagens, dimensões,
origem do candidato e IDs opacos. Para candidatos remotos, exibem host e uma impressão
digital do caminho; a URL-fonte em artefatos também usa uma impressão digital do caminho.
Nunca persistem URL completa, query string, cookie, header, token ou pixels de canvas.

Após uma execução genérica que termina tecnicamente completa **e** aprovada pelo quality
gate, o sistema pode guardar uma dica local em
`.cache/runtime/source_profiles.json`. A dica é por host exato e contém apenas evidência
sanitizada de container/sinais, score observado, modo de seleção e versão. Ela não contém
URL, query, cookie, token ou imagem.

Um perfil não concede acesso, não registra uma fonte como suportada e não seleciona páginas
sozinho. Numa visita futura ele apenas anota a evidência fresca de container que coincide
com o host exato; não muda score, ranking ou limiar de confiança. Toda a validação de URL,
DNS, observação e clusterização ocorre outra vez. Um perfil ausente, incompatível,
divergente ou associado a uma análise incompleta é ignorado.

## Limitações explícitas

- Não há promessa de suporte a qualquer site, plataforma, capítulo ou formato de leitor.
- Um score alto é evidência de coleta, não prova de direito de acesso, estabilidade do site
  ou qualidade da tradução posterior.
- Leitores dependentes de autenticação, desafios, conteúdo protegido, canvas inacessível,
  iframes cross-origin, APIs privadas, controles de paginação fora do `href` same-origin/
  same-path N+1, carrosséis/sliders ou estruturas não observáveis podem parar para revisão ou
  falhar fechados e normalmente exigem adapter específico.
- Não há bypass de challenge, login, DRM ou bloqueio anti-bot.
- A análise reabre um navegador porque o usuário submeteu uma URL; ela não é parte da suíte
  de testes padrão, que permanece hermética/offline.

Para o desenho geral do download, consulte [Arquitetura](ARCHITECTURE.md); para os estados
de fila, [Fila de worker](WORKER_QUEUE.md); e para os testes herméticos,
[Testes](TESTING.md).
