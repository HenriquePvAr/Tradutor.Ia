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

No Windows há também `start_tradutor.bat`. `stop` e `stop-worker` são o mesmo comando.

## Supervisão do worker

`all` **supervisiona** o worker que ele mesmo iniciou (`worker_supervisor.py`): se aquele
processo morre inesperadamente, um substituto é criado sob política limitada — backoff de
`2s`, `5s`, `15s`, no máximo `3` reinícios, e depois o launcher fica `degraded` em vez de
respawnar indefinidamente. Um worker que fica de pé por `120s` recupera o orçamento de
reinícios. A supervisão vive e morre com o processo do launcher e **nunca adota** um worker
que ele não criou — por isso `worker`, que sai imediatamente, inicia sem supervisionar.

O supervisor cuida apenas de **disponibilidade de processo**: nunca marca job como
interrompido, nunca retoma um job e nunca reordena a fila. Isso continua sendo do
`JobStore` e do worker substituto. Detalhes em
[Documentação Técnica §5](technical/DOCUMENTACAO_TECNICA.md#5-ciclo-de-vida-do-launcher-e-supervisão-do-worker).

## Estados do job

Uma análise genérica de fonte pode começar em `awaiting_source_review`. Esse estado não
está em voo: não há runner, download de capítulo ou OCR. O usuário confirma os IDs opacos
das páginas encontradas e o job então segue para `queued`; também pode ser cancelado.

Depois da confirmação, o fluxo é `queued → claiming → starting → running → finished |
review_required`, com `cancelling → cancelled`, `running → interrupted → resumable →
queued` (retomada) e `failed`. Transições inválidas são rejeitadas (fail-closed).

A taxonomia completa tem 14 estados — os acima mais `staging` (job sendo montado antes de
entrar na fila) e `source_analysis_ready` (fonte analisada, pendente de política de
workspace). Tabela e diagrama em
[Documentação Técnica §8](technical/DOCUMENTACAO_TECNICA.md#8-máquina-de-estados-do-job).

`review_required` é diferente de `awaiting_source_review`: o primeiro é terminal e só
existe após uma execução que gerou artefatos com revisão de qualidade pendente. O segundo
é uma revisão de seleção de páginas anterior ao OCR.

O registro do job pode guardar `reason_code`, análise sanitizada da fonte e seleção de
candidatos para auditoria. Ele não deve guardar URL completa de recurso, query, cookie ou
pixel de canvas.

## Worker offline

Se nenhum worker estiver online, um novo job confirmado fica `queued` e a UI mostra
“aguardando worker”. Um job em `awaiting_source_review` não precisa de worker até a
confirmação. A UI **nunca** executa o pipeline como fallback. Ao iniciar um worker, ele
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

Um job `interrupted`/`resumable` recuperável é retomado por `POST /api/ui/resume`: cria um
novo attempt (`attempt+1`, com `previous_job_id`) reusando o mesmo diretório de saída;
checkpoints válidos de estágios já concluídos são reaproveitados. A linha original **não é
recolocada na fila** — ela permanece como o attempt anterior preservado.

A retomada é recusada quando:

| Recusa | Motivo |
| --- | --- |
| `job_type_not_resumable_from_ui` | Não é um job de tradução (publicação de comunidade tem recuperação própria no worker) |
| `Somente jobs interrompidos podem ser retomados.` | Status fora de `interrupted`/`resumable` |
| `job_not_recoverable` | Interrupção marcada `recoverable=0` — não há estado válido a continuar |
| `previous_attempt_still_running` | O runner do attempt anterior ainda está vivo |

Um segundo pedido para o mesmo job é **idempotente**: devolve o attempt já criado
(`already_resumed: true`) em vez de enfileirar o capítulo duas vezes.

A UI expõe isso como o botão **Retomar** (`#interruptedJobsPanel`), habilitado apenas pela
capability `can_resume` que o backend calcula — o frontend nunca deduz recuperabilidade a
partir do status.

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
- **Job em `awaiting_source_review`** → revise e confirme as páginas encontradas; o OCR
  ainda não foi iniciado.
- **Job `interrupted`** → se o backend marcou o job como recuperável, a UI mostra
  **Retomar**; caso contrário (`recoverable=0`) reenviar o capítulo é o caminho prático —
  os artefatos anteriores e o cache são preservados.
- **Worker duplicado** → o segundo sai limpo; verifique com `status`.
- **Porta 8080 ocupada** → outra UI já está rodando.
- **Banco bloqueado** → operação concorrente momentânea; o WAL + busy_timeout resolvem;
  não apague o banco.
- **Processo órfão após crash** → inicie um worker; ele reconcilia e encerra a árvore
  órfã. Não encerre processos por nome.
