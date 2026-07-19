# Auditoria funcional — início de tradução e fontes suportadas

Escopo desta auditoria: o caminho de submissão da tradução (o botão que não iniciava), a
seleção de fonte por adapter, a visibilidade da fila e a filtragem de fixtures na UI.

Não é um inventário completo do projeto. As áreas verificadas estão abaixo com o método real
usado; o que não foi executado está marcado como tal, sem presunção de PASS.

## Causa raiz do botão "Iniciar tradução"

O clique **funcionava** e o job **era persistido**. O que faltava era um consumidor e um
relato honesto do estado:

1. `UiBridge.start()` gravava o job com status `queued` e retornava `ok: true`.
2. `runtime_state()` só considerava "em andamento" os status `IN_FLIGHT`. Um job `queued`
   caía no ramo `else` e a UI reportava `status: "ready"` — "pronto".
3. O frontend chamava `setRunControls(true)` otimisticamente; o poll seguinte (850 ms) lia
   `queue_running: false` e revertia o botão. Efeito visível: **nada aconteceu**.
4. Nenhuma camada verificava se havia worker para reivindicar a fila.

Cliques repetidos empilhavam jobs idênticos, todos invisíveis.

## Correções

| Camada | Correção |
| --- | --- |
| `ui_bridge.start()` | `ensure_worker()` garante um consumidor; o resultado do worker volta no payload |
| `ui_bridge.start()` | `_pending_duplicate()` impede o mesmo capítulo de ser enfileirado duas vezes |
| `ui_bridge.runtime_state()` | job `queued` reporta `status: queued`, `pending: true`, `queue_running: true` |
| `ui_bridge.runtime_state()` | `blocked` + `blocked_reason: worker_offline` quando há fila sem worker |
| `ui_bridge._latest_terminal_job()` | fixtures não são apresentadas como resultado |
| `ui_helpers.build_run_command()` | seleção de fonte explícita antes de qualquer persistência |
| `app_ui.py` | erro estruturado (`code`, `stage`, `message`, `action`) em vez de 400 seco |
| `static/tradutor_ui.js` | "Iniciando processamento…", painel de erro, guarda de clique duplo, reset da pré-visualização |

## Fontes suportadas

A seleção vive em [chapter_source.py](../chapter_source.py). Um adapter específico sempre
vence; uma URL HTTP(S) pública sem adapter registrado pode usar o
`UniversalChapterAdapter`, mas somente como fallback controlado. Isso não marca o host como
suportado nem permite buscar recursos arbitrários dele.

| Adapter | Hosts | Runner | Registrado |
| --- | --- | --- | --- |
| `WEBTOONS` | `webtoons.com`, `webtoon.com` (e subdomínios) | `run_webtoon.py` | sim |
| `GenericImageChapterAdapter` | os que você passar no construtor | `run_webtoon.py` | **não** — template |
| `UniversalChapterAdapter` | URL HTTP(S) pública submetida, validada por DNS | `run_webtoon.py` | fallback por execução |

`GenericImageChapterAdapter` existe para você registrar uma fonte própria cujo leitor seja
imagens lazy-loaded simples. Ele **não** é fallback: precisa ser instanciado com hosts
explícitos e adicionado a `ADAPTERS`. Registrar uma fonte é um ato deliberado que afirma
duas coisas — que o adapter a suporta e que você tem o direito de ler aquele conteúdo.

Cada adapter específico é dono do próprio conhecimento de leitor (`reader_selectors`,
`classify_candidate`, `exclude_candidate`). O fallback, por sua vez, observa evidência de
DOM/recursos já vistos pelo navegador/JSON inline/canvas local, agrupa candidatos e toma
uma decisão auditável: score `>= 0,85` segue automaticamente, `0,60–0,84` pede confirmação
humana e score menor falha fechado. Limite de coleta, scroll não comprovadamente completo
ou mais de 400 páginas resulta em `incomplete_download`, sem OCR nem confirmação parcial.
Ele não tenta resolver challenge, autenticação, DRM ou canvas protegido.

