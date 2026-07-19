# Auditoria funcional — fontes de capítulo e submissão de jobs

## Escopo e método

Esta é uma auditoria documental da implementação atual do caminho de fonte de capítulo, da
submissão à fila e das fronteiras de segurança associadas. Ela separa deliberadamente três tipos
de evidência:

| Rótulo | Significado |
| --- | --- |
| **IMPLEMENTADO** | comportamento encontrado no código atual |
| **COBERTO_HERMETICAMENTE** | há teste local/fake direcionado ao contrato |
| **NÃO_EXECUTADO_AQUI** | exige site, browser, OCR, provider ou smoke autorizado; não é declarado como aprovado por este documento |

A atualização desta auditoria não executa URL de capítulo, Webtoon, VortexScans, NVIDIA, Chrome,
Selenium, OCR de capítulo nem script manual. Portanto, ela não transforma testes de fixture em
homologação de uma fonte externa.

## Fluxo de decisão

1. A UI/CLI exige uma única origem: URL pública ou pasta local.
2. Para URL, o registro escolhe primeiro um adapter específico; somente uma URL pública não
   reclamada chega ao fallback universal.
3. A análise usa a mesma interface de evidência para adapters específicos e universal:
   coleta limitada, clusterização, score/resultado, manifesto de IDs opacos e diagnóstico
   sanitizado.
4. Um resultado médio aguarda revisão antes de OCR; cobertura incompleta, challenge, autenticação,
   canvas inacessível ou reader cross-origin falham fechados.
5. Para pasta local, a fonte é validada, snapshotada e encaminhada por referência opaca; ela não
   passa por navegador ou downloader remoto.
6. O worker recebe comando em lista de argumentos e somente inicia o pipeline depois de o job
   atingir estado enfileirado.

Nenhuma etapa converte uma URL genérica em fonte “homologada”, nem converte caminho local em URL
de arquivo.

## Estado funcional por área

| Área | Comportamento atual | Evidência disponível | Status |
| --- | --- | --- | --- |
| Registro HTTP(S) | Webtoons e VortexScans são específicos; outros hosts públicos podem seguir para UniversalChapterAdapter | testes de seleção/host em test_chapter_sources.py | COBERTO_HERMETICAMENTE |
| Contrato de adapter | hooks para espera, coletores DOM/rede/JSON, cluster, score, manifesto, comando e erro sanitizado | testes de contrato/fixtures em test_chapter_sources.py e test_universal_chapter_adapter.py | COBERTO_HERMETICAMENTE |
| Análise específica | adapters específicos passam pelo mesmo contrato de análise e manifesto aceito, sem atalho DOM legado | inspeção de chapter_source.py e down.py; testes de adapter | IMPLEMENTADO |
| Resolução lazy Webtoons | slots `pending_lazy` são resolvidos com o driver Selenium já aberto antes de `source_selection`; placeholders não entram no manifesto | test_webtoons_reader_bridge.py com driver falso, progresso, timeout, cancelamento e E2E sintético | COBERTO_HERMETICAMENTE |
| VortexScans | host literal vortexscans.org, path de capítulo restrito, seletores próprios e grants de recurso por instância | fixtures/DNS falso em test_chapter_sources.py | COBERTO_HERMETICAMENTE |
| Fallback universal | DOM, lazy attributes, JSON reconhecido, Shadow DOM aberto, iframe same-origin, canvas exportável e eventos de rede já observados | fixtures/driver fake em test_universal_chapter_adapter.py | COBERTO_HERMETICAMENTE |
| CDP/performance | metadados sanitizados e JSON pequeno já recebido podem contribuir; texto não é executado e corpos não são persistidos | fixtures de performance/CDP no teste do adapter universal | COBERTO_HERMETICAMENTE |
| Revisão visual de fonte | somente data URI local e limitada derivada de imagem DOM já carregada; sem URL/fetch remoto; CORS/tainted ou fonte apenas de rede ficam sem miniatura, mas mantêm metadados | test_review_thumbnails_are_bounded_data_uris_and_never_source_urls e contrato estático de UI em test_ui_integration.py | COBERTO_HERMETICAMENTE |
| Polling da revisão | para o mesmo job aguardando revisão, não rerenderiza o DOM e preserva exclusão/reordenação na aba atual; reload/troca de job não é persistência | contrato estático de shouldRenderSourceReview em test_ui_integration.py | COBERTO_HERMETICAMENTE |
| Paginação genérica limitada | somente fallback universal: href same-origin/same-path com query numérica N+1, sem click; limite de 24 avanços/45 s e agregação de candidatos aceitos | PaginatedReaderSafetyTests em test_downloader_regressions.py | COBERTO_HERMETICAMENTE |
| Perfil universal | dica por host exato, expira, não aumenta score nem concede acesso; criação depende de opt-in de configuração e seleção fresca | test_source_profile.py e lógica de job_runner.py | COBERTO_HERMETICAMENTE |
| Pasta local | raiz permitida, validação de bytes, snapshot de nomes gerados e referência opaca | test_local_folder_source.py e test_local_folder_input.py | COBERTO_HERMETICAMENTE |
| UI de pasta local | seletor URL/Pasta local; caminho aceito só com bind e peer loopback | implementação em app_ui.py, ui_bridge.py e UI estática | IMPLEMENTADO |
| CLI de pasta local | run_webtoon.py aceita local-folder e encaminha snapshot; run_local_folder.py aceita apenas snapshot-ref opaco | test_local_folder_cli.py | COBERTO_HERMETICAMENTE |
| Páginas lógicas | entrada local preserva ordem/dimensões e pula Smart Split | test_logical_pages.py e test_local_pipeline_e2e.py | COBERTO_HERMETICAMENTE |
| Manifest de saída | versão 2 registra source_type, adapter, versão e transporte; leitor continua aceitando manifest versão 1 | output_manifest.py e testes de manifest existentes | IMPLEMENTADO |
| Fila/UI | job local começa em staging, é snapshotado antes de entrar em queued e nunca persiste o caminho bruto | inspeção de ui_bridge.py/job_store.py; testes de fila existentes | IMPLEMENTADO |
| Teste de ponta a ponta local | imagens sintéticas → snapshot → job → PDF de fake_pipeline | test_local_pipeline_e2e.py | COBERTO_HERMETICAMENTE |

