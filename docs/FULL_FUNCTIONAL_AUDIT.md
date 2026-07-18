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

A seleção é explícita e vive em [chapter_source.py](../chapter_source.py). Um host só é
buscado se algum adapter registrado o reivindicar; **não existe fallback** que tente um site
desconhecido — um palpite errado significaria buscar de um host que ninguém validou.

| Adapter | Hosts | Runner | Registrado |
| --- | --- | --- | --- |
| `WEBTOONS` | `webtoons.com`, `webtoon.com` (e subdomínios) | `run_webtoon.py` | sim |
| `GenericImageChapterAdapter` | os que você passar no construtor | `run_webtoon.py` | **não** — template |

`GenericImageChapterAdapter` existe para você registrar uma fonte própria cujo leitor seja
imagens lazy-loaded simples. Ele **não** é fallback: precisa ser instanciado com hosts
explícitos e adicionado a `ADAPTERS`. Registrar uma fonte é um ato deliberado que afirma
duas coisas — que o adapter a suporta e que você tem o direito de ler aquele conteúdo.

Cada adapter é dono do próprio conhecimento de leitor (`reader_selectors`,
`classify_candidate`, `exclude_candidate`), então `down.py` não guarda mais nenhum seletor
específico de site — há teste que falha se algum voltar para lá.

Host não registrado → `UnsupportedSource` (código `unsupported_source`), rejeitado **antes**
de o job existir. A UI mostra "Esta fonte ainda não é suportada." com a lista de hosts.

A checagem de host é por igualdade ou sufixo de ponto (`.webtoons.com`), nunca por sufixo de
string — `evil-webtoons.com` e `webtoons.com.evil.net` são rejeitados, o que está coberto por
teste.

Adicionar uma fonte = registrar um adapter em `ADAPTERS`. O contrato exige que o adapter
tenha direito de acessar o conteúdo daquela fonte.

## Estados de job

| Estado | Significado |
| --- | --- |
| `queued` | aceito e aguardando worker — a UI mostra como em andamento |
| `claiming` / `starting` / `running` | em voo, com processo validado |
| `interrupted` | processo sumiu sem prova de conclusão (`process_not_found`) |
| `cancelled` | cancelado pelo usuário ou fonte não suportada |
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
| Fonte | host desconhecido | `select_adapter` | unit | `unsupported_source` | PASS |
| Fonte | host sósia | `BaseAdapter.supports` | unit | rejeitado | PASS |
| Fonte | subdomínio permitido | `BaseAdapter.supports` | unit | aceito | PASS |
| Fonte | credenciais na URL | `validate_url` | unit | rejeitado | PASS |
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

## Códigos de falha

Um job sempre termina em um código; nunca fica preso em `queued`/`running`.

| Código | Quando |
| --- | --- |
| `unsupported_source` | host não registrado, esquema proibido, credenciais na URL, alvo privado, path que não é capítulo |
| `source_not_ready` | leitor não carregou |
| `challenge_required` | CAPTCHA/Turnstile/desafio interativo — **paramos, não contornamos** |
| `source_access_denied` | 401/403 |
| `source_rate_limited` | 429/503 |
| `no_chapter_images` | zero imagens após a coleta |
| `invalid_image_response` | MIME fora de `ALLOWED_IMAGE_MIME`, ou HTML disfarçado de imagem |
| `incomplete_download` | conjunto de páginas incompleto |

`sanitize_error()` devolve só o código ou o nome da classe da exceção — nunca a mensagem,
que poderia carregar URL, cookie ou header.

## SSRF e falsificação de host

| Vetor | Resultado |
| --- | --- |
| `evil-webtoons.com`, `webtoons.com.evil.net` | rejeitado (match exato ou sufixo de ponto) |
| `file:`, `data:`, `ftp:`, `gopher:` | rejeitado |
| `https://user:senha@host/` | rejeitado |
| `127.0.0.1`, `::1`, `10/8`, `192.168/16`, `172.16/12` | rejeitado |
| `169.254.169.254` (metadados de nuvem) | rejeitado |
| hostname público que **resolve** para IP interno | rejeitado (checagem por DNS, não só literal) |
| hostname que não resolve | rejeitado — não buscamos o que não conseguimos localizar |
| redirect para host não permitido | revalidado e rejeitado |

A validação roda **antes** de qualquer driver abrir: há teste que afirma
`_create_driver` não foi chamado.

## Segurança verificada

- comando montado como lista de argumentos, nunca string de shell
- allowlist de hosts, sem fallback para host desconhecido (superfície de SSRF fechada)
- credenciais na URL rejeitadas
- nome de saída sanitizado (`sanitize_output_name`)
- painel de erro sem traceback, token, chave, caminho ou conteúdo de `.env`
- nenhuma credencial nova; nenhum `service_role`; nenhuma publicação automática

## Limitações

- Só existe um adapter concreto (`webtoons`). Outras fontes exigem um adapter novo e o
  direito de acessar aquele conteúdo.
- O worker é iniciado sob demanda no submit; não há scheduler.
- A auditoria cobre o caminho de submissão, não o pipeline de tradução inteiro.

## Como rodar

```
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest test_translation_start.py -q
node --check static/tradutor_ui.js
```
