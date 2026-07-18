# Retenção, restauração, lixeira e reconciliação dos PDFs sociais

Quando um PDF deixa de ser o arquivo ativo de um capítulo (**substituição** ou
**desvinculação**), ele **não é apagado**. Passa a ser um *asset retido*, com prazo. O owner
pode restaurá-lo dentro do prazo; só depois disso uma varredura manual pode movê-lo para a
**lixeira do Google Drive**. Nada nesta fase apaga permanentemente um arquivo.

## Regras invioláveis

| Regra | Onde é garantida |
| --- | --- |
| Nunca apagar imediatamente em replace/unlink | `social_pdf_publishing.py` chama `retain_superseded_asset` / `retain_unlinked_asset` |
| Nunca apagar permanentemente | nenhum caminho chama `delete_file`; teste `test_no_permanent_delete_anywhere_in_the_retention_code` |
| Nunca esvaziar a lixeira | idem |
| Nunca mover para a lixeira arquivo ainda referenciado | `evaluate_for_trash` → `still_referenced` |
| Nunca mover antes do prazo | `evaluate_for_trash` → `within_retention` |
| Ambiguidade nunca vira ação | qualquer dúvida → `reconcile_required` (revisão humana) |
| Nunca varrer o Drive nem tocar arquivos pessoais | escopo `drive.file` + consultas só por id conhecido, resolvido server-side |
| Nada roda no import nem no startup | varredura só pelo CLI, com `--apply` explícito |

## Ciclo de vida

```
                  replace/unlink
   (ativo) ─────────────────────────► retained ──restore──► restored (terminal)
                                         │
                              prazo vencido + sem referência
                                         ▼
                                   pending_trash ──ok──► trashed (terminal p/ este fase)
                                         │
                                       erro ──► failed ──(retry)──┘
                                         │
                                    ambiguidade ──► reconcile_required ──► retained | ignored
```

As transições permitidas ficam em `ChapterAssetRepository.TRANSITIONS` e são validadas **no
repositório** (fail-closed), de modo que um chamador que ignore a camada de serviço também é
barrado. Toda transição usa trava otimista (`version`) e grava no `social_asset_audit_log`.

## Configuração

| Variável | Padrão | Efeito |
| --- | --- | --- |
| `COMMUNITY_ASSET_RETENTION_DAYS` | `30` | prazo de retenção; limitado a 1–365; valor inválido volta ao padrão (nunca 0) |
| `COMMUNITY_ASSET_RETENTION_SWEEP_ENABLED` | desligado | só o valor exato `1` habilita; qualquer outro valor mantém desligado |

## Endpoints (todos owner-only, autenticados)

| Método | Rota | Retorna |
| --- | --- | --- |
| `GET` | `/api/community/social/retained-assets` | lista dos assets retidos do próprio usuário |
| `GET` | `/api/community/social/chapters/{id}/asset/retention` | estado da retenção do capítulo |
| `POST` | `/api/community/social/chapters/{id}/asset/restore` | restaura (corpo **vazio**) |

Anônimo recebe 401; usuário não-owner recebe **404** (anti-enumeração). O corpo do restore não
aceita **nenhum** campo — `owner_id`, `state`, `retain_until`, `publication_id`, `force` e
afins retornam 422. Não existe endpoint para forçar lixeira, alterar estado ou prazo, apagar
definitivamente ou disparar reconciliação.

O DTO devolvido carrega apenas `chapter_id`, `state`, `reason`, datas, `days_remaining` e
`restorable` — nunca id do Drive, `storage_file_id`, `publication_id`, caminho, URL ou checksum.

## Restauração

Restaurar exige: retenção do próprio owner, estado `retained`/`pending_trash` e prazo aberto.
Se o capítulo já tiver um PDF ativo, o pedido falha com **409** em vez de sobrescrever em
silêncio. Se o arquivo já foi para a lixeira, o pedido falha com `asset_already_trashed`: o
provider atual não implementa *untrash* e o sistema não finge que o arquivo voltou.

## CLI de manutenção (nunca automático)

```
python social_asset_maintenance_cli.py retention-scan
python social_asset_maintenance_cli.py retention-sweep            # dry-run
python social_asset_maintenance_cli.py retention-sweep --apply
python social_asset_maintenance_cli.py reconcile                  # dry-run
python social_asset_maintenance_cli.py reconcile --apply
```

Sem `--apply` nada é modificado. Cada execução grava um registro em
`social_asset_reconcile_runs` com contagens (`scanned`, `safe`, `ambiguous`, `changed`,
`failed`).

## Categorias da reconciliação

| Categoria | Significado | Correção automática |
| --- | --- | --- |
| `healthy` | estado local atrás do Drive (`pending_trash` + Drive já confirma lixeira) | sim, com `--apply` |
| `retained` | dentro do prazo, ou ainda referenciado | não (nada a fazer) |
| `safe_for_trash` | prazo vencido e sem referência | não — só o sweep age |
| `orphan_candidate` | sem referência e fora do fluxo normal | não |
| `reconcile_required` | arquivo remoto ausente, publicação irresolúvel, ou local `trashed` com remoto ativo | **não** — revisão humana |

## Falhas

Um erro do provider grava apenas o **nome da classe** da exceção em `last_error_code`
(nunca a mensagem, o id do Drive ou credenciais), incrementa `attempt_count` e leva a
retenção para `failed`, que continua elegível para nova tentativa (`retry_pending`).

## Fora de escopo (não implementado nesta fase)

Exclusão permanente, esvaziamento de lixeira, *untrash*, Supabase Storage, migração para
`private.chapter_assets`, `service_role`, Edge Functions, painel administrativo, moderação e
varredura ampla do Google Drive.

## Migração

Apenas SQLite, aditiva: `social_asset_retention`, `social_asset_audit_log` e
`social_asset_reconcile_runs`, mais índices — incluindo um índice único parcial que impede
retenções vivas duplicadas para o mesmo par (capítulo, publicação). Nenhuma tabela existente
é alterada ou reescrita, e **nenhuma migração no Supabase** é necessária.