“Coberto hermeticamente” significa que o cenário foi desenhado para rodar sem rede, com
arquivos temporários, fakes ou drivers falsos. O resultado de uma execução concreta da suíte deve
ser registrado no relatório de CI/validação, não inferido desta tabela.

## Fontes e resultados controlados

### URL específica

Webtoons preserva seus próprios hosts e seletores. VortexScans aceita somente o host literal
vortexscans.org e paths no formato geral de capítulo. Para Vortex, uma URL de CDN não é aceitada
como página de leitor; um recurso só pode ser autorizado depois de observação/validação na
instância recém-criada do adapter. A autorização é efêmera e não vira allowlist global.

Ainda não há afirmação de compatibilidade real com VortexScans: a execução externa controlada
permanece NÃO_EXECUTADA_AQUI.

### URL universal

O fallback universal aceita apenas URL HTTP(S) pública e validada. A decisão usa limites e
resultados explícitos:

| Resultado | Efeito |
| --- | --- |
| supported_specific_adapter | segue pelo adapter específico, sujeito à cobertura e aos limites |
| supported_generic_high_confidence | pode entrar em seleção automática |
| review_required_medium_confidence | permanece em awaiting_source_review antes de OCR |
| incomplete_download | não oferece capítulo parcial |
| unsupported_low_confidence | não baixa páginas |
| challenge/authentication/canvas/cross-origin reader | encerra de modo codificado, sem contorno |

O browser pode ajudar a observar recursos que já foram carregados, mas o fallback não tenta APIs
privadas, não executa JSON/texto extraído nem atravessa iframe cross-origin. Ele só tem paginação
genérica por href: a âncora precisa ser visível, sem target/download, ter mesma origem e path,
preservar a query estável e avançar exatamente de N para N+1 em uma chave convencional numérica.
A navegação é `driver.get(href)`, nunca click/handler; cada superfície recebe scroll e análise,
e só os candidatos aceitos entram no agregado. Há máximo de 24 avanços e 45 segundos.

