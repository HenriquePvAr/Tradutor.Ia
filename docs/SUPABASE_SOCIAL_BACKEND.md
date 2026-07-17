# Backend social integrado ao Supabase (Data API + RLS)

Esta fase liga o backend do Tradutor.Ia ao Postgres social do Supabase. O navegador **não**
acessa as tabelas diretamente: chama endpoints autenticados do backend, que encaminham o
**JWT do próprio usuário** à Data API (PostgREST), onde a **RLS** revalida cada linha.

## Arquitetura

```
Frontend
  → backend Tradutor.Ia  (valida o JWT, cria RequestPrincipal)
  → user JWT + publishable key
  → Supabase Data API (PostgREST)
  → PostgreSQL + RLS
```

Duas camadas de autorização: (1) o backend valida o JWT e deriva a identidade do `sub`;
(2) o Postgres aplica RLS com o mesmo token. Nada confia apenas no frontend.

## Providers

`COMMUNITY_SOCIAL_PROVIDER` seleciona a implementação, com falha fechada:

- `supabase` (padrão) — `SupabaseSocialRepository` sobre a Data API.
- `local` — reconhecido mas **não implementado nesta fase** (as entidades sociais não têm
  store local pré-existente; a comunidade SQLite de PDFs é separada e intocada). Falha
  fechado, sem fallback silencioso. Provider desconhecido também falha fechado.

O `SupabaseSocialRepository`:

- usa a **publishable key** (`apikey`) + o **JWT real do usuário** (`Authorization: Bearer`);
- **nunca** usa `SUPABASE_SECRET_KEY`, `service_role`, senha do banco ou conexão
  administrativa; **nunca** faz bypass de RLS;
- **nunca** acessa `private.chapter_assets` nem retorna `storage_file_id`;
- não faz rede no import; o transporte HTTP é injetável (testes usam um fake).

## Fluxo JWT

O `SupabaseAuthProvider` valida o Bearer por JWKS e cria o `RequestPrincipal` (imutável,
`user_id = sub`). O router extrai o **mesmo** token bruto do header `Authorization` e o
entrega ao repositório, que o reenvia à Data API. O token não é persistido nem logado; o
backend nunca fabrica um JWT.

## Cliente Data API

`SupabaseDataClient` (server-side) fala só com `<SUPABASE_URL>/rest/v1` (URL construída de
config confiável, nunca do cliente). Headers: `apikey`, `Authorization: Bearer`,
`Content-Type`. Timeouts de conexão/leitura; resposta limitada (4 MiB); JSON defensivo;
sem redirects; sem retry em POST/PATCH/DELETE; falha fechado. Mapa de erros (sem SQL bruto):

| Situação | HTTP |
|---|---|
| JWT ausente/inválido | 401 |
| RLS negou / linha invisível ou não sua | 404 (nunca revela existência) |
| unique violation | 409 |
| constraint/validação/entrada inválida | 422 |
| rate limit | 429 |
| Supabase indisponível / resposta grande demais | 503 |

## Endpoints (prefixo `/api/community/social`)

- **Perfis**: `GET /profile/me`, `PATCH /profile/me`, `GET /profiles/{username}`.
- **Obras**: `GET /feed`, `GET /my-works`, `POST /works`, `GET/PATCH/DELETE /works/{id}`.
- **Capítulos**: `GET/POST /works/{id}/chapters`, `GET/PATCH/DELETE /chapters/{id}`.
- **Comentários**: `GET/POST /chapters/{id}/comments`, `PATCH/DELETE /comments/{id}`.
- **Likes**: `PUT/DELETE /chapters/{id}/like`, `PUT/DELETE /comments/{id}/like`.
- **Favoritos**: `GET /favorites`, `PUT/DELETE /works/{id}/favorite`.
- **Histórico**: `GET /history`, `PUT /chapters/{id}/history`.
- **Denúncias**: `GET /reports/my`, `POST /reports`.
- **Notificações**: `GET /notifications`, `PATCH /notifications/{id}/read`.

## Propriedade

`owner_id`/`author_id`/`user_id`/`reporter_id`/`recipient_id` derivam sempre de
`RequestPrincipal.user_id`. Campos de identidade/role num body de cliente são **rejeitados**
(`422 client_identity_not_allowed`); `X-User-Id`/`X-Role` não autenticam (só o Bearer conta).
`owner_id`/`author_id` são imutáveis (triggers no banco). A RLS reforça tudo de novo.

## Soft delete

Excluir obra/capítulo/comentário é **lógico** (`deleted_at`), nunca físico. Um comentário
apagado preserva as respostas; o texto é omitido (o DTO devolve `content:""`,
`is_deleted:true`). Likes/favoritos/histórico permitem remoção real da própria linha.

## Paginação e DTOs

Listas usam **cursor keyset** estável (`created_at`/`last_read_at` + id), opaco (base64),
com `limit` padrão 20 e máximo 100; a resposta é `{items, next_cursor}`. Os DTOs são
**whitelists** de colunas (`select=` explícito): nunca `storage_file_id`, checksum,
`byte_size` privado, SQL, JWT ou refresh token.

## Idempotência (likes/favoritos)

Likes e favoritos usam `INSERT ... ON CONFLICT DO NOTHING` (as tabelas têm policy de
INSERT/DELETE, não de UPDATE). O histórico usa upsert com merge (tem policy de UPDATE).

## Correção de RLS incluída

`20260717120000_fix_chapter_read_policy.sql` (aditiva, só `ALTER POLICY`): a policy de
SELECT de `chapters` passou a referenciar as colunas da própria linha (`work_id`, `status`)
em vez de `can_read_chapter(id)`, que re-consultava `chapters` — durante `INSERT ...
RETURNING` a linha nova ainda não é visível ao scan aninhado, negando ao owner o capítulo
recém-criado. `can_read_chapter` segue usada por comentários/likes (o capítulo já existe).

## Como executar/testar localmente

```bash
npx supabase start              # stack local (Docker)
npx supabase db reset --local   # aplica as 3 migrations
npx supabase test db            # pgTAP (104 pontos)
npx supabase db lint --level error
.\.venv\Scripts\python.exe -m pytest -q   # 805 offline (fake transport)
```

Os testes end-to-end usam JWTs locais reais contra a stack local; nenhum dado é escrito no
Supabase remoto e o Google Drive não é acessado.

## Por que sem secret/service_role e sem acesso direto do navegador

A `SUPABASE_SECRET_KEY`/`service_role` dariam poder administrativo que ignora a RLS — o
runtime normal (login + validação de JWT + Data API) não precisa dela, então ela foi
removida do `.env`. O navegador ainda passa pelo backend para: manter uma fronteira única
de validação/DTO/erros, evitar expor detalhes internos e permitir evoluir a política sem
mudar o cliente.

## Limitações

- Provider `local` diferido; a comunidade SQLite de PDFs continua separada e intocada.
- Criação de notificações é reservada ao backend/futuro (sem caminho de escrita pelo cliente).
- Sem UI, Storage, upload, roles admin/moderator, realtime — fora de escopo desta fase.