URL sem HTTP(S), credenciais, host local/privado/reservado ou DNS não público continua sendo
rejeitada antes do navegador. Redirects são revalidados e hosts de recurso só entram na
política efêmera depois de aparecerem no cluster selecionado. O diagnóstico público usa IDs,
host e impressão digital do caminho, não URL completa, query, cookie ou header. Veja
[UNIVERSAL_CHAPTER_ADAPTER.md](UNIVERSAL_CHAPTER_ADAPTER.md) para o contrato completo.

## Estados de job

| Estado | Significado |
| --- | --- |
| `awaiting_source_review` | análise de fonte mediana; páginas aguardam confirmação antes de OCR/worker |
| `queued` | aceito e aguardando worker — a UI mostra como em andamento |
| `claiming` / `starting` / `running` | em voo, com processo validado |
| `interrupted` | processo sumiu sem prova de conclusão (`process_not_found`) |
| `cancelled` | cancelado pelo usuário, inclusive durante a revisão de fonte |
| `failed` | erro explícito |
| `finished` / `review_required` | conclusão com prova (exit 0 + manifest + PDF) |

Transições validadas no repositório, fail-closed. Ver
[SOCIAL_ASSET_RETENTION_RECONCILIATION.md](SOCIAL_ASSET_RETENTION_RECONCILIATION.md) para o
ciclo de retenção dos PDFs publicados.

## Matriz funcional

Método: `unit` = teste hermético; `manual` = execução real local; `not_run` = não exercitado
nesta tarefa.

| Área | Função | Arquivo | Método | Resultado | Status |
| --- | --- | --- | --- | --- | --- |
| Submit | job persistido com id | `ui_bridge.start` | unit | job `queued` criado | PASS |
| Submit | consumidor garantido | `ui_bridge.ensure_worker` | unit | chamado uma vez por submit | PASS |
| Submit | worker offline relatado | `ui_bridge.start` | unit | `worker.online: false` no payload | PASS |
| Submit | clique duplo | `_pending_duplicate` | unit | 1 job, `duplicate: true` | PASS |
| Submit | URL inválida | `build_run_command` | unit | erro, nada persistido | PASS |
| Submit | cache + force | `build_run_command` | unit | rejeitado | PASS |
| Fonte | host conhecido | `select_adapter` | unit | adapter `webtoons` | PASS |
| Fonte | URL pública sem adapter | `select_adapter` + análise | unit | fallback universal, sujeito a score/revisão | PASS |
| Fonte | URL insegura ou DNS privado | validação de URL | unit | `unsupported_source` antes do navegador | PASS |
| Fonte | host sósia de adapter específico | `BaseAdapter.supports` | unit | não é reivindicado pelo adapter específico; fallback ainda exige análise pública | PASS |
| Fonte | subdomínio permitido | `BaseAdapter.supports` | unit | aceito | PASS |
| Fonte | credenciais na URL | `validate_url` | unit | rejeitado | PASS |
| Fonte | score médio | análise universal | unit | `awaiting_source_review`, sem OCR | PASS |
| Fonte | score baixo | análise universal | unit | falha fechada, sem runner | PASS |
| Fonte | normalização | `normalize_url` | unit | fragmento removido, host minúsculo | PASS |
| Fonte | comando é lista | `build_run_command` | unit | sem string de shell | PASS |
| Fila | `queued` não é "pronto" | `runtime_state` | unit + manual | `status: queued` | PASS |
| Fila | worker offline | `runtime_state` | unit + manual | `blocked_reason: worker_offline` | PASS |
| Fila | fila vazia | `runtime_state` | unit | `ready`, não bloqueado | PASS |
| Painel | fixture não vira resultado | `_latest_terminal_job` | unit + manual | `latest: None` | PASS |
| Painel | fixture sinalizada | `_is_presentable_result` | unit | filtrada | PASS |
| Painel | resultado real aparece | `_latest_terminal_job` | unit | job real retornado | PASS |
| Frontend | rótulo em voo | `tradutor_ui.js` | unit (fonte) | "Iniciando processamento…" | PASS |
| Frontend | controles só após aceite | `startTranslation` | unit (fonte) | `await` antes de `setRunControls` | PASS |
| Frontend | guarda de clique duplo | `startTranslation` | unit (fonte) | `dataset.busy` | PASS |
| Frontend | painel de erro | `showStartError` | unit (fonte) | com "Tentar novamente" | PASS |
| Frontend | erro sem segredos | `showStartError` | unit (fonte) | sem traceback/token/`.env` | PASS |
| Frontend | reset da pré-visualização | `resetRunPreview` | unit (fonte) | presente | PASS |
| Job | reconciliação de órfão | `reconcile_orphans` | unit | `interrupted` | PASS |
| Job | timer congelado | `_duration` | unit | congela no último sinal | PASS |
| Job | cancelamento sem processo | `cancel` | unit | `cancelled` | PASS |
| Retenção | ciclo completo | `social_asset_retention` | unit + smoke real | 52/52 no smoke | PASS |
| Comunidade | leitura autenticada | `social_content` | unit | owner 200 / anônimo 401 | PASS |
| Pipeline | OCR / tradução / PDF | `manga_translation_pipeline` | not_run | não exercitado nesta tarefa | NOT_RUN |
| Pipeline | extração de imagens | adapter | not_run | não exercitado nesta tarefa | NOT_RUN |
| UI | smoke de navegador | — | not_run | não executado | NOT_RUN |