Qualquer controle page-shaped que não prove essa forma, ciclo, redirect inesperado, timeout,
scroll incompleto ou análise posterior inutilizável recebe `pagination_incomplete`; a cobertura
bloqueia a seleção, inclusive pelo caminho de revisão manual, e não segue como capítulo parcial.
Load more, botões sem href, carrosséis, sliders e demais mecanismos interativos continuam pedindo
adapter específico, não uma regra por site no downloader.

Na revisão humana, a UI recebe IDs opacos, dimensões e, quando possível, uma prévia `data:image`
local gerada de imagem DOM já carregada. A UI não recebe URL da página para montar uma miniatura e
não faz request remoto de imagem; CORS/tainted, ausência de DOM visível ou candidato só de rede
deixam a prévia ausente sem apagar os metadados. Para o mesmo job em revisão, a atualização por
polling preserva o DOM atual e, com ele, exclusões e reordenações feitas na aba. A garantia não se
estende a reload do navegador nem a troca de job.

### Pasta local

A pasta local é uma entrada offline apenas para aquisição. Ela aceita arquivos diretos com
extensões e bytes validados, ordem natural, limites de arquivo/capítulo e detecção de duplicata.
Uma falha em qualquer página recusa a entrada completa. O snapshot gera nomes internos e o
manifest/jogo de dados persistido contém fingerprint e IDs, não o caminho nem o nome original da
página.

A UI só libera essa origem com servidor e cliente loopback. A CLI também exige raiz permitida e
confinamento da saída em output/. O pipeline real posterior continua podendo usar OCR/tradução
configurados; o E2E hermético usa fake_pipeline, não NVIDIA ou OCR real.

## Segurança e privacidade verificadas no desenho

- URL, redirect e host de recurso passam por validação; endereços não públicos são recusados.
- A análise e os relatórios usam IDs/fingerprints; não foram desenhados para persistir query,
  cookies, headers privados, request IDs, tokens, corpos ou pixels de canvas.
- A pasta local não usa file://; paths relativos, traversal, UNC, dispositivos, symlinks e
  junctions são recusados.
- Originais da pasta local não são alterados. A cópia para output exige snapshot válido e não
  remove uma pasta de saída não marcada como pertencente ao pipeline.
- Requests e browser-session usam limites compartilhados; Cloudscraper é opcional por flag
  exata e não altera política de challenge.
- A suíte padrão instala bloqueio de rede antes da importação de testes e exclui markers network
  e manual.

Esses controles reduzem risco, mas não são uma garantia total de SSRF para URLs arbitrárias:
subrecursos podem ser solicitados durante renderização e existe janela DNS TOCTOU. Uma implantação
de produção para URLs não confiáveis ainda requer política de egress/isolamento adequada.

## Itens que continuam intencionalmente fora do escopo

| Item | Situação correta |
| --- | --- |
| Clicar em controles, load more, carrossel ou slider | não implementado; adapter específico necessário |
| Paginação além do href same-origin/same-path com query N+1 | não implementado; adapter específico necessário |
| Ler iframe cross-origin ou Shadow DOM fechado | não implementado; não há contorno |
| Resolver CAPTCHA/login/DRM/paywall | não implementado; fluxo para com motivo codificado |
| Homologar qualquer site público | não prometido |
| Confirmar Vortex contra site real | NÃO_EXECUTADO_AQUI; requer smoke autorizado separado |
| Confirmar OCR/tradução/PDF reais com pasta local | NÃO_EXECUTADO_AQUI; o teste integrado é sintético |
| Tratar score alto como garantia de tradução | não é uma garantia |
| Usar caminho de usuário como chave/URL no job ou output | não é permitido pelo desenho |

## Próxima validação recomendada

1. Rodar a coleta e as suítes herméticas relevantes, mantendo a guarda offline ativa.
2. Revisar o diff para garantir que snapshots, cache e outputs não foram rastreados.
3. Só então, se houver autorização operacional explícita, executar no máximo o smoke externo
   específico necessário, com limites, sem paralelo e registrando resultado separado.
4. Não promover uma limitação de leitor a “suporte universal” apenas porque uma fixture passou.

Para detalhes contratuais consulte [Adapters de fonte](SOURCE_ADAPTERS.md), para caminhos locais
consulte [Entrada por pasta local](LOCAL_FOLDER_INPUT.md) e para limites de rede consulte
[Segurança](SECURITY.md).
