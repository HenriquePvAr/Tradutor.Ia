# Fila de worker persistente

O pipeline de tradução roda em um **worker independente**, fora do processo da UI.
Fechar ou reiniciar a UI não interrompe um capítulo em andamento.

## Processos

- `app_ui.py` — interface e API. Cria/consulta/cancela/retoma jobs. **Não** executa o
  pipeline.
- `worker_service.py` — worker independente. Reclama um job por vez (concorrência 1),
  inicia um runner isolado, mantém heartbeat/lease e recupera jobs abandonados.
- `job_runner.py` — executa **um** capítulo: escreve manifest inicial, roda o pipeline,
  emite progresso/checkpoints e encerra com exit code confiável.

Fonte de verdade do estado dos jobs: `.cache/runtime/jobs.sqlite3` (SQLite, WAL).
Logs por job: `.cache/runtime/logs/<job_id>.log`. Ambos são ignorados pelo Git.

## Como iniciar

```
python start_tradutor.py            # inicia o worker (destacado) e a UI
python start_tradutor.py worker     # só o worker (destacado)
python start_tradutor.py ui         # só a UI
python start_tradutor.py status     # saúde do worker e da fila
python start_tradutor.py stop-worker [--force]   # parada graciosa do worker
```

No Windows há também `start_tradutor.bat`.

## Estados do job

`queued → claiming → starting → running → finished | review_required`, com
`cancelling → cancelled`, `running → interrupted → resumable → queued` (retomada) e
`failed`. Transições inválidas são rejeitadas (fail-closed).

## Worker offline

Se nenhum worker estiver online, um novo job fica `queued` e a UI mostra “aguardando
worker”. A UI **nunca** executa o pipeline como fallback. Ao iniciar um worker, ele
drena a fila.

## Cancelamento

O botão de cancelar seta uma flag no banco; o runner a observa, encerra o pipeline pela
árvore de processos validada (PID + start time + linha de comando) e preserva os
artefatos. Nenhum processo é encerrado por nome.

## Parada e interrupção

`stop-worker` pede parada graciosa via banco (alcança até um worker destacado). O worker
encerra a **árvore inteira** do runner ativo e marca o job como `interrupted`
(retomável). `--force` é fallback: encerra a árvore validada do worker; nunca toca em um
processo cuja linha de comando não seja a do worker.

## Interrupção e recuperação

Se o worker morre, seu runner pode continuar vivo. Um novo worker detecta o job órfão
(pelo lease do worker dono expirado, não pelo heartbeat do job, que é escrito pelo
runner) e **reconcilia**: encerra a árvore do runner validado e marca `interrupted`. Um
PID reutilizado por outro processo nunca é encerrado — o job é marcado
`ownership_mismatch` (falha fechada). Nunca há dois attempts ativos para o mesmo
capítulo.

## Retomada

Um job `interrupted`/`resumable` pode ser retomado pela UI: cria um novo attempt
(`attempt+1`, com `previous_job_id`) reusando o mesmo diretório de saída; checkpoints
válidos de estágios já concluídos são reaproveitados. A retomada é **bloqueada**
enquanto o runner do attempt anterior ainda estiver vivo
(`previous_attempt_still_running`).

## Concorrência

Concorrência inicial: **1**. Dois workers não executam o mesmo job (claim atômico). Um
segundo worker que encontra um lease saudável sai de forma limpa.

## Compatibilidade legada

Outputs antigos, sem registro no banco, continuam aparecendo no histórico via descoberta
de `output/`. Nada é migrado automaticamente.

## Troubleshooting

- **UI abriu, worker offline** → `python start_tradutor.py worker`; o job sai de `queued`
  sozinho.
- **Job preso em `queued`** → confirme o worker com `status`.
- **Job `interrupted`** → use “Retomar” na UI (cria `attempt 2`).
- **Worker duplicado** → o segundo sai limpo; verifique com `status`.
- **Porta 8080 ocupada** → outra UI já está rodando.
- **Banco bloqueado** → operação concorrente momentânea; o WAL + busy_timeout resolvem;
  não apague o banco.
- **Processo órfão após crash** → inicie um worker; ele reconcilia e encerra a árvore
  órfã. Não encerre processos por nome.