Nada foi marcado PASS sem execução real. As linhas `NOT_RUN` são honestas: o pipeline de
tradução ponta a ponta e o smoke de navegador não foram exercitados nesta tarefa.

## Matriz de compatibilidade do fallback universal (fixtures herméticas)

Esta matriz descreve o contrato exercitado por fixtures/fakes locais; não é uma lista de
sites homologados nem substitui um smoke autorizado.

| Tipo de leitor | Fixture / evidência | Estratégia | Decisão esperada | Teste | Status |
| --- | --- | --- | --- | --- | --- |
| Vertical com imagens coerentes | 3+ páginas grandes no mesmo reader | DOM + cluster + ordem vertical | alta (`>= 0,85`) | `test_vertical_reader_is_high_confidence...` | PASS hermético |
| Lazy/infinite scroll com fim comprovado | candidatos após scroll estável | scroll incremental + nova análise | alta ou revisão conforme score | contrato de `analyse_driver` | PASS unitário |
| Scroll sem cobertura comprovada | aviso `scroll_incomplete` | bloqueio de completude | `incomplete_download` | `test_incomplete_scroll_and_page_limit...` | PASS hermético |
| `srcset`, background, shadow aberto e iframe same-origin | metadados DOM locais | coletor limitado, com ciclo/profundidade de iframe limitados | entra no cluster, nunca por regra isolada; teto falha fechado | `test_open_shadow_same_origin_iframe...`, `test_iframe_collection_has_cycle...` | PASS unitário |
| Manifest JSON inline | JSON já presente no DOM | parser limitado, sem executar texto | entra no cluster | `test_json_manifest_is_collected...` | PASS hermético |
| Recursos de imagem vistos pelo navegador | Performance entries limitados | metadados de recurso, sem corpo/CDP | bloqueia se exceder teto | `test_network_resource_cap...` | PASS hermético |
| Canvas visível exportável | PNG local em memória | canvas limitado + validação normal | pode entrar no cluster | `test_visible_canvas_capture...` | PASS hermético |
| Canvas não exportável | aviso de captura | falha fechada | `unsupported_canvas_reader` ou `incomplete_download` | `test_challenge_auth_zero_images_and_canvas...` | PASS hermético |
| Anúncios, thumbnails e duplicatas | sinais negativos locais | exclusão antes da seleção | não entram no PDF | `test_advertisements_thumbnails...` | PASS hermético |
| Iframe cross-origin / shadow fechado | inacessível | diagnóstico, sem contorno | revisão/falha | coletor limitado | LIMITADO |
| Paginação, próxima página, carrossel ou slider | não há clique genérico | adapter específico | não suportado pelo fallback | — | ADAPTER_REQUIRED |
| XHR/fetch com manifest só na resposta | sem CDP/corpo de resposta | adapter específico | não suportado pelo fallback | — | ADAPTER_REQUIRED |

## Códigos de falha

Falhas e decisões de análise de fonte têm código explícito; um job não deve ficar em
`queued`/`running` sem estado ou motivo. `awaiting_source_review` é uma pausa deliberada,
visível e cancelável antes do OCR.

| Código | Quando |
| --- | --- |
| `unsupported_source` | esquema proibido, credenciais na URL, alvo privado/reservado, DNS inseguro, rebinding ou recurso não autorizado |
| `source_not_ready` | leitor não carregou |
| `challenge_required` | CAPTCHA/Turnstile/desafio interativo — **paramos, não contornamos** |
| `authentication_required` | o leitor indica login/autenticação necessária |
| `source_access_denied` | 401/403 |
| `source_rate_limited` | 429/503 |
| `no_chapter_images` | zero imagens após a coleta |
| `unsupported_canvas_reader` | havia canvas de leitor, mas nenhuma captura local utilizável |
| `review_required_medium_confidence` | seleção genérica precisa de confirmação humana antes do OCR |
| `unsupported_low_confidence` | não houve evidência suficiente para escolher páginas com segurança |
| `invalid_image_response` | MIME fora de `ALLOWED_IMAGE_MIME`, ou HTML disfarçado de imagem |
| `incomplete_download` | conjunto de páginas incompleto |

`sanitize_error()` devolve só o código ou o nome da classe da exceção — nunca a mensagem,
que poderia carregar URL, cookie ou header.

## SSRF e falsificação de host

| Vetor | Resultado |
| --- | --- |
| `evil-webtoons.com`, `webtoons.com.evil.net` | não são atribuídos ao adapter Webtoons; se públicos, só podem seguir pelo fallback e sua análise independente |
| `file:`, `data:`, `ftp:`, `gopher:` | rejeitado |
| `https://user:senha@host/` | rejeitado |
| `127.0.0.1`, `::1`, `10/8`, `192.168/16`, `172.16/12` | rejeitado |
| `169.254.169.254` (metadados de nuvem) | rejeitado |
| hostname público que **resolve** para IP interno | rejeitado (checagem por DNS, não só literal) |
| hostname que não resolve | rejeitado — não buscamos o que não conseguimos localizar |
| redirect de navegação | cada hop público é revalidado e limitado; host de recurso não vira destino de navegação |
| redirect de imagem | cada hop é revalidado contra a política efêmera do cluster |

A validação roda **antes** de qualquer driver abrir: há teste que afirma
`_create_driver` não foi chamado.

## Segurança verificada

- comando montado como lista de argumentos, nunca string de shell
- adapters específicos têm allowlist; fallback genérico só aceita URL pública validada e recursos observados no cluster vencedor
- credenciais na URL rejeitadas
- DNS é reavaliado; mudança de resposta durante a execução falha fechada
- limites compartilhados de tempo, redirects, arquivos e bytes impedem coleta ilimitada
- nome de saída sanitizado (`sanitize_output_name`)
- painel de erro sem traceback, token, chave, caminho ou conteúdo de `.env`
- nenhuma credencial nova; nenhum `service_role`; nenhuma publicação automática

## Limitações

- Só existe um adapter concreto (`webtoons`). O fallback universal não substitui um adapter
  específico nem promete suporte a outras fontes; leitores não observáveis podem exigir
  revisão ou falhar fechados.
- A política cobre a navegação e as imagens selecionadas, não o isolamento completo de cada
  subrecurso que o navegador carrega ao renderizar a página; sem proxy/firewall de egress ou
  interceptação no navegador, ela não é uma garantia de SSRF para URL arbitrária não confiável.
- O fallback não clica paginação/carrossel/slider, não inspeciona XHR/fetch via CDP, não lê
  iframe cross-origin/Shadow DOM fechado e não mostra thumbnails reais. Esses leitores exigem
  adapter específico.
- O worker é iniciado sob demanda no submit; não há scheduler.
- A auditoria cobre o caminho de submissão, não o pipeline de tradução inteiro.

## Como rodar

```
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest test_translation_start.py -q
node --check static/tradutor_ui.js
```
